"""service.py 门面离线单测:三域写入顺序、压缩联动、上下文组装。"""

from __future__ import annotations

import pytest

from deepsearch.memory import archive, intercept, service, vectors
from tests.fakes import FakeRedis


@pytest.fixture
def mocks(settings, monkeypatch):
    """PG/Milvus 全部替身,记录调用序列;Redis 用 FakeRedis。"""
    calls: list[tuple] = []

    def fake_append_turn(thread, u, a, **kw):
        calls.append(("pg_archive", thread))
        return len([c for c in calls if c[0] == "pg_archive"])

    monkeypatch.setattr(archive, "append_turn", fake_append_turn)
    monkeypatch.setattr(
        archive,
        "record_summary",
        lambda thread, f, t, s, m=None: calls.append(("pg_summary", f, t)),
    )
    monkeypatch.setattr(
        vectors,
        "upsert_conversation_summary",
        lambda s, thread, f, t: calls.append(("milvus", thread)) or "mv-1",
    )
    return calls


def fake_compressor(prev, turns):
    return "压缩产物"


class TestPostTurn:
    def test_order_pg_then_redis_no_compaction(self, mocks):
        result = service.post_turn(
            "t9", "q1", "a1", compressor=fake_compressor, client=FakeRedis()
        )
        assert mocks == [("pg_archive", "t9")]
        assert result["turn_no"] == 1
        assert result["window_rounds"] == 1
        assert result["compacted"] is None

    def test_compaction_at_30_writes_all_domains(self, mocks):
        client = FakeRedis()
        for i in range(1, 31):
            result = service.post_turn(
                "t9", f"q{i}", f"a{i}", compressor=fake_compressor, client=client
            )
        # 第 30 轮触发:PG 摘要行 + Milvus 向量都写,窗口剩 10 轮
        assert result["compacted"] is not None
        assert result["window_rounds"] == 10
        kinds = [c[0] for c in mocks]
        assert kinds.count("pg_archive") == 30
        assert ("pg_summary", 1, 20) in mocks
        assert ("milvus", "t9") in mocks
        # 摘要行的 milvus_id 透传自向量层(由 on_stored 编排保证)

    def test_summary_failure_keeps_window_intact(self, mocks):
        client = FakeRedis()
        for i in range(1, 31):
            result = service.post_turn(
                "t9", f"q{i}", f"a{i}", compressor=lambda p, t: "", client=client
            )
        assert result["compacted"] is None
        assert result["window_rounds"] == 30
        assert [c for c in mocks if c[0] != "pg_archive"] == []  # 未跨域写


class TestChatContext:
    def test_new_session_prepends_injection(self, settings, monkeypatch):
        monkeypatch.setattr(intercept, "is_new_session", lambda t: True)
        monkeypatch.setattr(
            intercept,
            "injection_for_node",
            lambda node, q, new_session=False: "〈长期记忆〉注入块",
        )
        messages = service.chat_context("t1", "你好", client=FakeRedis())
        assert messages[0] == {"role": "system", "content": "〈长期记忆〉注入块"}
        assert messages[-1] == {"role": "user", "content": "你好"}

    def test_old_session_no_injection(self, settings, monkeypatch):
        monkeypatch.setattr(intercept, "is_new_session", lambda t: False)
        monkeypatch.setattr(
            intercept,
            "injection_for_node",
            lambda *a, **kw: pytest.fail("旧会话不应拦截"),
        )
        client = FakeRedis()
        service.ConversationWindow("t1", client=client).push_turn("旧问", "旧答")
        messages = service.chat_context("t1", "新问题", client=client)
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]


class DeadClient:
    """任何 Redis 调用都超时的存储替身(自备实例离线的真实形态)。"""

    def __getattr__(self, name):
        def raiser(*a, **kw):
            raise TimeoutError(f"redis {name} timed out")

        return raiser


class TestStorageDegradation:
    """定稿契约:PG/Redis/Milvus 离线只告警降级,chat 短链路不得整单失败。"""

    def test_post_turn_survives_all_storages_down(self, settings, monkeypatch):
        def die(*a, **kw):
            raise ConnectionError("pg down")

        monkeypatch.setattr(archive, "append_turn", die)
        result = service.post_turn(
            "t-dead", "q", "a", compressor=fake_compressor, client=DeadClient()
        )
        assert result["turn_no"] == -1
        assert result["window_rounds"] == 0
        assert result["compacted"] is None

    def test_chat_context_survives_redis_down(self, settings, monkeypatch):
        monkeypatch.setattr(intercept, "is_new_session", lambda t: False)
        messages = service.chat_context("t-dead", "你好", client=DeadClient())
        assert messages == [{"role": "user", "content": "你好"}]

    def test_compaction_survives_one_domain_failure(self, mocks, monkeypatch):
        """满窗压缩时 Milvus 挂:PG 摘要行照落,窗口照裁,主链路不炸。"""

        def milvus_die(*a, **kw):
            raise TimeoutError("milvus timed out")

        monkeypatch.setattr(vectors, "upsert_conversation_summary", milvus_die)
        client = FakeRedis()
        for i in range(1, 31):
            result = service.post_turn(
                "t9", f"q{i}", f"a{i}", compressor=fake_compressor, client=client
            )
        assert result["compacted"] is not None
        assert result["window_rounds"] == 10
        assert ("pg_summary", 1, 20) in mocks  # 另一边未被单边故障阻断
