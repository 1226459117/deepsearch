"""自主搜索 tool-loop(chat / factual 档,定稿 Q36)。

给主档模型挂两个工具:web_search(网络检索)、get_financial(AkShare 速查),
由模型自主决定是否调用;上层按档位封顶额度——chat ≤1 次搜索、factual ≤2 搜索
+1 财务——超额的调用直接回"额度用尽",逼模型基于已有信息收敛作答。

永不抛穿:单次工具异常转成一条 tool 结果文本喂回模型;步数耗尽则去掉工具
强制出最终答案。返回 (最终回答, 实际工具调用次数)。
"""

from __future__ import annotations

import json
from typing import Any

from deepsearch.config import get_settings
from deepsearch.memory import llm
from deepsearch.tools import akshare_data, search

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "检索公开网络信息,返回若干条带标题/链接/摘要的结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "count": {"type": "integer", "description": "返回条数,默认 5"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_financial",
            "description": "查询 A 股上市公司基础财务指标(需 6 位股票代码)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位 A 股代码,如 600519"},
                },
                "required": ["symbol"],
            },
        },
    },
]


def run_tool_loop(
    messages: list[dict[str, Any]],
    *,
    max_search: int,
    max_akshare: int,
    tier: str = "primary",
) -> tuple[str, int]:
    """执行带预算封顶的 tool-loop,返回 (answer, tool_calls_used)。"""
    settings = get_settings()
    work = list(messages)
    used_search = used_ak = 0

    for _ in range(settings.tool_loop_max_steps):
        resp = llm.chat_with_tools(work, TOOL_SPECS, tier=tier)
        if not resp.tool_calls:
            return resp.content.strip(), used_search + used_ak

        work.append(
            {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {"id": tc["id"], "type": "function", "function": {
                        "name": tc["name"], "arguments": tc["arguments"]}}
                    for tc in resp.tool_calls
                ],
            }
        )
        for tc in resp.tool_calls:
            payload = _safe_args(tc["arguments"])
            if tc["name"] == "web_search":
                if used_search >= max_search:
                    result = "检索额度已用尽,请基于已有信息作答。"
                else:
                    bundle = search.web_search(
                        str(payload.get("query", "")),
                        count=int(payload.get("count") or settings.search_count_per_query),
                    )
                    used_search += 1
                    result = _fmt_results(bundle)
            elif tc["name"] == "get_financial":
                if used_ak >= max_akshare:
                    result = "财务查询额度已用尽。"
                else:
                    used_ak += 1
                    result = json.dumps(
                        akshare_data.quick_metrics(str(payload.get("symbol", ""))),
                        ensure_ascii=False,
                    )
            else:
                result = f"未知工具: {tc['name']}"
            work.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    # 步数耗尽:去掉工具,强制模型收敛出最终答案
    final = llm.chat(work, tier=tier)
    return final.content.strip(), used_search + used_ak


def _safe_args(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def _fmt_results(bundle: search.SearchBundle) -> str:
    if not bundle.results:
        return json.dumps({"provider": bundle.provider_used, "results": []}, ensure_ascii=False)
    slim = [
        {"title": r.title, "url": r.url, "snippet": r.snippet[:300], "site": r.domain}
        for r in bundle.results
    ]
    return json.dumps(
        {"provider": bundle.provider_used, "fell_back": bundle.fell_back, "results": slim},
        ensure_ascii=False,
    )
