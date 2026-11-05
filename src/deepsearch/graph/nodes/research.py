"""调研段流水线节点(定稿 拓扑 v6 · 研报主线前半)。

plan_queries →(data_pipeline ∥ dual_search)→ merge_align → assess_coverage
assess_coverage 判定证据缺口:有缺口且未达补搜上限 → 回边 dual_search;
否则 → 子图 END(交后续 outline/CP1)。

真相源:L2。素材原文入 FTS 任务池,data 段 parquet 入 reports/;state 只存
source_ids(doc_id 引用)与指标摘要,不搬运大段文本。所有外部调用 best-effort,
失败即记为证据缺口,绝不抛穿主链路。
"""

from __future__ import annotations

import json
import re
from typing import Any

from deepsearch.config import get_settings
from deepsearch.graph.state import DeepSearchState
from deepsearch.memory import llm
from deepsearch.tools import akshare_data, fts, search

# 数值主张抽取:百分比 / 金额(亿/万)——用于逐题冲突比对
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|亿元?|万元?|千亿|倍)")

# 来源权威性分级(三级裁决第一级:权威性排序)。数值越大越权威。
_AUTHORITY = {
    "akshare": 5,  # 结构化财务数据
    "private_pdf": 4,  # 用户上传一手资料
    "tavily": 2,
    "bocha": 2,
}
_KNOWN_AUTHORITATIVE_DOMAINS = (
    "sse.com.cn", "szse.cn", "cninfo.com",  # 交易所/披露
    "stats.gov.cn", "pbc.gov.cn", "cbirc.gov.cn",  # 官方
)


# ---------- ① plan_queries ----------

_PLAN_SYSTEM = (
    "你是投研规划师。把用户的研报主题拆解成 5-8 个互不重叠、可独立检索取证的关键问题,"
    "覆盖:行业格局、竞争壁垒、财务质量、增长驱动、风险因素、估值水位。"
    "严格输出 JSON:{\"questions\": [\"...\", ...]},不要多余文字。"
)


def plan_queries(state: DeepSearchState) -> dict[str, Any]:
    if state.get("research_plan"):  # 续跑/重入不重复规划
        return {}
    topic = state.get("report_topic") or state.get("input", "")
    settings = get_settings()
    questions: list[str] = []
    try:
        raw = llm.chat(
            [
                {"role": "system", "content": _PLAN_SYSTEM},
                {"role": "user", "content": f"研报主题:{topic}"},
            ],
            tier="primary",
            temperature=0.3,
        ).content
        questions = _parse_questions(raw)
    except Exception:
        questions = []
    if not questions:  # 兜底:主题即问题
        questions = [topic]

    plan = [
        {"id": f"q{i}", "question": q}
        for i, q in enumerate(questions[: settings.plan_max_questions], start=1)
    ]
    return {"research_plan": plan, "report_topic": topic}


# ---------- ③ data_pipeline ----------

def data_pipeline(state: DeepSearchState) -> dict[str, Any]:
    topic = state.get("report_topic") or state.get("input", "")
    plan_text = " ".join(p["question"] for p in state.get("research_plan", []))
    result = akshare_data.run_data_pipeline(
        state["thread_id"], f"{topic} {plan_text}"
    )
    symbol = result.get("symbol")
    findings = {f"data_pipeline_{symbol or 'none'}": result}
    # 指标也作为一手素材入任务池,供 merge/章节引用
    if result.get("status") == "ok" and result.get("metrics"):
        doc_id = fts.index_document(
            state["thread_id"],
            title=f"{symbol} 基础财务指标(内部数据)",
            content=json.dumps(result["metrics"], ensure_ascii=False),
            url="akshare",
            source="akshare",
        )
        findings["_source_ids"] = [doc_id]
    return {"data_findings": findings}


# ---------- ④ dual_search(本地 FTS ∥ 网络) ----------

def dual_search(state: DeepSearchState) -> dict[str, Any]:
    settings = get_settings()
    task_id = state["thread_id"]
    used = 0  # 本轮增量(通道为累加 reducer)
    new_ids: list[str] = []
    budget_left = settings.web_search_budget_per_report - state.get(
        "search_calls_used", 0
    )

    for question in _pending_questions(state):
        # 本地链路:先查任务池已有素材(private PDF 等),不消耗网络预算
        fts.search(task_id, question, limit=settings.search_count_per_query)
        # 网络链路:预算内才发
        if budget_left <= 0:
            continue
        bundle = search.web_search(question, count=settings.search_count_per_query)
        used += 1
        budget_left -= 1
        for r in bundle.results:
            doc_id = fts.index_document(
                task_id,
                title=r.title,
                content=f"{question}\n{r.snippet}",
                url=r.url,
                source=r.provider,
            )
            new_ids.append(doc_id)

    return {"source_ids": new_ids, "search_calls_used": used}


# ---------- ⑤ merge_align(归并 + 三级裁决 + 分歧清单) ----------

def merge_align(state: DeepSearchState) -> dict[str, Any]:
    task_id = state["thread_id"]
    conflicts: list[dict[str, Any]] = []

    for question in [p["question"] for p in state.get("research_plan", [])]:
        hits = fts.search(task_id, question, limit=get_settings().search_count_per_query)
        docs = fts.get_documents([h.doc_id for h in hits])
        conflict = _arbitrate(question, docs)
        if conflict:
            conflicts.append(conflict)

    return {"conflict_notes": conflicts}


# ---------- ⑥ assess_coverage(证据缺口判定 + 补搜回边) ----------

def assess_coverage(state: DeepSearchState) -> dict[str, Any]:
    settings = get_settings()
    task_id = state["thread_id"]
    gaps = [
        p["question"]
        for p in state.get("research_plan", [])
        if not fts.search(task_id, p["question"], limit=1)
    ]
    rounds = state.get("coverage_rounds", 0)

    if gaps and rounds < settings.supplement_search_max_rounds:
        return {"coverage_gaps": gaps, "coverage_rounds": rounds + 1}

    # 收口:产出调研段摘要,冲突项转人工
    digest = _digest(state, gaps)
    return {
        "coverage_gaps": gaps,
        "answer": digest,
        "needs_human": bool(state.get("conflict_notes")),
    }


def assess_coverage_route(state: DeepSearchState) -> str:
    settings = get_settings()
    gaps = state.get("coverage_gaps") or []
    rounds = state.get("coverage_rounds", 0)
    if gaps and rounds < settings.supplement_search_max_rounds:
        return "supplement"
    return "done"


# ---------- 辅助 ----------

def _pending_questions(state: DeepSearchState) -> list[str]:
    gaps = state.get("coverage_gaps") or []
    if gaps:
        return gaps  # 补搜轮只打缺口
    return [p["question"] for p in state.get("research_plan", [])]


def _authority_of(doc: fts.CorpusDoc) -> int:
    score = _AUTHORITY.get(doc.source, 1)
    if any(d in (doc.url or "") for d in _KNOWN_AUTHORITATIVE_DOMAINS):
        score += 2
    return score


def _arbitrate(question: str, docs: list[fts.CorpusDoc]) -> dict[str, Any] | None:
    """逐题冲突比对:各源数值主张不一致时,按权威性→取新→分歧清单裁决。"""
    claims = {d.doc_id: set(_NUM_RE.findall(d.content or d.title)) for d in docs}
    non_empty = {k: v for k, v in claims.items() if v}
    if len(non_empty) < 2:
        return None
    distinct = {frozenset(v) for v in non_empty.values()}
    if len(distinct) <= 1:
        return None  # 各源数值一致,无冲突

    ranked = sorted(docs, key=_authority_of, reverse=True)
    winner = next(d for d in ranked if d.doc_id in non_empty)
    return {
        "question": question,
        "positions": [
            {"doc_id": d.doc_id, "url": d.url, "source": d.source,
             "figures": sorted(claims[d.doc_id])}
            for d in docs if d.doc_id in non_empty
        ],
        "resolved_with": winner.doc_id,
        "resolution_basis": "authority",
        "needs_human": _top_tie(ranked, non_empty),  # 权威并列→人工兜底
    }


def _top_tie(ranked: list[fts.CorpusDoc], non_empty: dict[str, set]) -> bool:
    top = None
    for d in ranked:
        if d.doc_id in non_empty:
            if top is None:
                top = _authority_of(d)
            elif _authority_of(d) == top:
                return True  # 最高权威层级出现并列
    return False


def _digest(state: DeepSearchState, gaps: list[str]) -> str:
    n_sources = len({sid for sid in state.get("source_ids", [])})
    lines = [
        f"调研段完成:主题「{state.get('report_topic', '')}」",
        f"关键问题 {len(state.get('research_plan', []))} 个,归集素材 {n_sources} 条,"
        f"网络检索 {state.get('search_calls_used', 0)} 次。",
    ]
    if state.get("data_findings"):
        lines.append("已获取内部财务数据(见 data_findings)。")
    conflicts = state.get("conflict_notes") or []
    if conflicts:
        lines.append(f"存在 {len(conflicts)} 处来源分歧,待人工定夺(CP 前会提示)。")
    if gaps:
        lines.append(f"补搜 {state.get('coverage_rounds', 0)} 轮后仍有 {len(gaps)} 题证据不足。")
    return "\n".join(lines)


def _parse_questions(raw: str) -> list[str]:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return []
    data = json.loads(raw[start : end + 1])
    return [str(q).strip() for q in data.get("questions", []) if str(q).strip()]
