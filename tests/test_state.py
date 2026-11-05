"""state schema 单测:reducer 语义 + 初始 state 完整性。"""

from __future__ import annotations

from deepsearch.graph.state import (
    add_channel,
    last_value,
    merge_dict,
    new_state,
)


class TestReducers:
    def test_add_channel_concatenates(self):
        assert add_channel([1, 2], [3]) == [1, 2, 3]
        assert add_channel([], ["a"]) == ["a"]

    def test_merge_dict_overwrites_same_key(self):
        left = {"c1": "旧稿", "c2": "保留"}
        right = {"c1": "新稿", "c3": "新增"}
        assert merge_dict(left, right) == {
            "c1": "新稿",
            "c2": "保留",
            "c3": "新增",
        }

    def test_last_value_takes_right(self):
        assert last_value(0.5, 3.2) == 3.2
        assert last_value(["a"], ["b"]) == ["b"]


class TestNewState:
    def test_accumulator_channels_start_empty(self, settings):
        st = new_state("t1", "你好")
        assert st["pending_directives"] == []
        assert st["source_ids"] == []
        assert st["research_plan"] == []
        assert st["chapters"] == {}

    def test_identity_defaults_from_settings(self, settings):
        st = new_state("t1", "q", tenant_id="acme", user_id="u9")
        assert st["tenant_id"] == "acme" and st["user_id"] == "u9"
        assert st["thread_id"] == "t1" and st["input"] == "q"

    def test_route_blank_until_triage(self, settings):
        st = new_state("t1", "q")
        assert st["route"] == "" and st["needs_human"] is False
