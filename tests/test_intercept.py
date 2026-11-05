"""intercept.py 离线单测:注入块组装、token 预算裁剪、节点白名单。"""

from __future__ import annotations

import pytest

from deepsearch.memory import intercept
from deepsearch.memory.intercept import MemoryContext, build_injection
from deepsearch.memory.vectors import VectorHit


def hit(text, score, kind="fact"):
    return VectorHit(id=text[:4], kind=kind, text=text, score=score)


@pytest.fixture
def big_ctx():
    return MemoryContext(
        query="新能源",
        facts=[hit("事实A高分", 0.9), hit("事实B中分", 0.6), hit("事实C低分", 0.2)],
        profiles=[hit("偏好X", 0.8)],
        style_card="偏好数据密集、少形容词",
    )


class TestBuildInjection:
    def test_empty_context_returns_blank(self, settings):
        assert build_injection(MemoryContext(query="q")) == ""

    def test_full_sections(self, big_ctx):
        block = build_injection(big_ctx, budget_tokens=1000)
        assert block.startswith("〈长期记忆〉")
        assert "〈用户画像〉" in block and "偏好X" in block
        assert "事实A高分" in block and "偏好数据密集" in block

    def test_budget_drops_lowest_first(self, big_ctx):
        # 单条约 3–4 tok:预算 10 刚好装下事实A(0.9)+偏好X(0.8),其余逐出
        block = build_injection(big_ctx, budget_tokens=10)
        assert "偏好X" in block          # score 0.8 保留
        assert "事实A高分" in block       # score 0.9 保留
        assert "事实B中分" not in block   # 0.6 起逐出
        assert "事实C低分" not in block
        assert "偏好数据密集" not in block  # 风格卡超预算即弃

    def test_token_estimate_handles_empty(self):
        assert intercept._estimate_tokens("十个汉字测试") > 0
        assert intercept._estimate_tokens("") >= 1


class TestNodeWhitelist:
    def test_unknown_node_never_injects(self, settings, monkeypatch):
        monkeypatch.setattr(
            intercept, "retrieve", lambda q, with_style=True: pytest.fail("不应检索")
        )
        assert intercept.injection_for_node("render_export", "q") == ""

    def test_chapter_write_injects_style_only(self, settings, monkeypatch):
        # 定稿三注入点:逐章写作只挂风格卡,事实/画像即使命中也不注入
        ctx = MemoryContext(
            query="q", facts=[hit("事实", 0.9)], profiles=[hit("偏好", 0.9)],
            style_card="数据密集少形容词",
        )
        monkeypatch.setattr(intercept, "retrieve", lambda q, with_style=True: ctx)
        block = intercept.injection_for_node("chapter_write", "q")
        assert "数据密集少形容词" in block
        assert "事实" not in block and "偏好" not in block

    def test_first_turn_requires_new_session(self, settings, monkeypatch):
        called = []
        monkeypatch.setattr(
            intercept,
            "retrieve",
            lambda q, with_style=True: called.append(q) or MemoryContext(query=q),
        )
        assert intercept.injection_for_node("chat_first_turn", "q", new_session=False) == ""
        assert called == []  # 旧会话不触发检索
        assert intercept.injection_for_node("chat_first_turn", "q", new_session=True) == ""
        assert called == ["q"]

    def test_triage_drops_facts_keeps_profile(self, settings, monkeypatch):
        ctx = MemoryContext(query="q", facts=[hit("f", 0.9)], profiles=[hit("p", 0.9)])
        monkeypatch.setattr(intercept, "retrieve", lambda q, with_style=True: ctx)
        block = intercept.injection_for_node("triage", "q")
        assert "p" in block and "f" not in block
