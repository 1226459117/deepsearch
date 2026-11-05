"""LangGraph 状态 schema(定稿 Q28 / R6 双真相源)。

设计约束:
- 累加通道:凡需跨节点合并的列表一律用 `add_channel`(operator.add 语义),
  节点返回增量而非全量;
- 章节用 dict reducer:chapter_write 并行扇出各自写 `{chapter_id: draft}`,
  reducer 按 key 合并,组间传摘要求覆盖同 key;
- 只存 ID 不存原文:检索素材以 source_ids 引用落 state,原文在 L1 PG 归档 /
  L2 sources 目录 / L3 Milvus,state 保持精简(checkpoint 不被大文本撑爆);
- chat 对话不写本 state(以 Redis 为唯一真相源,见 R46),仅经 service 门面落库。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

# ---- reducers ----


def add_channel(left: list[Any], right: list[Any]) -> list[Any]:
    """列表累加通道:右值追加到左值。LangGraph 对同 key 多次写入合并。"""
    return [*left, *right]


def merge_dict(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """字典按 key 浅合并:右值覆盖同名 key(chapter_write 扇出专用)。"""
    return {**left, **right}


def last_value(left: Any, right: Any) -> Any:
    """后写覆盖(用于并发下只需保留最新一次的标量通道)。"""
    return right


def add_number(left: float, right: float) -> float:
    """数值累加通道:成本核算专用(并行扇出各自上报增量,不可后写覆盖)。"""
    return (left or 0.0) + (right or 0.0)


# ---- 分诊档位 ----

ROUTE_CHAT = "chat"
ROUTE_FACTUAL = "factual"
ROUTE_REPORT = "report"
ROUTE_STEERING = "steering"
ALL_ROUTES = (ROUTE_CHAT, ROUTE_FACTUAL, ROUTE_REPORT, ROUTE_STEERING)


class DeepSearchState(TypedDict):
    """主图共享状态。字段按三域归属注释;研报工作记忆=L2(checkpoint 持久化)。"""

    # ===== 入参与身份 =====
    thread_id: str
    tenant_id: str
    user_id: str
    input: str  # 本轮用户消息

    # ===== 分诊 =====
    route: str  # ALL_ROUTES 之一,由 triage 写入,条件边读取
    triage_confidence: float  # 低置信 → web 层弹窗人工选档(Q34 兜底)

    # ===== 面向 web 的输出(非累加,末值) =====
    answer: str  # chat/factual 的直接回复
    needs_human: bool  # 低置信或 human gate:等待人工介入

    # ===== task_steering(R46:仅写通道,下一 human gate 消费) =====
    pending_directives: Annotated[list[dict[str, Any]], add_channel]
    consumed_directives: Annotated[int, last_value]  # 已被 gate 消费的指令数(游标)
    stop_requested: bool  # "停/暂停"关键词逃生门,节点边界触发提前 interrupt

    # ===== 研报工作记忆 L2(P3/P4 填充,P2 先声明通道) =====
    report_topic: str
    research_plan: Annotated[list[dict[str, Any]], add_channel]  # 5–8 调研问题
    source_ids: Annotated[list[str], add_channel]  # 素材卡 ID,不存原文(指向 FTS 任务池)
    conflict_notes: Annotated[list[dict[str, Any]], last_value]  # 分歧清单:merge_align 每轮重算
    coverage_rounds: Annotated[int, last_value]  # assess_coverage 计数(≤2 轮补搜)
    coverage_gaps: Annotated[list[str], last_value]  # 仍缺证据的调研问题
    search_calls_used: Annotated[int, add_number]  # 已用网络检索次数(各节点报增量,成本护栏)
    data_findings: Annotated[dict[str, Any], merge_dict]  # AkShare 指标摘要

    # ===== 研报写作段(P4) =====
    outline: Annotated[list[dict[str, Any]], last_value]  # [{id,title,bullets}]
    chapters: Annotated[dict[str, Any], merge_dict]  # {chapter_id: 草稿 MD}
    chapter_summaries: Annotated[dict[str, str], merge_dict]  # 组间串行传摘要
    group_idx: Annotated[int, last_value]  # 当前写作组游标(分组扇出)
    chapter_directives: Annotated[dict[str, Any], last_value]  # {id: 定向重写指令}
    review_rounds: Annotated[int, last_value]  # consistency_review 计数(≤2 轮重写)
    review_findings: Annotated[list[dict[str, Any]], last_value]  # 五维审稿发现
    dirty_chapters: Annotated[list[str], last_value]  # 待重写章节 id
    human_edits: Annotated[dict[str, str], last_value]  # CP2 人工改稿 {id: md}
    conflict_resolutions: Annotated[list[dict[str, Any]], last_value]  # CP2 人工定夺
    report_dir: str  # assemble_final 产出的 reports/<date>-<slug>/ 目录

    # ===== 成本护栏 =====
    cost_accrued_cny: Annotated[float, add_number]  # 各节点 LLM 开销累加(¥5/篇超限暂停)
    cost_limit_cny: Annotated[float, last_value]  # 当前上限,pause 后人可抬高


def new_state(
    thread_id: str,
    input_text: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> DeepSearchState:
    """构造带完整累加通道初始值的入参。

    累加通道显式初始化为空容器(而非依赖 LangGraph 惰性默认),既保证
    首次 invoke 各 reducer 有合法左值,也让 checkpoint 首轮快照自洽。
    """
    from deepsearch.config import get_settings

    settings = get_settings()
    return DeepSearchState(
        thread_id=thread_id,
        tenant_id=tenant_id or settings.tenant_id,
        user_id=user_id or settings.user_id,
        input=input_text,
        route="",
        triage_confidence=0.0,
        answer="",
        needs_human=False,
        pending_directives=[],
        consumed_directives=0,
        stop_requested=False,
        report_topic="",
        research_plan=[],
        source_ids=[],
        conflict_notes=[],
        coverage_rounds=0,
        coverage_gaps=[],
        search_calls_used=0,
        data_findings={},
        outline=[],
        chapters={},
        chapter_summaries={},
        group_idx=0,
        chapter_directives={},
        review_rounds=0,
        review_findings=[],
        dirty_chapters=[],
        human_edits={},
        conflict_resolutions=[],
        report_dir="",
        cost_accrued_cny=0.0,
        cost_limit_cny=settings.cost_limit_cny_per_report,
    )
