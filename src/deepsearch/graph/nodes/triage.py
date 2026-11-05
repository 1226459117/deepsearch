"""① triage 分诊节点(定稿 Q34 四档 + 低置信人工兜底)。

四档:chat(闲聊/无关)、factual(小型事实题)、report(研报选题)、
steering(在跑任务的转向指令)。分档由 utility 档模型判定,输出 JSON。

人工兜底:置信 < 阈值 → needs_human=True,web 层弹窗让用户改档;改档后
重新 invoke 时把 route 预置进 state,triage 直接尊重(免二次判定)。
LLM 故障/解析失败 → 保守回落 chat 且 needs_human=True,绝不误触发研报重流程。
"""

from __future__ import annotations

import json
from typing import Any

from deepsearch.graph.state import (
    ALL_ROUTES,
    ROUTE_CHAT,
    ROUTE_FACTUAL,
    ROUTE_REPORT,
    ROUTE_STEERING,
    DeepSearchState,
)
from deepsearch.memory import intercept, llm

CONFIDENCE_FLOOR = 0.55  # 低于此值需人工确认档位

_SYSTEM = (
    "你是投研智能系统的入口分诊器。把用户消息分到且仅分到以下四档之一,输出严格 JSON,不要多余文字:\n"
    '{{"route": "chat|factual|report|steering", "confidence": 0.0~1.0, "topic": "若为report则提取研报主题,否则留空"}}\n'
    "判定标准:\n"
    "- chat:问候、天气、闲聊、与投资研究无关的通用问题;\n"
    "- factual:有明确答案的小型事实/数据查询(如某公司营收、某指标数值),无需成深度报告;\n"
    "- report:要求行业/公司深度分析、研报、系统调研(会触发多步流水线);\n"
    "- steering:对某个正在运行的研报任务下达调整/补充/暂停指令。\n"
    "拿不准时压低 confidence,不要臆造 route。"
)


def triage_node(state: DeepSearchState) -> dict[str, Any]:
    # 人工改档优先:web 层已把最终 route 预置好则直接放行
    preset = state.get("route")
    if preset in ALL_ROUTES:
        return {"route": preset, "needs_human": False}

    query = state.get("input", "")
    profile_hint = intercept.injection_for_node("triage", query)
    try:
        raw = llm.chat(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _with_hint(query, profile_hint)},
            ],
            tier="utility",
            temperature=0.0,
        ).content
        route, confidence, topic = _parse(raw)
    except Exception:
        route, confidence, topic = ROUTE_CHAT, 0.0, ""

    update: dict[str, Any] = {"route": route, "triage_confidence": confidence}
    if topic:
        update["report_topic"] = topic
    if confidence < CONFIDENCE_FLOOR:
        update["needs_human"] = True
    return update


def route_from_state(state: DeepSearchState) -> str:
    """条件边路由函数:返回四档之一,chat 为安全默认。"""
    return state.get("route") if state.get("route") in ALL_ROUTES else ROUTE_CHAT


def _with_hint(query: str, hint: str) -> str:
    return f"{query}\n\n〔参考:用户近期关注域〕\n{hint}" if hint else query


def _parse(raw: str) -> tuple[str, float, str]:
    """从模型回复中提取首个 JSON 对象并校验档位。"""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("回复无 JSON")
    data = json.loads(text[start : end + 1])
    route = str(data.get("route", "")).strip().lower()
    if route not in (ROUTE_CHAT, ROUTE_FACTUAL, ROUTE_REPORT, ROUTE_STEERING):
        raise ValueError(f"非法档位 {route!r}")
    confidence = float(data.get("confidence", 0.0))
    confidence = min(max(confidence, 0.0), 1.0)
    topic = str(data.get("topic", "")).strip()
    return route, confidence, topic
