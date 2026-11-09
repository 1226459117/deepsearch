"""Vue SPA JSON API 离线单测:与 test_web 同一套替身,手动 run_once 驱动。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepsearch.graph.build import build_graph
from deepsearch.graph.checkpoint import memory_checkpointer
from deepsearch.memory import archive, intercept, llm, profile, service, vectors
from deepsearch.memory.llm import ChatResult
from deepsearch.tools import search
from deepsearch.web import queue as Q, worker as WK
from deepsearch.web.main import create_app
from tests.test_web import make_fake_chat


@pytest.fixture
def studio(settings, monkeypatch):
    fake_chat, _ = make_fake_chat()
    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "chat_with_tools",
                        lambda *a, **k: ChatResult(content="你好,我在。"))
    monkeypatch.setattr(service, "post_turn", lambda *a, **k: {})
    monkeypatch.setattr(service, "chat_context",
                        lambda t, c, **k: [{"role": "user", "content": c}])
    monkeypatch.setattr(intercept, "injection_for_node", lambda *a, **k: "")
    monkeypatch.setattr(search, "web_search",
                        lambda q, count=5: search.SearchBundle([], "none", False))
    monkeypatch.setattr(profile, "distill",
                        lambda *a, **k: {"fact": 0, "profile": 0})

    conn, store = Q.open_job_store(str(settings.data_dir))
    graph = build_graph(memory_checkpointer())
    bus = WK.EventBus()
    wkr = WK.Worker(graph, store, bus)
    reports = Path(settings.data_dir.replace("data", "reports"))
    deps = WK.WebState(graph, store, wkr, bus, conn, reports)
    app = create_app(state=deps, start_worker=False)
    with TestClient(app) as client:
        yield {"store": store, "worker": wkr, "client": client,
               "graph": graph, "reports": reports}
    conn.close()


def _create(client, topic="光伏行业前景", route="report"):
    r = client.post("/api/tasks", json={"topic": topic, "route": route})
    assert r.status_code == 200
    return r.json()["thread_id"]


class TestTaskApi:
    def test_create_and_overview(self, studio):
        client = studio["client"]
        tid = _create(client)
        ov = client.get("/api/overview").json()
        assert ov["version"]
        assert any(j["thread_id"] == tid and j["status"] == "queued"
                   for j in ov["jobs"])
        assert ov["counts"]["queued"] >= 1

    def test_create_validation(self, studio):
        client = studio["client"]
        assert client.post("/api/tasks", json={"topic": "  "}).status_code == 422
        assert client.post("/api/tasks",
                           json={"topic": "x", "route": "steering"}).status_code == 422

    def test_thread_snapshot_and_404(self, studio):
        client, wkr = studio["client"], studio["worker"]
        tid = _create(client)
        wkr.run_once()  # report → CP1 挂起
        data = client.get(f"/api/threads/{tid}").json()
        assert data["job"]["status"] == "waiting_human"
        assert data["job"]["gate"]["gate"] == "CP1"
        assert data["progress"]["status"] == "waiting_human"
        assert data["progress"]["gate"] == "CP1"
        assert client.get("/api/threads/no-such").status_code == 404

    def test_gate_pipeline_to_report(self, studio):
        client, wkr = studio["client"], studio["worker"]
        tid = _create(client)
        wkr.run_once()  # CP1
        assert client.post(f"/api/threads/{tid}/gate",
                           json={"action": "approve"}).json()["ok"]
        wkr.run_once()  # CP2
        data = client.get(f"/api/threads/{tid}").json()
        assert data["job"]["gate"]["gate"] == "CP2"
        r = client.post(f"/api/threads/{tid}/gate",
                        json={"action": "submit", "edit_c1": "人工首章定稿"})
        assert r.status_code == 200
        wkr.run_once()  # revise→assemble→render→END
        data = client.get(f"/api/threads/{tid}").json()
        assert data["job"]["status"] == "done", data["job"]["error"]
        assert data["progress"]["report_name"]
        # 网关已清:再提交 409
        assert client.post(f"/api/threads/{tid}/gate",
                           json={"action": "approve"}).status_code == 409
        name = data["progress"]["report_name"]
        md = client.get(f"/files/{name}/report.md")
        assert "人工首章定稿" in md.text
        assert md.headers["content-disposition"].startswith("attachment")
        html = client.get(f"/files/{name}/report.html?inline=1")
        assert html.headers["content-disposition"].startswith("inline")

    def test_steer_and_cancel_retry(self, studio):
        client, wkr, store = studio["client"], studio["worker"], studio["store"]
        tid = _create(client)
        wkr.run_once()  # CP1
        assert client.post(f"/api/threads/{tid}/steer",
                           json={"text": "多画两张图"}).status_code == 200
        wkr.run_once()  # steering job
        values = studio["graph"].get_state(
            {"configurable": {"thread_id": tid}}).values
        assert "多画两张图" in [d["text"] for d in values["pending_directives"]]
        assert client.get(f"/api/threads/{tid}").json()["progress"]["directives_open"] == 1

        job = store.waiting_for_thread(tid)
        assert client.post(f"/api/jobs/{job.id}/cancel").json()["ok"]
        assert store.get(job.id).status == Q.FAILED
        r = client.post(f"/api/jobs/{job.id}/retry").json()
        assert r["thread_id"] == f"{tid}-r1"
        new_job = store.latest_for_thread(f"{tid}-r1")
        assert client.post(f"/api/jobs/{new_job.id}/retry").status_code == 409  # queued 不可重试
        assert client.post("/api/jobs/999999/cancel").status_code == 404

    def test_upload_api(self, studio, settings):
        client, wkr = studio["client"], studio["worker"]
        tid = _create(client)
        wkr.run_once()  # CP1
        body = "多晶硅组件价格2026年走势与产能过剩分析".encode("utf-8")
        r = client.post(f"/api/threads/{tid}/upload",
                        files={"file": ("私有笔记.md", body, "text/markdown")})
        assert r.status_code == 200 and r.json()["doc_id"]
        values = studio["graph"].get_state(
            {"configurable": {"thread_id": tid}}).values
        assert r.json()["doc_id"] in values["source_ids"]
        assert studio["store"].waiting_for_thread(tid).gate["gate"] == "CP1"

    def test_reroute_api(self, studio):
        client, wkr = studio["client"], studio["worker"]
        tid = _create(client, topic="你好", route="chat")
        wkr.run_once()
        assert client.post(f"/api/threads/{tid}/reroute",
                           json={"route": "report"}).json()["ok"]
        wkr.run_once()
        assert client.get(f"/api/threads/{tid}").json()["job"]["gate"]["gate"] == "CP1"
        assert client.post(f"/api/threads/{tid}/reroute",
                           json={"route": "steering"}).status_code == 422


class TestReportsApi:
    def test_listing_and_empty(self, studio):
        client, reports = studio["client"], studio["reports"]
        reports.mkdir(parents=True, exist_ok=True)
        data = client.get("/api/reports").json()
        assert data["dirs"] == []
        d = reports / "20261109-测试-t1"
        d.mkdir()
        (d / "report.md").write_text("# 测试", encoding="utf-8")
        (d / "report.html").write_text("<h1>测试</h1>", encoding="utf-8")
        data = client.get("/api/reports").json()
        assert data["dirs"][0]["name"] == d.name
        assert "report.md" in data["dirs"][0]["rels"]


class TestMemoryApi:
    @pytest.fixture
    def mem(self, studio, monkeypatch):
        from datetime import datetime

        cand = archive.Candidate(
            id=1, tenant_id="default", user_id="owner", kind="profile",
            source_type="cp2_diff", source_ref="t-1", content="用户偏好稳健风格",
            status="pending", created_at=datetime(2026, 11, 5), reviewed_at=None,
        )
        calls = {"adopted": [], "style": [], "rating": []}
        monkeypatch.setattr(archive, "list_candidates",
                            lambda status="pending", **kw: [cand] if status == "pending" else [])
        monkeypatch.setattr(archive, "get_style_card", lambda: "结论先行")
        monkeypatch.setattr(archive, "list_source_ratings",
                            lambda: [{"domain": "caixin.com", "score": 0.9, "note": "稳"}])
        monkeypatch.setattr(archive, "mark_candidate", lambda cid, st, **kw: None)
        monkeypatch.setattr(archive, "record_adopted",
                            lambda kind, content, **kw: calls["adopted"].append((kind, content)))
        monkeypatch.setattr(archive, "upsert_style_card",
                            lambda c: calls["style"].append(c))
        monkeypatch.setattr(archive, "upsert_source_rating",
                            lambda d, s, n=None: calls["rating"].append((d, s, n)))
        monkeypatch.setattr(vectors, "upsert_memory",
                            lambda kind, text, source_ref="": "mv-" + kind)
        studio["calls"] = calls
        return studio

    def test_snapshot(self, mem):
        data = mem["client"].get("/api/memory").json()
        assert data["offline"] is False
        assert data["pending"][0]["content"] == "用户偏好稳健风格"
        assert data["style_card"] == "结论先行"
        assert data["ratings"][0]["domain"] == "caixin.com"

    def test_adopt_reject_style_rating(self, mem):
        client = mem["client"]
        assert client.post("/api/memory/adopt", json={"candidate_id": 1}).json()["ok"]
        assert mem["calls"]["adopted"] == [("profile", "用户偏好稳健风格")]
        assert client.post("/api/memory/adopt", json={"candidate_id": 99}).status_code == 404
        assert client.post("/api/memory/reject", json={"candidate_id": 1}).json()["ok"]
        assert client.post("/api/memory/style", json={"content": " 多画图 "}).json()["ok"]
        assert mem["calls"]["style"] == ["多画图"]
        assert client.post("/api/memory/rating",
                           json={"domain": " Weibo.COM ", "score": 0.3,
                                 "note": "口径杂"}).json()["ok"]
        assert mem["calls"]["rating"] == [("weibo.com", 0.3, "口径杂")]
        assert client.post("/api/memory/rating",
                           json={"domain": "a", "score": 1.5}).status_code == 422

    def test_offline_degrades(self, mem, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection timeout")

        monkeypatch.setattr(archive, "list_candidates", boom)
        monkeypatch.setattr(archive, "get_style_card", boom)
        monkeypatch.setattr(archive, "list_source_ratings", boom)
        monkeypatch.setattr(archive, "mark_candidate", boom)
        data = mem["client"].get("/api/memory").json()
        assert data["offline"] is True and data["pending"] == []
        assert mem["client"].post("/api/memory/reject",
                                  json={"candidate_id": 1}).status_code == 502


class TestSpaHosting:
    def test_index_serves_html(self, studio):
        r = studio["client"].get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]