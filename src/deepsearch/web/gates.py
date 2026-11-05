"""网关表单 → resume 决策解析(纯函数,离线可测)。

三类必停网关(定稿 Q26)的 HTML 表单字段约定:
- 通用:action ∈ {approve, abort, submit}
- CP1 :outline_json(编辑后大纲,JSON list 或 {"chapters": [...]})
- CP2 :edit_<章id>(逐章改稿 textarea,与原稿相同则忽略)、
        resolutions_json(分歧人工定夺,JSON list)
- COST:raise_to(新成本上限,元)

产出决策恒为非空 dict —— P4 实测坑:`Command(resume={})` 空 dict 会被
LangGraph 当成空 id→value 映射,什么网关都不恢复。
"""

from __future__ import annotations

import json
from typing import Any


def _loads_json_field(raw: str) -> Any:
    """宽容解析 JSON 文本框:空串→None;非法→抛 ValueError。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    return json.loads(raw)


def parse_outline_form(raw: str) -> list[dict[str, Any]] | None:
    """大纲文本 → [{title, bullets}];写回 CP1 决策的 outline 键。"""
    data = _loads_json_field(raw)
    if data is None:
        return None
    if isinstance(data, dict):
        data = data.get("chapters") or data.get("outline")
    if not isinstance(data, list) or not data:
        raise ValueError("大纲必须是章节列表")
    out = []
    for i, ch in enumerate(data, start=1):
        if isinstance(ch, str):
            out.append({"title": ch, "bullets": []})
        elif isinstance(ch, dict) and str(ch.get("title", "")).strip():
            out.append(
                {
                    "title": str(ch["title"]).strip(),
                    "bullets": [str(b) for b in (ch.get("bullets") or [])],
                }
            )
        else:
            raise ValueError(f"第 {i} 章缺 title")
    return out


def parse_resolutions_form(raw: str) -> list[dict[str, str]]:
    """分歧定夺文本:JSON list[dict],或每行 `问题 | 决定`。"""
    data = _loads_json_field(raw)
    if data is None:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("question"):
            out.append({"question": str(item["question"]),
                        "decision": str(item.get("decision", ""))})
        else:
            raise ValueError("定夺条目必须含 question")
    return out


def parse_gate_decision(gate: dict[str, Any], form: dict[str, str]) -> dict[str, Any]:
    """按 gate 类型把表单字段翻译成 resume 决策。表单非法字段抛 ValueError。"""
    name = (gate or {}).get("gate", "")
    action = (form.get("action") or "approve").strip()
    if action == "abort":
        return {"abort": True}

    if name == "CP1":
        outline = parse_outline_form(form.get("outline_json", ""))
        return {"outline": outline} if outline else {"approve": True}

    if name == "CP2":
        decision: dict[str, Any] = {}
        edits: dict[str, str] = {}
        chapters = gate.get("chapters") or {}
        originals = chapters if isinstance(chapters, dict) else dict(chapters)
        for key, val in form.items():
            if key.startswith("edit_") and (val or "").strip():
                cid = key[len("edit_"):]
                if (val or "").strip() != (originals.get(cid) or "").strip():
                    edits[cid] = val.strip()
        if edits:
            decision["edits"] = edits
        resolutions = parse_resolutions_form(form.get("resolutions_json", ""))
        if resolutions:
            decision["resolutions"] = resolutions
        return decision or {"approve": True}

    if name == "COST":
        raw = (form.get("raise_to") or "").strip()
        if raw:
            try:
                return {"raise_to": float(raw)}
            except ValueError:
                raise ValueError(f"新上限必须是数字,收到:{raw!r}") from None
        # 不抬上限又没中止 → 视为按原上限继续(仍必须非空 dict)
        return {"approve": True}

    raise ValueError(f"未知网关:{name!r}")
