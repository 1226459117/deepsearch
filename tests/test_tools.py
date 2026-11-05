"""tools 层离线单测:fts 任务池 / search 降级边 / akshare 指标 / agent tool-loop。

网络与模型全部替身:search 只 stub 底层请求函数,akshare 只喂构造 DataFrame,
tool-loop 只 stub llm.chat_with_tools 与工具实现。
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest

from deepsearch.memory.llm import ChatResult
from deepsearch.tools import agent_search, akshare_data, fts, search
from deepsearch.tools.search import SearchBundle, SearchResult


# ---------------- fts.py ----------------


class TestFts:
    def test_tokenizer_picked(self, settings):
        tok = fts.ensure_schema()
        assert tok in fts._TOKENIZER_CANDIDATES

    def test_index_search_roundtrip(self, settings):
        doc_id = fts.index_document(
            "task1", title="宁德时代 2025 年报", content="营收 3000 亿元,增长 12%",
            url="http://x", source="private_pdf",
        )
        hits = fts.search("task1", "营收")
        assert [h.doc_id for h in hits] == [doc_id]
        docs = fts.get_documents([doc_id])
        assert docs[0].content.startswith("营收")
        assert docs[0].source == "private_pdf"

    def test_task_isolation(self, settings):
        fts.index_document("taskA", title="A 材料", content="alpha 内容")
        assert fts.search("taskB", "alpha") == []

    def test_bad_query_never_raises(self, settings):
        fts.index_document("taskC", title="t", content="c")
        result = fts.search("taskC", '"""')  # 非法 MATCH 语法必须吞成空结果
        assert isinstance(result, list)


# ---------------- search.py 降级边 ----------------


def ok_result(provider="bocha"):
    return SearchResult(
        title="标题", url="https://sse.com.cn/x", snippet="摘要", site="sse",
        published="2026-01-01", provider=provider,
    )


class TestSearchFallback:
    def test_bocha_hit(self, settings, monkeypatch):
        settings.bocha_api_key = "bk"
        monkeypatch.setattr(search, "_bocha_request", lambda p, h: {"data": {"webPages": {"value": [
            {"name": "t", "url": "https://a.com", "summary": "s"}]}}})
        bundle = search.web_search("q")
        assert bundle.provider_used == "bocha" and not bundle.fell_back
        assert bundle.results[0].domain == "a.com"

    def test_bocha_error_falls_to_tavily(self, settings, monkeypatch):
        settings.bocha_api_key = "bk"
        settings.tavily_api_key = "tk"

        def boom(*a, **k):
            raise RuntimeError("bocha down")

        monkeypatch.setattr(search, "_bocha_request", boom)
        monkeypatch.setattr(search, "_tavily_request", lambda p: {"results": [
            {"title": "t2", "url": "https://b.com", "content": "s2"}]})
        bundle = search.web_search("q")
        assert bundle.provider_used == "tavily" and bundle.fell_back

    def test_both_down_returns_empty_bundle(self, settings, monkeypatch):
        settings.bocha_api_key = "bk"
        settings.tavily_api_key = "tk"

        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(search, "_bocha_request", boom)
        monkeypatch.setattr(search, "_tavily_request", boom)
        bundle = search.web_search("q")
        assert bundle.provider_used == "none" and bundle.results == []

    def test_no_keys_no_network(self, settings, monkeypatch):
        # FakeSettings 双 key 皆空:不发请求,直接空束
        def never(*a, **k):  # pragma: no cover
            raise AssertionError("不应发起网络请求")

        monkeypatch.setattr(search, "_bocha_request", never)
        monkeypatch.setattr(search, "_tavily_request", never)
        assert search.web_search("q").provider_used == "none"

    def test_bocha_retries_then_reraises(self, settings, monkeypatch):
        settings.dual_search_retry = 3
        attempts = []

        def flaky(*a, **k):
            attempts.append(1)
            raise httpx.ConnectError("net down")

        monkeypatch.setattr(search.httpx, "post", flaky)
        with pytest.raises(httpx.HTTPError):
            search._bocha_request({}, {})
        assert len(attempts) == 3  # tenacity 按配置重试到上限


# ---------------- akshare_data.py 纯计算 ----------------


def income_df():
    return pd.DataFrame({
        "营业总收入": [100.0, 120.0],
        "净利润": [10.0, 18.0],
    })


def balance_df():
    return pd.DataFrame({
        "资产总计": [300.0, 360.0],
        "负债总计": [150.0, 180.0],
    })


class TestAksharePure:
    def test_market_symbol_prefix_mapping(self):
        assert akshare_data.market_symbol("600519") == "SH600519"
        assert akshare_data.market_symbol("000001") == "SZ000001"
        assert akshare_data.market_symbol("300750") == "SZ300750"
        assert akshare_data.market_symbol("688981") == "SH688981"
        assert akshare_data.market_symbol("832000") == "BJ832000"
        assert akshare_data.market_symbol("SH600519") == "SH600519"

    def test_extract_symbol(self):
        assert akshare_data.extract_symbol("分析 600519 茅台") == "600519"
        assert akshare_data.extract_symbol("光伏行业前景") is None

    def test_compute_indicators(self):
        f = akshare_data.compute_indicators(
            {"income": income_df(), "balance": balance_df()}
        )
        assert f["latest_revenue"] == 120.0
        assert f["net_margin"] == 15.0            # 18/120
        assert f["revenue_yoy_pct"] == 20.0       # (120-100)/100
        assert f["debt_ratio_pct"] == 50.0        # 180/360

    def test_descending_input_uses_latest_period(self):
        # 东财真实顺序:最新报告期在前——按 REPORT_DATE 重排后仍应取 2025 年值
        df = pd.DataFrame({
            "REPORT_DATE": ["2025-12-31", "2024-12-31"],
            "TOTAL_OPERATE_INCOME": [150.0, 100.0],
            "PARENT_NETPROFIT": [30.0, 10.0],
        })
        f = akshare_data.compute_indicators({"income": df})
        assert f["latest_revenue"] == 150.0
        assert f["revenue_yoy_pct"] == 50.0
        assert f["net_margin"] == 20.0

    def test_compute_empty_statements(self):
        assert akshare_data.compute_indicators({}) == {"symbol_metrics": {}}

    def test_pipeline_skips_without_symbol(self, settings):
        out = akshare_data.run_data_pipeline("t1", "分析光伏行业")
        assert out["status"] == "skipped"

    def test_save_parquet_roundtrip(self, settings, tmp_path):
        paths = akshare_data.save_parquet("t2", "600519", {"income": income_df()})
        df = pd.read_parquet(paths["income"])
        assert df.shape == (2, 2)
        assert str(tmp_path) in paths["income"]  # 落在 tmp data_dir 的 reports 下


# ---------------- agent_search.run_tool_loop ----------------


def tool_call(name, args, cid="c1"):
    return ChatResult(
        content="",
        tool_calls=[{"id": cid, "name": name, "arguments": args}],
    )


class TestToolLoop:
    def test_no_tool_direct_answer(self, settings, monkeypatch):
        monkeypatch.setattr(
            agent_search.llm, "chat_with_tools",
            lambda m, t, **k: ChatResult(content="直接回答", tool_calls=None),
        )
        answer, used = agent_search.run_tool_loop(
            [{"role": "user", "content": "hi"}], max_search=1, max_akshare=0
        )
        assert (answer, used) == ("直接回答", 0)

    def test_search_then_answer(self, settings, monkeypatch):
        seq = [
            tool_call("web_search", '{"query": "宁德时代营收"}'),
            ChatResult(content="据查为 3000 亿", tool_calls=None),
        ]
        monkeypatch.setattr(agent_search.llm, "chat_with_tools",
                            lambda m, t, **k: seq.pop(0))
        bundle = SearchBundle([ok_result()], "bocha", False)
        seen = []

        def fake_web(q, count=5):
            seen.append(q)
            return bundle

        monkeypatch.setattr(agent_search.search, "web_search", fake_web)
        answer, used = agent_search.run_tool_loop(
            [{"role": "user", "content": "q"}], max_search=1, max_akshare=0
        )
        assert answer == "据查为 3000 亿" and used == 1
        assert seen == ["宁德时代营收"]

    def test_budget_exhausted_feeds_hint_not_network(self, settings, monkeypatch):
        seq = [
            tool_call("web_search", '{"query": "一"}', "c1"),
            tool_call("web_search", '{"query": "二"}', "c2"),
            ChatResult(content="终", tool_calls=None),
        ]
        monkeypatch.setattr(agent_search.llm, "chat_with_tools",
                            lambda m, t, **k: seq.pop(0))
        calls = []
        monkeypatch.setattr(
            agent_search.search, "web_search",
            lambda q, count=5: calls.append(q) or SearchBundle([], "none", False),
        )
        answer, used = agent_search.run_tool_loop(
            [{"role": "user", "content": "q"}], max_search=1, max_akshare=0
        )
        assert calls == ["一"]      # 第二次调用被额度闸门拦下,未发网络请求
        assert used == 1 and answer == "终"

    def test_unknown_tool_and_json_garbage_safe(self, settings, monkeypatch):
        seq = [
            tool_call("hack_something", "not-json"),
            ChatResult(content="ok", tool_calls=None),
        ]
        monkeypatch.setattr(agent_search.llm, "chat_with_tools",
                            lambda m, t, **k: seq.pop(0))
        answer, used = agent_search.run_tool_loop(
            [{"role": "user", "content": "q"}], max_search=2, max_akshare=1
        )
        assert (answer, used) == ("ok", 0)  # 未知工具不计额度

    def test_step_exhaustion_forces_final(self, settings, monkeypatch):
        settings.tool_loop_max_steps = 2
        monkeypatch.setattr(
            agent_search.llm, "chat_with_tools",
            lambda m, t, **k: tool_call("web_search", '{"query": "x"}'),
        )
        monkeypatch.setattr(
            agent_search.search, "web_search",
            lambda q, count=5: SearchBundle([], "none", False),
        )
        forced = []

        def fake_chat(messages, **kw):
            forced.append(len(messages))
            return ChatResult(content="强制收敛")

        monkeypatch.setattr(agent_search.llm, "chat", fake_chat)
        answer, used = agent_search.run_tool_loop(
            [{"role": "user", "content": "q"}], max_search=9, max_akshare=9
        )
        assert answer == "强制收敛" and forced
