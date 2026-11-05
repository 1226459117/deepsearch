"""研报段(P4)离线单测:纯函数 + 图级端到端(CP1/CP2/成本闸/重写循环)。

模型/网络/记忆蒸馏全部替身;FTS 池与 reports 落盘走 tmp data_dir。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepsearch.graph.build import build_graph, interrupts_of, run_turn
from deepsearch.graph.checkpoint import memory_checkpointer
from deepsearch.graph.nodes import writing as W
from deepsearch.graph.state import new_state
from deepsearch.memory import intercept, llm, profile, service
from deepsearch.memory.llm import ChatResult
from deepsearch.tools import search

# ---------------- 纯函数 ----------------


class TestPureHelpers:
    def test_groups_chunking(self):
        items = [{"id": str(i)} for i in range(7)]
        gs = W._groups(items, 3)
        assert [len(g) for g in gs] == [3, 3, 1]

    def test_norm_outline_assigns_ids(self, settings):
        raw = '{"chapters": [{"title": "总论", "bullets": ["a", "b"]}, {"title": "风险"}]}'
        out = W._norm_outline(raw)
        assert [c["id"] for c in out] == ["c1", "c2"]
        assert out[0]["bullets"] == ["a", "b"]

    def test_norm_outline_rejects_garbage(self, settings):
        with pytest.raises(ValueError):
            W._norm_outline("不是 JSON")

    def test_rule_summary_strips_marks(self):
        s = W._rule_summary("# 标题\n\n**重点**:数据见 [1] 来源`code`")
        assert "#" not in s and "**" not in s and len(s) <= 240

    def test_is_rewrite_round(self, settings):
        outline = [{"id": "c1"}, {"id": "c2"}]
        assert W._is_rewrite_round({"outline": outline, "chapters": {"c1": "x", "c2": "y"}, "dirty_chapters": ["c1"]})
        assert not W._is_rewrite_round({"outline": outline, "chapters": {"c1": "x"}, "dirty_chapters": ["c1"]})
        assert not W._is_rewrite_round({"outline": outline, "chapters": {"c1": "x", "c2": "y"}, "dirty_chapters": []})

    def test_parse_review(self):
        raw = '{"dirty": [{"id": "c2", "dimensions": ["数据", "逻辑"], "note": "口径不一"}], "summary": "良"}'
        dirty, notes = W._parse_review(raw)
        assert dirty == ["c2"] and "数据/逻辑" in notes["c2"]
        assert W._parse_review("垃圾输出") == ([], {})

    def test_build_markdown_sections(self, settings):
        st = new_state("t9", "光伏")
        st.update(
            report_topic="光伏行业前景",
            outline=[{"id": "c1", "title": "总论", "bullets": ["格局"]}],
            chapters={"c1": "正文内容。[1]"},
            source_ids=[],
            conflict_notes=[{"question": "装机量?", "resolved_with": "d1", "resolution_basis": "authority"}],
            conflict_resolutions=[{"question": "装机量?", "decision": "采信公告"}],
        )
        md = W.build_markdown(st)
        assert md.startswith("# 光伏行业前景")
        assert "## 总论" in md and "正文内容。[1]" in md
        assert "来源分歧与处理" in md and "采信公告" in md
        assert "免责声明" in md

    def test_report_root_layout(self, settings):
        p = W.report_root("abcdef1234", "光伏行业/前景 分析")
        assert p.parent.name == "reports"
        assert p.name.startswith(str(__import__("time").strftime("%Y%m%d")))
        assert "abcdef" in p.name


# ---------------- 图级端到端 ----------------


def _outline_json(n=6):
    return json.dumps(
        {"chapters": [{"title": f"第{i}章", "bullets": [f"要点{i}"]} for i in range(1, n + 1)]}
    )


@pytest.fixture
def studio(settings, monkeypatch):
    """整图替身工作台:chat 按 system 分支,网络/蒸馏/记忆注入全 stub。

    review_rounds_seen 记录审稿调用次数,便于断言重写循环。
    """
    state = {"reviews": 0, "distilled": []}
    service_stub = {"post_turn": []}

    def cr(content: str) -> ChatResult:
        # 带真实用量,验证成本增量经 add_number 通道累加
        return ChatResult(
            content=content, prompt_tokens=1200, completion_tokens=600
        )

    def fake_chat(messages, *, tier="primary", temperature=0.3, max_tokens=None):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "入口分诊器" in system:
            return cr('{"route":"report","confidence":0.9,"topic":"光伏"}')
        if "投研规划师" in system:
            return cr('{"questions": ["行业格局?", "财务质量?"]}')
        if "报告架构师" in system:
            return cr(_outline_json())
        if "资深行业分析师" in system:
            title = system.split("撰写中文深度研报的一章:")[1].split("。")[0]
            tag = "重写" if "〈重写指令〉" in user else "初稿"
            return cr(f"{title}正文({tag}),数据见 [1]。")
        if "一致性审稿编辑" in system:
            state["reviews"] += 1
            if state["reviews"] == 1:  # 首轮打回 c2
                return cr(json.dumps({"dirty": [
                    {"id": "c2", "dimensions": ["数据"], "note": "统一口径"}], "summary": "x"}))
            return cr('{"dirty": [], "summary": "合格"}')
        return cr("")

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "chat_with_tools",
                        lambda *a, **k: ChatResult(content="不该发生"))
    monkeypatch.setattr(service, "post_turn",
                        lambda t, u, a, **k: service_stub["post_turn"].append((t, u, a)))
    monkeypatch.setattr(service, "chat_context",
                        lambda t, c, **k: [{"role": "user", "content": c}])
    monkeypatch.setattr(intercept, "injection_for_node", lambda *a, **k: "")
    monkeypatch.setattr(
        search, "web_search",
        lambda q, count=5: search.SearchBundle([], "none", False),
    )
    monkeypatch.setattr(
        profile, "distill",
        lambda text, stype, ref, **kw: state["distilled"].append(ref) or {"fact": 0, "profile": 0},
    )
    return state


def drive_to_end(graph, thread, topic, *, cp2_decision=None, cp1_decision=None):
    """CP1→(成本闸)→CP2 逐 gate 提交决策,返回终 state。"""
    out = run_turn(graph, thread, topic, route="report")
    for _ in range(6):  # 最多 CP1 / cost / CP2 三个闸,留余量
        gates = interrupts_of(out)
        if not gates:
            break
        gate = gates[0].get("gate")
        # 注意:Command(resume={}) 空 dict 会被 LangGraph 当作空 id→value 映射,
        # 什么 gate 都不恢复;"确认"决策必须传非空 dict。
        if gate == "CP1":
            out = run_turn(graph, thread, resume=cp1_decision or {"approve": True})
        elif gate == "COST":
            out = run_turn(graph, thread, resume={"raise_to": 99.0})
        elif gate == "CP2":
            out = run_turn(graph, thread, resume=cp2_decision or {"approve": True})
        else:  # pragma: no cover
            pytest.fail(f"未知 gate: {gate}")
    return out


class TestFullPipeline:
    def test_happy_path_with_rewrite_and_edits(self, settings, studio):
        graph = build_graph(memory_checkpointer())
        out = drive_to_end(
            graph, "rep-1", "光伏行业前景",
            cp2_decision={"edits": {"c1": "人工修订的首章"}, "resolutions": [
                {"question": "行业格局?", "decision": "以协会口径为准"}]},
        )
        assert interrupts_of(out) == []
        # 6 章齐;重写轮的 c2 带"重写"标记;CP2 人工稿覆盖生效
        assert len(out["chapters"]) == 6
        assert "重写" in out["chapters"]["c2"]
        assert out["chapters"]["c1"] == "人工修订的首章"
        assert studio["reviews"] == 2               # 打回一轮后再审
        assert out["review_rounds"] == 2
        assert out["dirty_chapters"] == []
        # 成本按章累加(14 次 primary 调用 > 0)
        assert out["cost_accrued_cny"] > 0
        # 落盘:reports/<date>-<slug>/report.md + html
        d = Path(out["report_dir"])
        assert (d / "report.md").is_file() and (d / "report.html").is_file()
        text = (d / "report.md").read_text(encoding="utf-8")
        assert "人工修订的首章" in text and "免责声明" in text
        assert "以协会口径为准" in text
        # CP2 后记忆蒸馏已触发
        assert studio["distilled"] == [str(d)]
        assert "导出完成" in out["answer"]

    def test_cp1_abort_ends_task(self, settings, studio):
        graph = build_graph(memory_checkpointer())
        out = run_turn(graph, "rep-2", "光伏行业前景", route="report")
        assert interrupts_of(out)[0]["gate"] == "CP1"
        out = run_turn(graph, "rep-2", resume={"abort": True})
        assert interrupts_of(out) == []
        assert "CP1 被人工终止" in out["answer"]
        assert out["chapters"] == {}  # 未进入写作

    def test_directives_consumed_at_gates(self, settings, studio):
        graph = build_graph(memory_checkpointer())
        # 研报启动前经 steering 投递指令(同 thread 累加通道),CP1 网关消费
        run_turn(graph, "rep-3", "第二章多画两张图", route="steering")
        out = run_turn(graph, "rep-3", "光伏行业前景", route="report")
        gate = interrupts_of(out)[0]
        assert gate["gate"] == "CP1"
        assert [d["text"] for d in gate["directives"]] == ["第二章多画两张图"]
        out = run_turn(graph, "rep-3", resume={"approve": True})
        assert out["consumed_directives"] == 1
        texts = [d["text"] for d in out["pending_directives"]]
        assert "第二章多画两张图" in texts

    def test_cost_guardrail_pauses_and_resumes(self, settings, studio, monkeypatch):
        # 上限压到 0.05 元:outline_gen 产生成本后 dispatch 即触发暂停闸
        settings.cost_limit_cny_per_report = 0.05
        real_chat = llm.chat

        def pricey(messages, *, tier="primary", temperature=0.3, max_tokens=None):
            r = real_chat(messages, tier=tier, temperature=temperature)
            return ChatResult(content=r.content, prompt_tokens=60_000,
                              completion_tokens=20_000, tool_calls=r.tool_calls)

        monkeypatch.setattr(llm, "chat", pricey)
        graph = build_graph(memory_checkpointer())
        out = run_turn(graph, "rep-4", "光伏行业前景", route="report")
        out = run_turn(graph, "rep-4", resume={"approve": True})  # CP1
        gates = interrupts_of(out)
        assert gates and gates[0]["gate"] == "COST"
        assert out["cost_accrued_cny"] >= 0.05
        out = run_turn(graph, "rep-4", resume={"raise_to": 99.0})  # 抬上限继续
        gates = interrupts_of(out)
        assert gates and gates[0]["gate"] == "CP2"

    def test_cp2_abort_keeps_drafts(self, settings, studio):
        graph = build_graph(memory_checkpointer())
        out = drive_to_end(graph, "rep-5", "光伏行业前景",
                           cp2_decision={"abort": True})
        assert "CP2 被人工终止" in out["answer"]
        assert len(out["chapters"]) == 6  # 草稿仍在 checkpoint,可续跑
        assert out["report_dir"] == ""    # 未装配落盘
