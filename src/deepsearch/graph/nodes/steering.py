"""task_steering 节点(定稿 Q29/R46:不抢占,仅写通道)。

转向指令 append 进 `pending_directives` 累加通道——同 thread_id 即同一段
checkpoint,故指令会被"正在跑的研报任务"在其下一个 human gate(CP1/CP2)
读到并消费,绝不打断当前正在执行的节点(单进程串行 worker 天然保证)。

"停/暂停"类关键词额外置 `stop_requested=True`:这是节点边界的提前
interrupt 逃生门标志,P4 的人闸在 interrupt 前会检查该标志并优先响应。
"""

from __future__ import annotations

from typing import Any

from deepsearch.graph.state import DeepSearchState

_STOP_KEYWORDS = ("停", "暂停", "中止", "先别", "别做了", "stop", "pause", "cancel")


def steering_node(state: DeepSearchState) -> dict[str, Any]:
    directive_text = state.get("input", "").strip()
    is_stop = _contains_stop(directive_text)

    directive: dict[str, Any] = {
        "text": directive_text,
        "is_stop": is_stop,
    }
    update: dict[str, Any] = {
        "pending_directives": [directive],  # 累加通道:下一 human gate 消费
        "answer": _ack(directive_text, is_stop),
        "needs_human": False,
    }
    if is_stop:
        update["stop_requested"] = True
    return update


def _contains_stop(text: str) -> bool:
    lowered = text.lower()
    return any(k in text or k in lowered for k in _STOP_KEYWORDS)


def _ack(text: str, is_stop: bool) -> str:
    if is_stop:
        return "已收到暂停指令,将在当前步骤结束的安全边界处停下。"
    return "已将你的调整加入待办,会在下一个确认节点纳入考虑。"
