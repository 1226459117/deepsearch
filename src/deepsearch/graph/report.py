"""研报流水线装配(调研段 P3 + 写作段 P4,共享主图 state 的模块化子图)。

不做独立编译子图:那样 source_ids / research_plan 等累加通道会在"子图输出
再并入主图"时被 reducer 二次追加。改为把节点直接注册进主 StateGraph 的
共享通道上,既保留流水线代码的内聚与可替换,又有正确的累加语义。

拓扑:
    plan_queries →(data_pipeline ∥ dual_search)→ merge_align → assess_coverage
    assess_coverage ─(缺口)→ dual_search(≤2 轮) / ─(done)→ outline_gen → CP1
    CP1 → dispatch_groups ─Send→ write_chapter → advance_group
    advance_group ─(下一组)→ dispatch_groups / → consistency_review
    consistency_review ─(脏章<2轮)→ dispatch_groups(重写) / → CP2
    CP2 → revise_dirty → assemble_final → render_export → END
    dispatch/review ─(成本超限)→ cost_pause ─(抬上限)→ dispatch / → END

CP1/CP2/cost_pause 是 interrupt 网关(定稿 Q26:独立网关节点固定必停);
human gate 处消费 pending_directives 并响应 stop_requested 逃生门。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.graph import END

from deepsearch.graph.nodes.research import (
    assess_coverage,
    assess_coverage_route,
    data_pipeline,
    dual_search,
    merge_align,
    plan_queries,
)
from deepsearch.graph.nodes.writing import (
    advance_group,
    assemble_final,
    consistency_review,
    cp1,
    cp2,
    cost_pause,
    dispatch_groups,
    outline_gen,
    render_export,
    revise_dirty,
    route_after_advance,
    route_after_cp1,
    route_after_cp2,
    route_after_cost_pause,
    route_after_review,
    write_chapter,
)

if TYPE_CHECKING:
    from langgraph.graph.state import StateGraph

ENTRY_NODE = "plan_queries"


def add_report_pipeline(builder: "StateGraph") -> str:
    """把调研段+研报写作段全部节点装进 builder,返回入口节点名。"""
    # ---- 调研段(P3) ----
    builder.add_node("plan_queries", plan_queries)
    builder.add_node("data_pipeline", data_pipeline)
    builder.add_node("dual_search", dual_search)
    builder.add_node("merge_align", merge_align)
    builder.add_node("assess_coverage", assess_coverage)

    builder.add_edge("plan_queries", "data_pipeline")
    builder.add_edge("plan_queries", "dual_search")  # 数据段 ∥ 检索段
    builder.add_edge("data_pipeline", "merge_align")
    builder.add_edge("dual_search", "merge_align")
    builder.add_edge("merge_align", "assess_coverage")
    builder.add_conditional_edges(
        "assess_coverage",
        assess_coverage_route,
        {"supplement": "dual_search", "done": "outline_gen"},
    )

    # ---- 写作段(P4) ----
    builder.add_node("outline_gen", outline_gen)
    builder.add_node("CP1", cp1)
    builder.add_node("dispatch_groups", dispatch_groups)
    builder.add_node("write_chapter", write_chapter)
    builder.add_node("advance_group", advance_group)
    builder.add_node("consistency_review", consistency_review)
    builder.add_node("CP2", cp2)
    builder.add_node("revise_dirty", revise_dirty)
    builder.add_node("assemble_final", assemble_final)
    builder.add_node("render_export", render_export)
    builder.add_node("cost_pause", cost_pause)

    builder.add_edge("outline_gen", "CP1")
    builder.add_conditional_edges(
        "CP1", route_after_cp1, {"abort": END, "dispatch": "dispatch_groups"}
    )
    # dispatch_groups 运行时返回 Command(goto=[Send]) 或 Command(goto=cost_pause)
    builder.add_edge("write_chapter", "advance_group")  # Send 扇入锚点
    builder.add_conditional_edges(
        "advance_group",
        route_after_advance,
        {"dispatch_groups": "dispatch_groups", "consistency_review": "consistency_review"},
    )
    builder.add_conditional_edges(
        "consistency_review",
        route_after_review,
        {"cost_pause": "cost_pause", "rewrite": "dispatch_groups", "cp2": "CP2"},
    )
    builder.add_conditional_edges(
        "CP2", route_after_cp2, {"abort": END, "continue": "revise_dirty"}
    )
    builder.add_edge("revise_dirty", "assemble_final")
    builder.add_edge("assemble_final", "render_export")
    builder.add_edge("render_export", END)

    builder.add_conditional_edges(
        "cost_pause",
        route_after_cost_pause,
        {"abort": END, "dispatch_groups": "dispatch_groups"},
    )
    return ENTRY_NODE
