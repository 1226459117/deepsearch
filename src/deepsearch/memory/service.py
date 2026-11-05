"""记忆门面:chat 单轮落库的跨域协调器(P1 收口)。

三域禁交叉写(R46),但"一轮对话"的完整落库天然跨三域,统一由此门面
按固定顺序编排,任何模块不得绕过门面直接双写:

    1. PG 归档全量原文(archive.append_turn)——永久真相;
    2. Redis 窗口推入该轮(window.push_turn);
    3. 满 30 轮触发压缩:utility 档摘要 → PG 落摘要行 →
       Milvus 入摘要向量 → Redis 裁掉旧 20 轮。

压缩失败(模型故障)只影响窗口膨胀,不影响消息安全:原文已在 PG。

存储降级契约(定稿:"存储不可达时记忆注入静默降级为空"):
PG/Redis/Milvus 任一不可达只告警不抛出——chat/factual 短链路不能因
自备存储离线而整单失败(浏览器走查实测:Redis 超时会炸穿 worker)。
"""

from __future__ import annotations

import logging
from typing import Any

from deepsearch.memory import archive, vectors
from deepsearch.memory.window import ConversationWindow

log = logging.getLogger(__name__)


def _degrade(op: str, exc: Exception) -> None:
    """存储故障 best-effort:告警留痕,主链路继续。"""
    log.warning("memory %s unavailable, degraded: %r", op, exc)


def post_turn(
    thread_id: str,
    user_content: str,
    assistant_content: str,
    *,
    compressor=None,
    vector_writer: Any = None,
    client=None,
) -> dict[str, Any]:
    """chat/factual 节点每轮结束后调用。返回窗口状态与压缩结果。

    compressor/vector_writer/client 均为测试或降级预留的注入位,默认走
    真实链路(utility 档摘要 / Milvus / .env Redis)。
    """
    win = ConversationWindow(thread_id, client=client)
    try:
        archive.append_turn(thread_id, user_content, assistant_content)
    except Exception as exc:  # PG 离线:丢一轮归档,对话继续
        _degrade("pg_append", exc)

    turn_no, rounds, needs = -1, 0, False
    try:
        turn_no = win.push_turn(user_content, assistant_content)
        rounds = win.turn_count()
        needs = win.needs_compaction()
    except Exception as exc:  # Redis 离线:窗口写不进去,降级为无状态单轮
        _degrade("window_push", exc)

    compacted = None
    if needs:
        vector_writer = vector_writer or vectors.upsert_conversation_summary

        def on_stored(summary: str, from_no: int, to_no: int) -> None:
            # 两域各自 best-effort:单边故障不阻断另一边,更不炸主链路
            milvus_id = None
            try:
                milvus_id = vector_writer(summary, thread_id, from_no, to_no)
            except Exception as exc:
                _degrade("milvus_summary", exc)
            try:
                archive.record_summary(thread_id, from_no, to_no, summary, milvus_id)
            except Exception as exc:
                _degrade("pg_summary", exc)

        try:
            compacted = win.compact(compressor=compressor, on_stored=on_stored)
        except Exception as exc:
            _degrade("compact", exc)
        if compacted is not None:  # 压缩裁过窗口,复算剩余轮数
            try:
                rounds = win.turn_count()
            except Exception as exc:
                _degrade("window_count", exc)

    return {
        "thread_id": thread_id,
        "turn_no": turn_no,
        "window_rounds": rounds,
        "compacted": compacted,
    }


def chat_context(
    thread_id: str, current_message: str, *, client=None
) -> list[dict[str, str]]:
    """组装发给模型的 messages:长期记忆注入(仅首轮)+ 窗口上下文 + 当前输入。"""
    from deepsearch.memory import intercept

    messages: list[dict[str, str]] = []
    try:
        if intercept.is_new_session(thread_id):
            injection = intercept.injection_for_node(
                "chat_first_turn", current_message, new_session=True
            )
            if injection:
                messages.append({"role": "system", "content": injection})
    except Exception as exc:  # intercept 内部已有降级,此为二道防线
        _degrade("intercept", exc)
    try:
        messages.extend(ConversationWindow(thread_id, client=client).build_context())
    except Exception as exc:  # Redis 离线:无窗口上下文,裸答继续
        _degrade("window_context", exc)
    messages.append({"role": "user", "content": current_message})
    return messages
