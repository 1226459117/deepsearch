"""主图装配:四档分诊入口 + 研报调研段流水线 + checkpoint 编译。

    START → triage ─┬─ chat      → END
                    ├─ factual   → END
                    ├─ steering  → END
                    └─ report → plan_queries → …调研段流水线… → END

`build_graph(checkpointer)` 返回可 invoke 的编译图;生产用 open_checkpointer()
的 SqliteSaver,测试用 memory_checkpointer()。同一 thread_id 的多次 invoke
经 checkpointer 累加 state(pending_directives / source_ids 等),支撑
可恢复与 steering 跨调用投递。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from deepsearch.graph.checkpoint import memory_checkpointer
from deepsearch.graph.nodes.chat import chat_node, factual_node
from deepsearch.graph.nodes.steering import steering_node
from deepsearch.graph.nodes.triage import route_from_state, triage_node
from deepsearch.graph.report import add_report_pipeline
from deepsearch.graph.state import (
    ROUTE_CHAT,
    ROUTE_FACTUAL,
    ROUTE_REPORT,
    ROUTE_STEERING,
    DeepSearchState,
    new_state,
)


def build_graph(checkpointer: Any = None):
    graph = StateGraph(DeepSearchState)

    graph.add_node("triage", triage_node)
    graph.add_node("chat", chat_node)
    graph.add_node("factual", factual_node)
    graph.add_node("steering", steering_node)
    report_entry = add_report_pipeline(graph)  # 注册调研段五节点

    graph.add_edge(START, "triage")
    graph.add_conditional_edges(
        "triage",
        route_from_state,
        {
            ROUTE_CHAT: "chat",
            ROUTE_FACTUAL: "factual",
            ROUTE_REPORT: report_entry,
            ROUTE_STEERING: "steering",
        },
    )
    for node in ("chat", "factual", "steering"):
        graph.add_edge(node, END)

    return graph.compile(checkpointer=checkpointer or memory_checkpointer())


def run_turn(
    graph,
    thread_id: str,
    input_text: str = "",
    *,
    route: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    resume: Any = None,
) -> DeepSearchState:
    """跑一轮:构造入参 → invoke → 返回最终 state(可能停在 interrupt)。

    route 传入即人工改档(triage 尊重,不再调用模型判定)。
    resume 非 None 即对当前 thread 挂起的 gate(CP1/CP2/cost)提交决策。
    """
    config = {"configurable": {"thread_id": thread_id}}
    if resume is not None:
        return graph.invoke(Command(resume=resume), config)
    state = new_state(
        thread_id, input_text, tenant_id=tenant_id, user_id=user_id
    )
    if route:
        state["route"] = route
    return graph.invoke(state, config)


def interrupts_of(result: DeepSearchState) -> list[dict[str, Any]]:
    """从 invoke 结果提取挂起的 human gate payload(web 层弹窗用)。"""
    return [getattr(i, "value", i) for i in result.get("__interrupt__", [])]
