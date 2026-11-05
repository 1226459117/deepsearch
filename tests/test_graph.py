"""主图离线单测:装配、四档分诊、checkpoint 累加续跑。

外部依赖(llm / memory.service / intercept)全部替身,不碰真实存储与模型。
"""

from __future__ import annotations

import pytest

from deepsearch.graph.build import build_graph, run_turn
from deepsearch.graph.checkpoint import memory_checkpointer
from deepsearch.memory import intercept, llm, service
from deepsearch.memory.llm import ChatResult


@pytest.fixture
def recorder(monkeypatch):
    """stub 掉模型与记忆门面,记录调用;utility 返回分诊 JSON,primary 返回答。"""
    calls: dict[str, list] = {"post_turn": [], "chat": []}

    def fake_chat(messages, *, tier="primary", temperature=0.3, max_tokens=None):
        calls["chat"].append({"tier": tier, "messages": messages})
        if tier == "utility":  # 分诊:固定判 chat 高置信
            return ChatResult(content='{"route":"chat","confidence":0.9,"topic":""}')
        return ChatResult(content="【主档回答】你好")

    def fake_post_turn(thread_id, user, assistant, **kw):
        calls["post_turn"].append((thread_id, user, assistant))
        return {"turn_no": 1, "window_rounds": 1, "compacted": None}

    def fake_context(thread_id, current, **kw):
        return [{"role": "user", "content": current}]

    def fake_chat_with_tools(messages, tools, *, tier="primary", temperature=0.3):
        # tool-loop 替身:不挂工具,一轮直接出最终答案
        calls["chat"].append({"tier": tier, "messages": messages})
        return ChatResult(content="【主档回答】你好", tool_calls=None)

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "chat_with_tools", fake_chat_with_tools)
    monkeypatch.setattr(service, "post_turn", fake_post_turn)
    monkeypatch.setattr(service, "chat_context", fake_context)
    monkeypatch.setattr(intercept, "injection_for_node", lambda *a, **k: "")
    return calls


# ---- 分诊节点(纯函数) ----


class TestTriageNode:
    def test_respects_preset_route_without_calling_model(self, settings, recorder):
        from deepsearch.graph.nodes.triage import triage_node

        out = triage_node({"route": "report", "input": "做宁德时代研报"})
        assert out == {"route": "report", "needs_human": False}
        assert recorder["chat"] == []  # 人工改档不触发模型

    def test_low_confidence_flags_human(self, settings, monkeypatch):
        from deepsearch.graph.nodes.triage import triage_node

        monkeypatch.setattr(
            llm,
            "chat",
            lambda *a, **k: ChatResult(
                content='{"route":"factual","confidence":0.3,"topic":""}'
            ),
        )
        monkeypatch.setattr(intercept, "injection_for_node", lambda *a, **k: "")
        out = triage_node({"route": "", "input": "那个大概怎么样"})
        assert out["route"] == "factual"
        assert out["needs_human"] is True

    def test_model_failure_falls_back_to_chat(self, settings, monkeypatch):
        from deepsearch.graph.nodes.triage import triage_node

        def boom(*a, **k):
            raise RuntimeError("model down")

        monkeypatch.setattr(llm, "chat", boom)
        monkeypatch.setattr(intercept, "injection_for_node", lambda *a, **k: "")
        out = triage_node({"route": "", "input": "hi"})
        assert out["route"] == "chat" and out["needs_human"] is True


# ---- 端到端:经装配好的图 ----


class TestGraphEndToEnd:
    def test_chat_route_full_chain(self, settings, recorder):
        graph = build_graph(memory_checkpointer())
        out = run_turn(graph, "t-chat", "你好啊")
        assert out["route"] == "chat"
        assert out["answer"] == "【主档回答】你好"
        # 落库经门面:post_turn 被调用一次(Redis+PG 的唯一写路径)
        assert recorder["post_turn"] == [("t-chat", "你好啊", "【主档回答】你好")]

    def test_report_route_via_override(self, settings, recorder):
        graph = build_graph(memory_checkpointer())
        out = run_turn(graph, "t-rpt", "分析光伏行业前景", route="report")
        assert out["route"] == "report"
        assert "光伏行业前景" in out["answer"] or out["report_topic"]

    def test_illegal_preset_route_falls_back_to_model(self, settings, recorder):
        # 非法 route 不会被 triage 当作人工改档放行,回落模型分诊→chat
        graph = build_graph(memory_checkpointer())
        out = run_turn(graph, "t-x", "随便说点喓", route="bogus")
        assert out["route"] == "chat"
        assert out["answer"] == "【主档回答】你好"


class TestCheckpointAccumulation:
    def test_steering_directives_accumulate_across_invokes(self, settings, recorder):
        graph = build_graph(memory_checkpointer())
        run_turn(graph, "task-1", "把第二章多画两张图", route="steering")
        out = run_turn(graph, "task-1", "重点看下海外营收占比", route="steering")
        texts = [d["text"] for d in out["pending_directives"]]
        assert texts == ["把第二章多画两张图", "重点看下海外营收占比"]

    def test_stop_keyword_sets_escape_hatch(self, settings, recorder):
        graph = build_graph(memory_checkpointer())
        out = run_turn(graph, "task-2", "先暂停一下", route="steering")
        assert out["stop_requested"] is True
        assert out["pending_directives"][0]["is_stop"] is True

    def test_isolated_threads_do_not_share_state(self, settings, recorder):
        graph = build_graph(memory_checkpointer())
        run_turn(graph, "taskA", "指令A", route="steering")
        out_b = run_turn(graph, "taskB", "指令B", route="steering")
        assert [d["text"] for d in out_b["pending_directives"]] == ["指令B"]
