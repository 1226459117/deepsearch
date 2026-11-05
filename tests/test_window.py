"""window.py 离线单测:30 轮窗口、绝对轮号基准、滚动摘要压缩。"""

from __future__ import annotations

import pytest

from deepsearch.memory.window import ConversationWindow
from tests.fakes import FakeRedis


@pytest.fixture
def win(settings):
    return ConversationWindow("t1", client=FakeRedis())


def push_n(win, n, prefix="内容"):
    for i in range(1, n + 1):
        win.push_turn(f"{prefix}-q{i}", f"{prefix}-a{i}")


def fake_compressor(prev, turns):
    joined = f"{prev}|" if prev else ""
    return f"{joined}摘要[{len(turns)}轮:{turns[0]['user']}..{turns[-1]['assistant']}]"


class TestWindowBasics:
    def test_push_returns_sequential_numbers(self, win):
        assert win.push_turn("q1", "a1") == 1
        assert win.push_turn("q2", "a2") == 2

    def test_window_grows_until_compaction(self, win):
        push_n(win, 35)
        # push 阶段不截断:未摘要原文绝不从窗口失踪
        assert win.turn_count() == 35

    def test_absolute_turn_no(self, win):
        push_n(win, 35)
        assert win._base() == 1
        assert win.push_turn("q36", "a36") == 36

    def test_key_schema(self, win):
        win.push_turn("q", "a")
        client = win._r
        key = next(k for k in client.lists)
        assert key.startswith("dp:default:owner:t1:")

    def test_build_context_order(self, win):
        win.set_summary("旧摘要")
        push_n(win, 2)
        ctx = win.build_context()
        assert ctx[0]["role"] == "system" and "旧摘要" in ctx[0]["content"]
        assert ctx[1]["content"] == "内容-q1"
        assert ctx[-1]["content"] == "内容-a2"

    def test_drop_clears_all_keys(self, win):
        push_n(win, 3)
        win.set_summary("s")
        win.drop()
        assert win.turn_count() == 0 and win.get_summary() == ""


class TestCompaction:
    def test_not_triggered_below_threshold(self, win):
        push_n(win, 29)
        assert not win.needs_compaction()
        assert win.compact(compressor=fake_compressor) is None

    def test_trigger_at_30_keeps_10(self, win):
        push_n(win, 30)
        result = win.compact(compressor=fake_compressor)
        assert result is not None
        assert win.turn_count() == 10
        assert result["from_turn"] == 1 and result["to_turn"] == 20
        assert "摘要[20轮" in win.get_summary()

    def test_rolling_chain_second_round(self, win):
        push_n(win, 30)
        win.compact(compressor=fake_compressor)
        push_n(win, 20, prefix="新内容")  # 窗口 10+20=30,再次满
        assert win.turn_count() == 30
        result = win.compact(compressor=fake_compressor)
        assert result["from_turn"] == 21 and result["to_turn"] == 40
        # 滚动链:第二次摘要以第一次为背景续写;批次=绝对轮 21–40(10 轮旧内容+10 轮新内容)
        assert "摘要[20轮:内容-q1" in win.get_summary()
        assert "内容-q21" in win.get_summary()

    def test_on_stored_callback(self, win):
        push_n(win, 30)
        stored = []
        win.compact(
            compressor=fake_compressor,
            on_stored=lambda s, f, t: stored.append((f, t)),
        )
        assert stored == [(1, 20)]

    def test_failed_compression_keeps_turns(self, win):
        push_n(win, 30)
        result = win.compact(compressor=lambda prev, turns: "")
        assert result is None
        assert win.turn_count() == 30  # 摘要失败绝不删原文

    def test_payload_is_json_roundtrip(self, win):
        win.push_turn("含中文\"引号\"", "多\n行")
        turn = win.get_turns()[0]
        assert turn == {"user": "含中文\"引号\"", "assistant": "多\n行"}
