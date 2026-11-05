"""调研段节点离线单测:plan/data/dual_search/merge 冲突裁决/assess 回边。

网络检索与 AkShare 管线全部替身;FTS 任务池用 tmp data_dir 真实建库。
"""

from __future__ import annotations

import pytest

from deepsearch.graph.nodes import research
from deepsearch.graph.state import new_state
from deepsearch.memory.llm import ChatResult
from deepsearch.tools import fts
from deepsearch.tools.search import SearchBundle, SearchResult


def make_state(topic="分析宁德时代 300750 的前景", **overrides):
    st = dict(new_state("task-r", topic))
    st["report_topic"] = topic
    st.update(overrides)
    return st


def web_result(i):
    return SearchResult(
        title=f"来源{i}", url=f"https://site{i}.com/a",
        snippet=f"摘要{i}", site=f"site{i}", published="", provider="bocha",
    )


# ---------------- plan_queries ----------------


class TestPlanQueries:
    def test_parses_llm_json(self, settings, monkeypatch):
        monkeypatch.setattr(
            research.llm, "chat",
            lambda *a, **k: ChatResult(
                content='{"questions": ["行业格局?", "财务质量?", "风险?"]}'
            ),
        )
        out = research.plan_queries(make_state())
        assert [p["question"] for p in out["research_plan"]] == [
            "行业格局?", "财务质量?", "风险?",
        ]
        assert out["research_plan"][0]["id"] == "q1"

    def test_model_failure_falls_back_to_topic(self, settings, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(research.llm, "chat", boom)
        out = research.plan_queries(make_state())
        assert len(out["research_plan"]) == 1
        assert out["research_plan"][0]["question"] == make_state()["report_topic"]

    def test_reentry_keeps_existing_plan(self, settings, monkeypatch):
        monkeypatch.setattr(
            research.llm, "chat",
            lambda *a, **k: pytest.fail("续跑不应再规划"),  # pragma: no cover
        )
        plan = [{"id": "q1", "question": "已有问题"}]
        assert research.plan_queries(make_state(research_plan=plan)) == {}


# ---------------- data_pipeline ----------------


class TestDataPipeline:
    def test_skipped_marks_findings(self, settings, monkeypatch):
        monkeypatch.setattr(
            research.akshare_data, "run_data_pipeline",
            lambda t, x: {"status": "skipped", "symbol": None},
        )
        out = research.data_pipeline(make_state())
        assert out["data_findings"]["data_pipeline_none"]["status"] == "skipped"

    def test_metrics_indexed_into_pool(self, settings, monkeypatch):
        monkeypatch.setattr(
            research.akshare_data, "run_data_pipeline",
            lambda t, x: {
                "status": "ok", "symbol": "300750",
                "metrics": {"net_margin": 15.0}, "files": {}, "rows": {},
            },
        )
        out = research.data_pipeline(make_state())
        ids = out["data_findings"]["_source_ids"]
        docs = fts.get_documents(ids)
        assert docs and docs[0].source == "akshare"


# ---------------- dual_search ----------------


class TestDualSearch:
    plan = [
        {"id": "q1", "question": "行业格局"},
        {"id": "q2", "question": "财务质量"},
    ]

    def test_indexes_results_and_counts(self, settings, monkeypatch):
        monkeypatch.setattr(
            research.search, "web_search",
            lambda q, count=5: SearchBundle([web_result(1)], "bocha", False),
        )
        st = make_state(research_plan=self.plan)
        out = research.dual_search(st)
        assert len(out["source_ids"]) == 2
        assert out["search_calls_used"] == 2
        # 素材原文确实落池,state 只存 ID
        assert len(fts.search(st["thread_id"], "行业格局")) == 1

    def test_budget_gate(self, settings, monkeypatch):
        settings.web_search_budget_per_report = 3
        asked = []
        monkeypatch.setattr(
            research.search, "web_search",
            lambda q, count=5: asked.append(q) or SearchBundle([], "none", False),
        )
        st = make_state(research_plan=self.plan, search_calls_used=2)  # 仅剩 1 次
        research.dual_search(st)
        assert asked == ["行业格局"]  # 第二题超预算,不再发网络请求

    def test_supplement_round_only_hits_gaps(self, settings, monkeypatch):
        asked = []
        monkeypatch.setattr(
            research.search, "web_search",
            lambda q, count=5: asked.append(q) or SearchBundle([], "none", False),
        )
        st = make_state(research_plan=self.plan, coverage_gaps=["财务质量"])
        research.dual_search(st)
        assert asked == ["财务质量"]


# ---------------- merge_align 冲突三级裁决 ----------------


def corpus_doc(doc_id, source, content, url=""):
    return fts.CorpusDoc(
        doc_id=doc_id, title=content[:10], url=url, content=content, source=source
    )


class TestArbitrate:
    def test_no_conflict_when_agree(self):
        docs = [
            corpus_doc("d1", "bocha", "营收增速 12%"),
            corpus_doc("d2", "tavily", "增速达 12%"),
        ]
        assert research._arbitrate("q", docs) is None

    def test_single_source_no_conflict(self):
        docs = [corpus_doc("d1", "bocha", "营收 300 亿元")]
        assert research._arbitrate("q", docs) is None

    def test_authority_wins(self):
        docs = [
            corpus_doc("d1", "bocha", "营收增速 12%"),
            corpus_doc("d2", "akshare", "营收增速 9%"),
        ]
        c = research._arbitrate("增长多少", docs)
        assert c["resolved_with"] == "d2"
        assert c["resolution_basis"] == "authority"
        assert c["needs_human"] is False

    def test_tie_escalates_human(self):
        docs = [
            corpus_doc("d1", "bocha", "营收增速 12%"),
            corpus_doc("d2", "tavily", "营收增速 8%"),
        ]
        c = research._arbitrate("增长多少", docs)
        assert c["needs_human"] is True  # 同层级并列 → 分歧清单人工兜底

    def test_authoritative_domain_boosts(self):
        docs = [
            corpus_doc("d1", "bocha", "净利 20%", url="https://sse.com.cn/x"),
            corpus_doc("d2", "tavily", "净利 18%"),
        ]
        c = research._arbitrate("q", docs)
        assert c["resolved_with"] == "d1"  # 交易所披露域名加权(+2)后反超同级


class TestMergeAlignNode:
    def test_conflicts_surface(self, settings, monkeypatch):
        st = make_state(research_plan=[{"id": "q1", "question": "增速"}])
        fts.index_document(st["thread_id"], title="A", content="增速 12%", source="bocha")
        fts.index_document(st["thread_id"], title="B", content="增速 8%", source="tavily")
        out = research.merge_align(st)
        assert len(out["conflict_notes"]) == 1
        assert out["conflict_notes"][0]["question"] == "增速"


# ---------------- assess_coverage 缺口判定与回边 ----------------


class TestAssessCoverage:
    plan = [{"id": "q1", "question": "有材的问题"}, {"id": "q2", "question": "无材的问题"}]

    def test_gap_triggers_supplement_round(self, settings):
        st = make_state(research_plan=self.plan)
        fts.index_document(st["thread_id"], title="材", content="有材的问题 答案", source="bocha")
        out = research.assess_coverage(st)
        assert out["coverage_gaps"] == ["无材的问题"]
        assert out["coverage_rounds"] == 1
        assert research.assess_coverage_route(out) == "supplement"

    def test_round_cap_closes_subgraph(self, settings):
        st = make_state(research_plan=self.plan, coverage_rounds=2)
        out = research.assess_coverage(st)
        assert "answer" in out  # 达上限即收口出摘要
        assert research.assess_coverage_route(
            {**st, **out}
        ) == "done"

    def test_full_coverage_digest_no_network(self, settings):
        st = make_state(research_plan=[{"id": "q1", "question": "问题一"}])
        fts.index_document(st["thread_id"], title="t", content="问题一 有答案", source="bocha")
        out = research.assess_coverage(st)
        assert out["coverage_gaps"] == []
        assert "调研段完成" in out["answer"]
        assert out["needs_human"] is False
