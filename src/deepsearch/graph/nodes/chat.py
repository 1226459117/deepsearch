"""chat / factual_answer 短链路节点(定稿 Q34 档一/档二,Q36 搜索护栏)。

两档都以 Redis 活跃窗口为对话唯一真相源(R46):每轮经 service.post_turn
落库(→ PG 归档 + 窗口压缩),上下文经 service.chat_context 组装
(含新会话首轮的长期记忆注入)。

自主搜索(Q36):经 agent_search.run_tool_loop 挂 web_search/get_financial,
额度按档位封顶——chat ≤1 次搜索(裸答为主,模型可自行查证一次);
factual ≤2 次搜索 + 1 次财务速查。
"""

from __future__ import annotations

from typing import Any

from deepsearch.config import get_settings
from deepsearch.graph.state import DeepSearchState
from deepsearch.memory import service
from deepsearch.tools import agent_search

_CHAT_SYSTEM = (
    "你是投研智能系统里的对话助手。对问候、常识或与深度研究无关的问题,"
    "自然、简洁地用中文回答。涉及具体时效信息但不确定的,可检索一次再答;"
    "仍不确定则坦率说明并建议发起调研。"
)

_FACTUAL_SYSTEM = (
    "你是投研数据速答助手。针对明确的小型事实/数据问题,可检索网络或查询财务数据,"
    "给出简明、可核验的回答,并标注数据来源与口径时间;无法确证的不要编造。"
)


def chat_node(state: DeepSearchState) -> dict[str, Any]:
    settings = get_settings()
    return _short_chain(
        state,
        _CHAT_SYSTEM,
        max_search=settings.chat_search_limit,
        max_akshare=0,
        with_escalate=False,
    )


def factual_node(state: DeepSearchState) -> dict[str, Any]:
    settings = get_settings()
    return _short_chain(
        state,
        _FACTUAL_SYSTEM,
        max_search=settings.factual_search_limit,
        max_akshare=settings.factual_akshare_limit,
        with_escalate=True,
    )


def _short_chain(
    state: DeepSearchState,
    system: str,
    *,
    max_search: int,
    max_akshare: int,
    with_escalate: bool,
) -> dict[str, Any]:
    thread_id = state["thread_id"]
    query = state.get("input", "")

    # 记忆增强后的上下文(首轮含长期记忆注入)+ 系统人设
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(service.chat_context(thread_id, query))

    answer, tool_calls_used = agent_search.run_tool_loop(
        messages, max_search=max_search, max_akshare=max_akshare, tier="primary"
    )
    answer = answer.strip()

    # 落 L1:Redis 窗口 + PG 归档 + 满窗压缩(经门面统一编排,禁节点直写)
    service.post_turn(thread_id, query, answer)

    update: dict[str, Any] = {
        "answer": answer,
        "needs_human": False,
        "search_calls_used": tool_calls_used,  # 累加通道只报增量
    }
    if with_escalate and _looks_researchable(query):
        update["report_topic"] = query  # factual 档提示可升级为研报选题
    return update


def _looks_researchable(query: str) -> bool:
    """粗启发:含研究类关键词时,事实答可挂"立项"提示。"""
    return any(k in query for k in ("分析", "研报", "行业", "前景", "值得", "怎么看出"))
