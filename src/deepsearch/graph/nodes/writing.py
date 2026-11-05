"""研报段流水线节点(定稿 拓扑 v6 · 研报主线后半)。

outline_gen → CP1(必停) → dispatch_groups ──Send──▶ write_chapter ×N
    (组内并行、组间串行传摘要)     │
advance_group ◀──fan-in────────────┘
advance_group →(下一组)dispatch_groups / → consistency_review
consistency_review(五维 ≤2 轮)→ 定向重写回 dispatch_groups / CP2(必停)
CP2 → revise_dirty → assemble_final(reports/ 落 MD 权威源 + 记忆蒸馏)
    → render_export(HTML/PDF) → END
cost_pause:成本护栏超限的暂停闸,resume 可抬上限或中止。

约定:LLM 成本增量一律报 `cost_accrued_cny`(add_number 累加通道);
检索增量报 `search_calls_used`;章节正文进 `chapters`(merge_dict),
组间摘要进 `chapter_summaries`(规则摘录,不再花模型钱)。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from langgraph.types import Command, Send, interrupt

from deepsearch.config import get_settings
from deepsearch.graph.state import DeepSearchState
from deepsearch.memory import intercept, llm, profile
from deepsearch.tools import export, fts, search

# ---------- 提示词 ----------

_OUTLINE_SYSTEM = (
    "你是投研报告架构师。把调研结论组织成 6-10 章的深度研报大纲,"
    "章序遵循:总论/行业格局→竞争与壁垒→财务与经营→前景与风险→估值与结论。"
    "严格输出 JSON:{\"chapters\": [{\"title\": \"章标题\", \"bullets\": [\"要点\", ...]}]},"
    "不要多余文字。"
)

_WRITE_SYSTEM = (
    "你是投研团队的资深行业分析师,正在撰写中文深度研报的一章:{title}。\n"
    "要求:正文约 {words} 字;只输出 Markdown 正文本身(不要章标题/heading 标记);"
    "关键数据句末以 [n] 标注来源编号,并在章末列出编号对应 URL;"
    "只使用给定素材中的数据,材料不足处明确说明局限,禁止编造;"
    "术语与前文摘要保持一致。{style}"
)

_REVIEW_SYSTEM = (
    "你是研报一致性审稿编辑。对全文按五个维度审查:术语统一、数据一致、"
    "逻辑连贯、覆盖度(是否回应全部关键调研问题)、证据支撑(引用是否落实)。\n"
    "严格输出 JSON:{{\"dirty\": [{{\"id\": \"章节id\", \"dimensions\": [\"数据\"], "
    "\"note\": \"具体修改指令\"}}], \"summary\": \"总评\"}}。"
    "合格的章节不要列;没有问题时 dirty 为空数组。"
)

_ASSEMBLE_NOTE = (
    "已完成逐章撰写与一致性审稿。本报告由 AI 检索与生成管线产出,"
    "引用数据以来源链接为准,请以人工核验后的结论为最终依据。"
)

_DISCLAIMER = (
    "---\n\n**免责声明**:本报告由深度研报智能体系统自动生成,仅供个人研究参考,"
    "不构成任何投资建议。文中数据来源于公开检索与用户上传资料,可能存在时效或"
    "口径差异;据此操作,风险自担。"
)


# ---------- 纯函数辅助(离线可测) ----------


def _groups(outline: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [outline[i : i + size] for i in range(0, len(outline), max(size, 1))]


def _norm_outline(raw: str) -> list[dict[str, Any]]:
    """解析大纲 JSON 并分配稳定 id(c1..);非法输出抛 ValueError。"""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("大纲 JSON 解析失败")
    data = json.loads(raw[start : end + 1])
    chapters = data.get("chapters") or []
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("大纲缺少 chapters")
    out = []
    for i, ch in enumerate(chapters[: get_settings().outline_max_chapters], start=1):
        title = str(ch.get("title", "")).strip()
        if not title:
            continue
        out.append(
            {
                "id": f"c{i}",
                "title": title,
                "bullets": [str(b).strip() for b in (ch.get("bullets") or [])][:6],
            }
        )
    if not out:
        raise ValueError("大纲无有效章节")
    return out


def _rule_summary(md: str, limit: int = 240) -> str:
    """组间传摘要:规则式压缩(去标记取前段),不花模型钱。"""
    text = re.sub(r"[#*>`\[\]()|-]", " ", md)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _over_budget(state: DeepSearchState) -> bool:
    limit = state.get("cost_limit_cny") or get_settings().cost_limit_cny_per_report
    return float(state.get("cost_accrued_cny", 0.0)) >= float(limit)


def _is_rewrite_round(state: DeepSearchState) -> bool:
    """初稿齐 + 有脏章 → dispatch 进入定向重写模式。"""
    outline = state.get("outline") or []
    chapters = state.get("chapters") or {}
    return bool(outline) and len(chapters) >= len(outline) and bool(
        state.get("dirty_chapters")
    )


def _unconsumed(state: DeepSearchState) -> list[dict[str, Any]]:
    done = state.get("consumed_directives", 0)
    return list(state.get("pending_directives", []))[done:]


def _ordered_chapters(state: DeepSearchState) -> list[tuple[str, str]]:
    ch = state.get("chapters") or {}
    return [(c["id"], ch.get(c["id"], "")) for c in state.get("outline", [])]


def _materials_block(tid: str, query: str, hits: list[fts.CorpusHit]) -> str:
    docs = fts.get_documents([h.doc_id for h in hits])
    return "\n\n".join(
        f"[[{i}]] {d.title}({d.source}|{d.url})\n{(d.content or '')[:500]}"
        for i, d in enumerate(docs, start=1)
    )


def _chat_cost(messages: list[dict[str, str]], **kw) -> tuple[llm.ChatResult, float]:
    res = llm.chat(messages, **kw)
    return res, llm.estimate_cost_cny(res, kw.get("tier", "primary"))


# ---------- ⑦ outline_gen ----------


def outline_gen(state: DeepSearchState) -> dict[str, Any]:
    if state.get("outline"):  # 续跑不重复生成
        return {}
    topic = state.get("report_topic", "")
    questions = "、".join(p["question"] for p in state.get("research_plan", []))
    res, cost = _chat_cost(
        [
            {"role": "system", "content": _OUTLINE_SYSTEM},
            {
                "role": "user",
                "content": f"主题:{topic}\n关键调研问题:{questions}\n"
                f"内部数据摘要:{json.dumps(state.get('data_findings', {}), ensure_ascii=False)[:800]}",
            },
        ],
        tier="primary",
        temperature=0.3,
    )
    try:
        outline = _norm_outline(res.content)
    except Exception:
        outline = [
            {"id": "c1", "title": f"{topic}:总论与行业格局", "bullets": []},
            {"id": "c2", "title": "竞争壁垒与财务质量", "bullets": []},
            {"id": "c3", "title": "增长驱动与风险因素", "bullets": []},
            {"id": "c4", "title": "估值水位与结论", "bullets": []},
        ]
    return {
        "outline": outline,
        "group_idx": 0,
        "cost_accrued_cny": cost,
    }


# ---------- ⑧ CP1 网关(固定必停) ----------


def cp1(state: DeepSearchState) -> dict[str, Any]:
    decision = interrupt(
        {
            "gate": "CP1",
            "topic": state.get("report_topic", ""),
            "outline": state.get("outline", []),
            "conflict_notes": state.get("conflict_notes", []),
            "directives": _unconsumed(state),
            "hint": "resume {\"outline\": [...]} 可改大纲;{\"abort\": true} 终止任务",
        }
    ) or {}
    update: dict[str, Any] = {
        "consumed_directives": len(state.get("pending_directives", [])),
        "needs_human": False,
    }
    if decision.get("abort"):
        update["stop_requested"] = True
        update["answer"] = "研报任务已在 CP1 被人工终止。"
    elif decision.get("outline"):
        try:
            update["outline"] = _norm_outline(
                json.dumps({"chapters": decision["outline"]})
            )
            update["group_idx"] = 0
        except Exception:
            pass  # 人工改的大纲不合法 → 维持原稿
    return update


def route_after_cp1(state: DeepSearchState) -> str:
    return "abort" if state.get("stop_requested") else "dispatch"


# ---------- ⑨ 分组扇出 / 章写作 / 组推进 ----------


def dispatch_groups(state: DeepSearchState) -> Command:
    """返回 Command(goto=[Send]) 扇出(组内并行);超限走暂停闸。重写模式只发脏章。

    注:LangGraph 1.2.x 节点裸返 list[Send] 会被 control 通道忽略而报
    InvalidUpdateError,必须包进 Command(goto=...)。
    """
    if _over_budget(state):
        return Command(goto="cost_pause")
    settings = get_settings()
    outline = state.get("outline", [])
    topic = state.get("report_topic", "")
    budget_left = settings.web_search_budget_per_report - state.get(
        "search_calls_used", 0
    )

    if _is_rewrite_round(state):
        dirty = set(state.get("dirty_chapters", []))
        notes = state.get("chapter_directives", {})
        targets = [c for c in outline if c["id"] in dirty]
        base_digest = ""  # 重写带全文摘要背景
        summaries = state.get("chapter_summaries", {})
        base_digest = "\n".join(f"- {k}: {v}" for k, v in list(summaries.items())[:3])
    else:
        groups = _groups(outline, settings.chapters_group_size)
        idx = min(state.get("group_idx", 0), len(groups) - 1)
        targets = groups[idx]
        done = state.get("chapter_summaries", {})
        before = [c for c in outline if c["id"] in done and c not in targets]
        base_digest = "\n".join(f"- {c['id']}: {done[c['id']]}" for c in before)
        notes = {}

    return Command(
        goto=[
            Send(
                "write_chapter",
                {
                    "thread_id": state["thread_id"],
                    "topic": topic,
                    "chapter": ch,
                    "prior_digest": base_digest,
                    "instruction": notes.get(ch["id"], ""),
                    "budget_left": budget_left,
                    "plan_questions": [
                        p["question"] for p in state.get("research_plan", [])
                    ],
                },
            )
            for ch in targets
        ]
    )


def write_chapter(payload: dict[str, Any]) -> dict[str, Any]:
    """Send 扇出的工作单元:查池→限量补查→主档写章→规则摘要。"""
    settings = get_settings()
    ch: dict[str, Any] = payload["chapter"]
    tid = payload["thread_id"]
    query = ch["title"] + " " + " ".join(ch.get("bullets", []))

    hits = fts.search(tid, query, limit=6)
    searched = 0
    new_ids: list[str] = []
    while (
        len(hits) < settings.chapter_material_min_hits
        and searched < settings.chapter_supplement_searches
        and searched < payload.get("budget_left", 0)
    ):
        bundle = search.web_search(query, count=settings.search_count_per_query)
        searched += 1
        for r in bundle.results:
            doc_id = fts.index_document(
                tid, title=r.title, content=r.snippet, url=r.url, source=r.provider
            )
            new_ids.append(doc_id)
        hits = fts.search(tid, query, limit=6)

    style = intercept.injection_for_node("chapter_write", payload["topic"])
    materials = _materials_block(tid, query, hits) or "(任务池暂无素材,请声明材料局限)"
    instruction = (
        f"\n〈重写指令〉{payload['instruction']}" if payload.get("instruction") else ""
    )
    messages = [
        {
            "role": "system",
            "content": _WRITE_SYSTEM.format(
                title=ch["title"], words=settings.chapter_target_words,
                style=("\n" + style) if style else "",
            ),
        },
        {
            "role": "user",
            "content": (
                f"研报主题:{payload['topic']}\n"
                f"本章需回应的调研问题:{'、'.join(payload.get('plan_questions', []))[:400]}\n"
                f"前文摘要(保持术语与结论一致):\n{payload.get('prior_digest') or '(本章为首批)'}"
                f"{instruction}\n\n〈素材〉\n{materials}"
            ),
        },
    ]
    res, cost = _chat_cost(messages, tier="primary", temperature=0.5)
    return {
        "chapters": {ch["id"]: res.content.strip()},
        "chapter_summaries": {ch["id"]: _rule_summary(res.content)},
        "source_ids": new_ids,
        "search_calls_used": searched,
        "cost_accrued_cny": cost,
    }


def advance_group(state: DeepSearchState) -> dict[str, Any]:
    if _is_rewrite_round(state):  # 重写轮不动组游标
        return {}
    groups = _groups(state.get("outline", []), get_settings().chapters_group_size)
    return {"group_idx": min(state.get("group_idx", 0) + 1, len(groups))}


def route_after_advance(state: DeepSearchState) -> str:
    if _is_rewrite_round(state):
        return "consistency_review"
    groups = _groups(state.get("outline", []), get_settings().chapters_group_size)
    return "dispatch_groups" if state.get("group_idx", 0) < len(groups) else "consistency_review"


# ---------- ⑩ consistency_review(五维 ≤2 轮) ----------


def consistency_review(state: DeepSearchState) -> dict[str, Any]:
    full = "\n\n".join(f"## {cid}\n{md}" for cid, md in _ordered_chapters(state))
    questions = "、".join(p["question"] for p in state.get("research_plan", []))
    res, cost = _chat_cost(
        [
            {"role": "system", "content": _REVIEW_SYSTEM},
            {"role": "user", "content": f"关键调研问题:{questions}\n\n全文:\n{full[:40000]}"},
        ],
        tier="primary",
        temperature=0.2,
    )
    dirty, notes = _parse_review(res.content)
    return {
        "review_rounds": state.get("review_rounds", 0) + 1,
        "dirty_chapters": dirty,
        "chapter_directives": notes,
        "cost_accrued_cny": cost,
    }


def _parse_review(raw: str) -> tuple[list[str], dict[str, str]]:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return [], {}  # 审稿解析失败视为通过,不卡流程
    try:
        data = json.loads(raw[start : end + 1])
    except Exception:
        return [], {}
    dirty: list[str] = []
    notes: dict[str, str] = {}
    for item in data.get("dirty", []):
        cid = str(item.get("id", ""))
        if cid:
            dirty.append(cid)
            dims = "/".join(item.get("dimensions", []) or [])
            notes[cid] = f"[{dims}] {item.get('note', '')}".strip()
    return dirty, notes


def route_after_review(state: DeepSearchState) -> str:
    if _over_budget(state):
        return "cost_pause"
    if state.get("dirty_chapters") and state.get("review_rounds", 0) < get_settings().review_max_rounds:
        return "rewrite"
    return "cp2"


# ---------- ⑪ CP2 网关(固定必停) ----------


def cp2(state: DeepSearchState) -> dict[str, Any]:
    decision = interrupt(
        {
            "gate": "CP2",
            "topic": state.get("report_topic", ""),
            "chapters": dict(_ordered_chapters(state)),
            "conflict_notes": state.get("conflict_notes", []),
            "review_findings": {
                cid: state.get("chapter_directives", {}).get(cid, "")
                for cid in state.get("dirty_chapters", [])
            },
            "directives": _unconsumed(state),
            "hint": "resume {\"edits\": {章id: 修订MD}, \"resolutions\": [...], \"abort\": bool}",
        }
    ) or {}
    update: dict[str, Any] = {
        "human_edits": decision.get("edits") or {},
        "conflict_resolutions": decision.get("resolutions") or [],
        "consumed_directives": len(state.get("pending_directives", [])),
        "needs_human": False,
    }
    if decision.get("abort"):
        update["stop_requested"] = True
        update["answer"] = "研报任务已在 CP2 被人工终止。"
    return update


def route_after_cp2(state: DeepSearchState) -> str:
    return "abort" if state.get("stop_requested") else "continue"


# ---------- ⑫ revise_dirty(人工改稿优先,余脏章定向修订) ----------


def revise_dirty(state: DeepSearchState) -> dict[str, Any]:
    edits = state.get("human_edits", {})
    update: dict[str, Any] = {}
    if edits:  # 人工定稿版本直接覆盖草稿(merge_dict 右值胜出)
        update["chapters"] = dict(edits)
        update["chapter_summaries"] = {k: _rule_summary(v) for k, v in edits.items()}

    resolutions = state.get("conflict_resolutions", [])
    targets = [
        c
        for c in state.get("outline", [])
        if c["id"] in set(state.get("dirty_chapters", [])) and c["id"] not in edits
    ]
    if targets:
        # 2 轮重写后仍不合格且未被人工改稿的章节,补一轮定向修订(审稿指令+分歧定夺注入)
        notes = state.get("chapter_directives", {})
        texts: dict[str, str] = {}
        summaries: dict[str, str] = {}
        cost = 0.0
        for c in targets:
            res, delta = _chat_cost(
                [
                    {
                        "role": "system",
                        "content": _WRITE_SYSTEM.format(
                            title=c["title"],
                            words=get_settings().chapter_target_words,
                            style="",
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"原稿:\n{(state.get('chapters', {}).get(c['id'], ''))[:6000]}\n"
                            f"修改指令:{notes.get(c['id'], '')}\n"
                            f"分歧定夺(必须采用):{json.dumps(resolutions, ensure_ascii=False)[:800]}"
                        ),
                    },
                ],
                tier="primary",
                temperature=0.3,
            )
            cost += delta
            texts[c["id"]] = res.content.strip()
            summaries[c["id"]] = _rule_summary(res.content)
        update["chapters"] = {**(update.get("chapters") or {}), **texts}
        update["chapter_summaries"] = summaries
        update["cost_accrued_cny"] = cost
    update["dirty_chapters"] = []
    return update


# ---------- ⑬ assemble_final(MD 权威源落盘) ----------


def report_root(thread_id: str, topic: str) -> Path:
    settings = get_settings()
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", topic).strip("-")[:24] or "report"
    base = Path(settings.data_dir).parent / "reports"
    return base / f"{time.strftime('%Y%m%d')}-{slug}-{thread_id[:6]}"


def build_markdown(state: DeepSearchState) -> str:
    """纯拼装:标题 + 章节 + 分歧处理 + 引用 + 免责声明。无副作用,可单测。"""
    topic = state.get("report_topic", "深度研报")
    parts = [f"# {topic}", "", f"> {_ASSEMBLE_NOTE}", ""]
    for c in state.get("outline", []):
        body = (state.get("chapters", {}).get(c["id"]) or "").strip()
        bullets = "".join(f"- {b}\n" for b in c.get("bullets", []))
        parts.append(f"## {c['title']}\n")
        if bullets:
            parts.append(f"<details><summary>本章要点</summary>\n\n{bullets}\n</details>\n")
        parts.append(body or "(本章缺失)")
        parts.append("")

    conflicts = state.get("conflict_notes", [])
    resolutions = state.get("conflict_resolutions", [])
    if conflicts or resolutions:
        parts.append("## 来源分歧与处理\n")
        covered = set()
        for n in conflicts:
            covered.add(n["question"])
            match = next((x for x in resolutions if x.get("question") == n["question"]), {})
            parts.append(
                f"- **{n['question']}**:采信 {n.get('resolved_with', '?')} 口径"
                f"({n.get('resolution_basis', 'authority')});"
                + (f"人工定夺:{match.get('decision', '按系统裁决')}" if match else "未列人工定夺")
            )
        for r in resolutions:  # 无系统冲突记录但人工主动定夺过,同样落笔
            if r.get("question") not in covered:
                parts.append(f"- **{r['question']}**:人工定夺:{r.get('decision', '')}")
        parts.append("")

    docs = fts.get_documents(sorted(set(state.get("source_ids", []))))
    if docs:
        parts.append("## 引用来源\n")
        for i, d in enumerate(docs, start=1):
            parts.append(f"{i}. [{d.title}]({d.url or d.source})({d.source})")
        parts.append("")

    parts.append(_DISCLAIMER)
    return "\n".join(parts)


def assemble_final(state: DeepSearchState) -> dict[str, Any]:
    md_text = build_markdown(state)
    out_dir = report_root(state["thread_id"], state.get("report_topic", ""))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(md_text, encoding="utf-8")

    # CP2 后廉价模型蒸馏记忆候选(定稿:fact 自动生效,profile 人审);失败不阻塞
    try:
        profile.distill(md_text[:6000], "report", str(out_dir))
    except Exception:
        pass

    return {
        "report_dir": str(out_dir),
        "answer": f"研报正文已组装:{out_dir / 'report.md'}({len(md_text)} 字符)。",
    }


# ---------- ⑭ render_export ----------


def render_export(state: DeepSearchState) -> dict[str, Any]:
    out_dir = Path(state.get("report_dir") or ".")
    md_text = (out_dir / "report.md").read_text(encoding="utf-8")
    files = export.write_outputs(out_dir, md_text, state.get("report_topic", "深度研报"))
    got = [k for k, v in files.items() if v]
    return {
        "answer": state.get("answer", "")
        + f"\n导出完成:{'/'.join(got)}"
        + ("" if files["pdf"] else "\n(PDF 未生成:weasyprint 原生库不可用,HTML 可直接打印)"),
        "needs_human": False,
    }


# ---------- 成本护栏暂停闸 ----------


def cost_pause(state: DeepSearchState) -> dict[str, Any]:
    decision = interrupt(
        {
            "gate": "COST",
            "accrued_cny": round(state.get("cost_accrued_cny", 0.0), 4),
            "limit_cny": state.get("cost_limit_cny"),
            "hint": "resume {\"raise_to\": 新上限(元)} 继续;{\"abort\": true} 终止",
        }
    ) or {}
    update: dict[str, Any] = {"needs_human": False}
    if decision.get("raise_to"):
        update["cost_limit_cny"] = float(decision["raise_to"])
    if decision.get("abort"):
        update["stop_requested"] = True
        update["answer"] = "任务因成本超限被人工终止。"
    return update


def route_after_cost_pause(state: DeepSearchState) -> str:
    return "abort" if state.get("stop_requested") else "dispatch_groups"
