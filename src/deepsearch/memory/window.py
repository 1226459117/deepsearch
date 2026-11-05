"""L1 Redis 活跃窗口:key=dp:{tenant}:{user}:{thread}(定稿 R2/R42)。

规则:窗口最多存 window_rounds(30)轮;每轮 = user+assistant 一条 JSON
LIST 元素。达到 30 轮触发压缩:取最早的 summary_batch_rounds(20)轮交
廉价档模型滚动摘要,压缩产物三写——摘要文本+行引用落 PG(archive)、
摘要 embedding 落 Milvus(conversations 集合)、Redis 删掉这 20 轮原文,
只留最近 active_keep_rounds(10)轮 + 一个滚动摘要键。

滚动摘要链:第 N+1 次压缩以第 N 次摘要为背景续写,信息不丢只折叠。
Redis 只做窗口截断,全量原文永久在 PG 归档(禁止交叉写:本模块不写
PG 消息表,由调用方先 archive.append_turn 再 push_turn,见 service.py)。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import redis

from deepsearch.config import get_settings

# 压缩默认实现:廉价档滚动摘要(测试可注入替身)
DEFAULT_COMPRESSOR: Callable[[str, list[dict[str, str]]], str] | None = None


def _llm_compressor(prev_summary: str, turns: list[dict[str, str]]) -> str:
    """utility 档:将旧段落压缩/续写为一段滚动摘要。"""
    from deepsearch.memory import llm  # 延迟导入,避免循环依赖

    body = "\n".join(
        f"用户: {t['user']}\n助手: {t['assistant']}" for t in turns
    )
    context = f"已有摘要(此前对话的压缩):\n{prev_summary}\n\n" if prev_summary else ""
    result = llm.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是对话记忆压缩器。请把待压缩的对话段落归纳进滚动摘要:"
                    "保留用户意图、关键事实、结论与未决问题,丢弃寒暄与冗余。"
                    "输出合并后的完整摘要(不超过500字),不要加解释。"
                ),
            },
            {"role": "user", "content": f"{context}待压缩段落:\n{body}"},
        ],
        tier="utility",
        temperature=0.2,
    )
    return result.content.strip()


class ConversationWindow:
    """单 thread 的活跃窗口读写。线程安全依赖 redis-py 客户端本身。"""

    def __init__(
        self,
        thread_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        client: redis.Redis | None = None,
    ) -> None:
        settings = get_settings()
        self.thread_id = thread_id
        self._r = client or redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,  # 存储离线时快速失败,降级不拖垮节点耗时
            socket_timeout=5,
        )
        prefix = f"dp:{tenant_id or settings.tenant_id}:{user_id or settings.user_id}"
        self._turns_key = f"{prefix}:{thread_id}:turns"
        self._summary_key = f"{prefix}:{thread_id}:summary"
        self._base_key = f"{prefix}:{thread_id}:base"  # 窗口首轮的绝对轮号

    # ---- 读写原语 ----

    def _base(self) -> int:
        """窗口内第一轮对应的绝对轮号(从 1 起);键缺失按 1 处理。"""
        return int(self._r.get(self._base_key) or 1)

    def push_turn(self, user_content: str, assistant_content: str) -> int:
        """追加一轮,返回该轮绝对轮号(=基准+窗口内序-1)。

        满窗截断只发生在 compact 成功压缩时(窗口唯一截断点),
        push 阶段超限不丢弃,避免未摘要原文在 Redis 与 PG 之间失踪。
        """
        payload = json.dumps(
            {"user": user_content, "assistant": assistant_content}, ensure_ascii=False
        )
        self._r.rpush(self._turns_key, payload)
        return int(self._base() + self.turn_count() - 1)

    def turn_count(self) -> int:
        return int(self._r.llen(self._turns_key))

    def needs_compaction(self) -> bool:
        return self.turn_count() >= get_settings().window_rounds

    def get_turns(self) -> list[dict[str, str]]:
        raw = self._r.lrange(self._turns_key, 0, -1)
        return [json.loads(item) for item in raw]

    def get_summary(self) -> str:
        return self._r.get(self._summary_key) or ""

    def set_summary(self, summary: str) -> None:
        self._r.set(self._summary_key, summary)

    def drop(self) -> None:
        """会话结束/清空:L1 域只动 Redis(归档与向量是各域自己的责任)。"""
        self._r.delete(self._turns_key, self._summary_key, self._base_key)

    # ---- 组装 LLM 上下文 ----

    def build_context(self) -> list[dict[str, str]]:
        """返回可直接拼进 messages 的上下文:摘要(若有)+ 窗口原文轮次。"""
        messages: list[dict[str, str]] = []
        summary = self.get_summary()
        if summary:
            messages.append(
                {"role": "system", "content": f"【早前对话摘要】{summary}"}
            )
        for turn in self.get_turns():
            messages.append({"role": "user", "content": turn["user"]})
            messages.append({"role": "assistant", "content": turn["assistant"]})
        return messages

    # ---- 压缩 ----

    def compact(
        self,
        compressor: Callable[[str, list[dict[str, str]]], str] | None = None,
        on_stored: "Any | None" = None,
    ) -> dict[str, Any] | None:
        """满 30 轮时压缩最早 20 轮。

        compressor: (prev_summary, turns) -> 新摘要文本(默认 utility 档)。
        on_stored: 回调 (summary, from_no, to_no),由 service 层负责
                   PG 落行 + Milvus upsert(窗口模块不跨域写)。
        返回压缩描述 dict;未触发返回 None。
        """
        settings = get_settings()
        turns = self.get_turns()
        if len(turns) < settings.window_rounds:
            return None

        batch = turns[: settings.summary_batch_rounds]
        compressor = compressor or _llm_compressor
        prev = self.get_summary()
        new_summary = compressor(prev, batch)
        if not new_summary:
            return None  # 摘要失败不删原文,宁可窗口满也不丢信息

        from_no = self._base()
        to_no = from_no + len(batch) - 1

        self.set_summary(new_summary)
        # 删除已压缩的 len(batch) 轮并前移基准(窗口唯一截断点)
        self._r.ltrim(self._turns_key, len(batch), -1)
        # 基准 = 被裁轮数的下一轮;直接 set 而非 incrby,避开"键不存在时从 0 起算"的差一
        self._r.set(self._base_key, str(to_no + 1))

        if on_stored is not None:
            on_stored(new_summary, from_no, to_no)

        return {
            "thread_id": self.thread_id,
            "from_turn": from_no,
            "to_turn": to_no,
            "summary": new_summary,
            "kept_rounds": self.turn_count(),
        }
