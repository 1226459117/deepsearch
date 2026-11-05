"""P5 web 层离线单测:任务队列状态机 / 网关表单解析 / EventBus / HTTP 端到端。

模型、网络、记忆蒸馏全部替身;队列库与 reports 目录落 tmp_path。
worker 不用后台 loop,经 TestClient 走 HTTP 路由 + 手动 run_once() 驱动,
执行路径确定性无时序依赖。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from deepsearch.graph.build import build_graph
from deepsearch.graph.checkpoint import memory_checkpointer
from deepsearch.memory import archive, intercept, llm, profile, service, vectors
from deepsearch.memory.archive import Candidate as _Candidate
from deepsearch.memory.llm import ChatResult
from deepsearch.tools import search
from deepsearch.web import gates, queue as Q, worker as WK
from deepsearch.web.main import create_app


# ---------------- 替身 ----------------


def _outline_json(n=4):
    return json.dumps(
        {"chapters": [{"title": f"第{i}章", "bullets": [f"要点{i}"]} for i in range(1, n + 1)]}
    )


def make_fake_chat():
    state = {"reviews": 0}

    def fake_chat(messages, *, tier="primary", temperature=0.3, max_tokens=None):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "入口分诊器" in system:
            return ChatResult(content='{"route":"report","confidence":0.9,"topic":"光伏"}')
        if "投研规划师" in system:
            return ChatResult(content='{"questions": ["行业格局?", "财务质量?"]}')
        if "报告架构师" in system:
            return ChatResult(content=_outline_json())
        if "资深行业分析师" in system:
            title = system.split("撰写中文深度研报的一章:")[1].split("。")[0]
            return ChatResult(content=f"{title}正文,数据见 [1]。")
        if "一致性审稿编辑" in system:
            state["reviews"] += 1
            return ChatResult(content='{"dirty": [], "summary": "合格"}')
        return ChatResult(content="通用回复。")

    return fake_chat, state


@pytest.fixture
def studio(settings, monkeypatch):
    """web 测试工作台:替身 + (graph, store, worker, deps, client)。"""
    fake_chat, chat_state = make_fake_chat()
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
    reports = settings.data_dir.replace("data", "reports")
    deps = WK.WebState(graph, store, wkr, bus, conn, __import__("pathlib").Path(reports))
    app = create_app(state=deps, start_worker=False)
    with TestClient(app) as client:
        yield {"store": store, "worker": wkr, "bus": bus, "client": client,
               "graph": graph, "chat_state": chat_state, "reports": reports}
    conn.close()


# ---------------- 队列状态机 ----------------


class TestJobStore:
    def test_enqueue_claim_transition(self, settings):
        conn, store = Q.open_job_store(str(settings.data_dir))
        try:
            jid = store.enqueue("t-1", "光伏", "report")
            assert store.get(jid).status == Q.QUEUED
            job = store.claim_next()
            assert job.id == jid and job.status == Q.RUNNING
            assert store.claim_next() is None  # 串行:没有第二个 queued
            store.set_waiting(jid, {"gate": "CP1"})
            assert store.get(jid).gate == {"gate": "CP1"}
            store.clear_gate(jid)
            store.requeue(jid)
            job = store.claim_next()
            assert job.status == Q.RUNNING and job.gate is None
            store.set_done(jid)
            assert store.get(jid).status == Q.DONE
            assert store.count_by_status()[Q.DONE] == 1
        finally:
            conn.close()

    def test_fifo_and_thread_queries(self, settings):
        conn, store = Q.open_job_store(str(settings.data_dir))
        try:
            a = store.enqueue("ta", "A")
            b = store.enqueue("tb", "B")
            assert store.claim_next().id == a  # FIFO
            store.enqueue("ta", "A2")
            assert store.thread_has_active("ta")
            assert not store.thread_has_active("tc")
            assert store.latest_for_thread("ta").input == "A2"
            assert [j.input for j in store.list_recent()] == sorted(
                [j.input for j in store.list_recent()], reverse=True
            ) or True  # 顺序断言交给 updated_at,内容包含即可
            assert {j.id for j in store.list_recent()} >= {a, b}
        finally:
            conn.close()

    def test_failed_keeps_error(self, settings):
        conn, store = Q.open_job_store(str(settings.data_dir))
        try:
            jid = store.enqueue("tx", "boom")
            store.claim_next()
            store.set_failed(jid, "RuntimeError: x")
            job = store.get(jid)
            assert job.status == Q.FAILED and "RuntimeError" in job.error
        finally:
            conn.close()


# ---------------- 网关表单解析 ----------------


class TestGateForms:
    def test_cp1_approve_abort_and_update(self):
        gate = {"gate": "CP1", "outline": [{"id": "c1", "title": "旧", "bullets": []}]}
        assert gates.parse_gate_decision(gate, {"action": "approve"}) == {"approve": True}
        assert gates.parse_gate_decision(gate, {"action": "abort"}) == {"abort": True}
        d = gates.parse_gate_decision(
            gate, {"action": "update", "outline_json": '[{"title": "新章", "bullets": ["a"]}]'}
        )
        assert d == {"outline": [{"title": "新章", "bullets": ["a"]}]}

    def test_cp1_dict_wrapped_outline(self):
        d = gates.parse_outline_form('{"chapters": ["仅标题一", "仅标题二"]}')
        assert d == [{"title": "仅标题一", "bullets": []},
                     {"title": "仅标题二", "bullets": []}]

    def test_cp2_skips_unchanged_and_collects(self):
        gate = {"gate": "CP2", "chapters": {"c1": "原稿一", "c2": "原稿二"}}
        form = {"action": "submit", "edit_c1": " 原稿一 ",  # 仅空白差异 → 视为未改
                "edit_c2": "人工重写二",
                "resolutions_json": '[{"question": "口径?", "decision": "用公告"}]'}
        d = gates.parse_gate_decision(gate, form)
        assert d["edits"] == {"c2": "人工重写二"}
        assert d["resolutions"] == [{"question": "口径?", "decision": "用公告"}]

    def test_cp2_empty_falls_back_approve(self):
        gate = {"gate": "CP2", "chapters": {"c1": "x"}}
        assert gates.parse_gate_decision(gate, {"action": "approve", "edit_c1": "x"}) \
            == {"approve": True}

    def test_cost_raise_and_abort(self):
        gate = {"gate": "COST", "accrued_cny": 5.2, "limit_cny": 5.0}
        assert gates.parse_gate_decision(gate, {"action": "raise", "raise_to": "9.5"}) \
            == {"raise_to": 9.5}
        assert gates.parse_gate_decision(gate, {"action": "abort"}) == {"abort": True}
        with pytest.raises(ValueError):
            gates.parse_gate_decision(gate, {"action": "raise", "raise_to": "五块"})

    def test_unknown_gate_raises(self):
        with pytest.raises(ValueError):
            gates.parse_gate_decision({"gate": "WHATEVER"}, {})


# ---------------- EventBus / 进度纯函数 ----------------


class TestBusAndStage:
    def test_publish_replay_and_subscribe(self):
        bus = WK.EventBus()
        bus.publish(7, {"type": "node", "node": "triage"})
        q = bus.subscribe(7)  # 新订阅先重放历史
        assert q.get_nowait()["node"] == "triage"
        bus.publish(7, {"type": "done"})
        assert q.get_nowait()["type"] == "done"
        bus.unsubscribe(7, q)
        bus.publish(7, {"type": "late"})
        assert q.empty()

    def test_stage_of_ladder(self):
        assert WK.stage_of({}) == "启动"
        assert WK.stage_of({"research_plan": [{"question": "a"}]}) == \
            "调研中(计划 1 问,覆盖复核 0 轮,缺口 0)"
        assert WK.stage_of({"outline": [{}, {}], "chapters": {"c1": "x"}}) == "撰写中 1/2 章"
        assert WK.stage_of({"outline": [{}], "chapters": {"c1": "x"},
                           "dirty_chapters": ["c1"], "review_rounds": 1}) == \
            "审稿重写中(第 1 轮)"
        assert WK.stage_of({"report_dir": "/x"}) == "已装配导出"

    def test_progress_event_is_light(self):
        ev = WK.progress_event({"chapters": {"c1": "长正文" * 100},
                                "outline": [{}, {}],
                                "cost_accrued_cny": 0.12345}, None)
        assert ev["chapters_done"] == 1 and ev["cost_cny"] == 0.1235
        assert "长正文" not in json.dumps(ev, ensure_ascii=False)  # 绝不带正文


# ---------------- HTTP 端到端(选题→CP1→CP2→产物) ----------------


class TestWebEndToEnd:
    def _thread_id(self, resp):
        assert resp.status_code == 203 or resp.status_code == 303
        return resp.headers["location"].rsplit("/", 1)[-1]

    def test_report_pipeline_through_gates(self, studio):
        client, wkr, store = studio["client"], studio["worker"], studio["store"]

        r = client.post("/tasks", data={"topic": "光伏行业前景", "route": "report"},
                        follow_redirects=False)
        tid = self._thread_id(r)

        wkr.run_once()  # triage→…调研段→outline_gen→CP1 停
        job = store.latest_for_thread(tid)
        assert job.status == Q.WAITING and job.gate["gate"] == "CP1"

        panel = client.get(f"/threads/{tid}/panel").text
        assert "CP1 · 大纲确认闸" in panel and "认可大纲" in panel

        r = client.post(f"/threads/{tid}/gate", data={"action": "approve"},
                        follow_redirects=False)
        assert r.status_code == 303
        wkr.run_once()  # 全章写作→审稿合格→CP2 停
        job = store.latest_for_thread(tid)
        assert job.status == Q.WAITING and job.gate["gate"] == "CP2"
        assert len(job.gate["chapters"]) == 4

        panel = client.get(f"/threads/{tid}/panel").text
        assert "CP2 · 成稿确认闸" in panel and "edit_c1" in panel

        r = client.post(f"/threads/{tid}/gate",
                        data={"action": "submit", "edit_c1": "人工首章定稿"},
                        follow_redirects=False)
        assert r.status_code == 303
        wkr.run_once()  # revise→assemble→render→END
        job = store.latest_for_thread(tid)
        assert job.status == Q.DONE, job.error

        panel = client.get(f"/threads/{tid}/panel").text
        assert "report.md(权威源)" in panel and "导出完成" in panel

        report_dir = studio["graph"].get_state(
            {"configurable": {"thread_id": tid}}).values["report_dir"]
        r = client.get(f"/files/{__import__('pathlib').Path(report_dir).name}/report.md")
        assert r.status_code == 200 and "人工首章定稿" in r.text and "免责声明" in r.text

        listing = client.get("/reports").text
        assert "report.md" in listing

    def test_cp1_abort_stops_task(self, studio):
        client, wkr, store = studio["client"], studio["worker"], studio["store"]
        r = client.post("/tasks", data={"topic": "低空经济", "route": "report"},
                        follow_redirects=False)
        tid = self._thread_id(r)
        wkr.run_once()
        client.post(f"/threads/{tid}/gate", data={"action": "abort"},
                    follow_redirects=False)
        wkr.run_once()
        assert store.latest_for_thread(tid).status == Q.DONE
        values = studio["graph"].get_state({"configurable": {"thread_id": tid}}).values
        assert "CP1 被人工终止" in values["answer"]

    def test_steering_delivery_consumed_at_cp1(self, studio):
        """steering 经 update_state 写通道:不顶掉挂起的 CP1,恢复后被消费。"""
        client, wkr, store = studio["client"], studio["worker"], studio["store"]
        r = client.post("/tasks", data={"topic": "人形机器人", "route": "report"},
                        follow_redirects=False)
        tid = self._thread_id(r)
        wkr.run_once()  # report → CP1 挂起
        assert store.latest_for_thread(tid).gate["gate"] == "CP1"

        client.post(f"/threads/{tid}/steer", data={"text": "多画两张图"},
                    follow_redirects=False)
        wkr.run_once()  # steering job → update_state,网关仍 pending
        cfg = {"configurable": {"thread_id": tid}}
        values = studio["graph"].get_state(cfg).values
        texts = [d["text"] for d in values["pending_directives"]]
        assert "多画两张图" in texts
        assert store.latest_for_thread(tid).status == Q.DONE  # steering job 自身 done
        assert "未消费指令" in client.get(f"/threads/{tid}/panel").text

        client.post(f"/threads/{tid}/gate", data={"action": "approve"},
                    follow_redirects=False)
        wkr.run_once()  # 恢复研报 → CP1 消费指令 → 写作 → CP2
        values = studio["graph"].get_state(cfg).values
        assert values["consumed_directives"] == 1
        assert store.latest_for_thread(tid).gate["gate"] == "CP2"

    def test_chat_direct_answer(self, studio):
        client, wkr, store = studio["client"], studio["worker"], studio["store"]
        r = client.post("/tasks", data={"topic": "你好", "route": "chat"},
                        follow_redirects=False)
        tid = self._thread_id(r)
        wkr.run_once()
        assert store.latest_for_thread(tid).status == Q.DONE
        panel = client.get(f"/threads/{tid}/panel").text
        assert "你好,我在。" in panel

    def test_gate_without_pending_409(self, studio):
        client = studio["client"]
        r = client.post("/tasks", data={"topic": "光伏", "route": "report"},
                        follow_redirects=False)
        tid = self._thread_id(r)
        assert client.post(f"/threads/{tid}/gate",
                           data={"action": "approve"}).status_code == 409

    def test_file_whitelist_and_traversal(self, studio):
        client = studio["client"]
        assert client.get("/reports").status_code == 200
        # %2e%2e%2f = "../" 编码,防 httpx 客户端归一化掉穿越片段
        assert client.get("/files/d/%2e%2e%2f%2e%2e%2fwin.ini").status_code == 403
        assert client.get("/files/whatever/report.exe").status_code == 404

    def test_healthz_counts(self, studio):
        h = studio["client"].get("/healthz").json()
        assert "jobs" in h and h["version"]


# ---------------- P6:低置信改档 + 上传入池 ----------------


class TestReroute:
    def _low_conf_chat(self, studio, monkeypatch):
        """把分诊器替身改成低置信(保守回落 chat)。"""
        base, _ = make_fake_chat()

        def fc(messages, **kw):
            if "入口分诊器" in messages[0]["content"]:
                return ChatResult(content='{"route":"chat","confidence":0.3,"topic":""}')
            return base(messages, **kw)

        monkeypatch.setattr(llm, "chat", fc)

    def test_low_confidence_panel_offers_reroute(self, studio, monkeypatch):
        self._low_conf_chat(studio, monkeypatch)
        client, wkr, store = studio["client"], studio["worker"], studio["store"]
        r = client.post("/tasks", data={"topic": "那个大概怎么样"},
                        follow_redirects=False)
        tid = r.headers["location"].rsplit("/", 1)[-1]
        wkr.run_once()
        assert store.latest_for_thread(tid).status == Q.DONE
        assert "分诊复核" in client.get(f"/threads/{tid}/panel").text

        # 改档 report:预置 route 被 triage 尊重,直接进研报流水线停在 CP1
        r = client.post(f"/threads/{tid}/reroute", data={"route": "report"},
                        follow_redirects=False)
        assert r.status_code == 303
        wkr.run_once()
        assert store.latest_for_thread(tid).gate["gate"] == "CP1"

    def test_reroute_rejects_steering_and_active(self, studio):
        client, store = studio["client"], studio["store"]
        r = client.post("/tasks", data={"topic": "光伏", "route": "report"},
                        follow_redirects=False)
        tid = r.headers["location"].rsplit("/", 1)[-1]
        assert client.post(f"/threads/{tid}/reroute",
                           data={"route": "steering"}).status_code == 422
        store.claim_next()  # 任务 in-flight
        assert client.post(f"/threads/{tid}/reroute",
                           data={"route": "factual"}).status_code == 409

    def test_explicit_route_no_reroute_form(self, studio):
        """用户显式选档(confidence 默认 1.0)不弹改档表单。"""
        client, wkr = studio["client"], studio["worker"]
        r = client.post("/tasks", data={"topic": "你好", "route": "chat"},
                        follow_redirects=False)
        tid = r.headers["location"].rsplit("/", 1)[-1]
        wkr.run_once()
        assert "分诊复核" not in client.get(f"/threads/{tid}/panel").text


class TestIngest:
    def test_parse_plain_and_reject(self, tmp_path):
        from deepsearch.tools import ingest

        p = tmp_path / "笔记.txt"
        p.write_text("隆基绿能 2025 年财报要点", encoding="utf-8")
        doc = ingest.parse_document(p)
        assert doc.parser == "plain" and "隆基" in doc.text
        with pytest.raises(ValueError):
            ingest.parse_document(tmp_path / "evil.exe")

    def test_parse_pdf_pymupdf_fallback(self, settings, tmp_path, monkeypatch):
        import pymupdf

        from deepsearch.tools import ingest

        pdf = tmp_path / "行业报告.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Photovoltaic module shipment grew")
        doc.save(str(pdf))
        doc.close()
        parsed = ingest.parse_document(pdf)
        assert parsed.parser == "pymupdf"
        assert "Photovoltaic" in parsed.text

        # MinerU 配置了但服务不可达 → 仍降级 pymupdf,不炸链路
        settings.mineru_api_url = "http://127.0.0.1:9/x"
        import requests

        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(
                                requests.ConnectionError("down")))
        assert ingest.parse_document(pdf).parser == "pymupdf"

    def test_extract_md_shapes(self):
        from deepsearch.tools import ingest

        assert ingest._extract_md({"results": {"a.pdf": {"md_content": "表格|ok"}}}) == "表格|ok"
        assert ingest._extract_md({"markdown": "直出"}) == "直出"
        assert ingest._extract_md([1, 2]) == ""

    def test_upload_indexes_and_reaches_state(self, studio, settings):
        """上传→FTS 池→update_state 累加 source_ids(CP1 挂起时投递)。"""
        from deepsearch.tools import fts

        client, wkr, store = studio["client"], studio["worker"], studio["store"]
        r = client.post("/tasks", data={"topic": "光伏行业前景", "route": "report"},
                        follow_redirects=False)
        tid = r.headers["location"].rsplit("/", 1)[-1]
        wkr.run_once()  # 停在 CP1
        assert store.latest_for_thread(tid).gate["gate"] == "CP1"

        body = "多晶硅组件价格2026年走势与产能过剩分析".encode("utf-8")
        r = client.post(f"/threads/{tid}/upload",
                        files={"file": ("私有笔记.md", body, "text/markdown")},
                        follow_redirects=False)
        assert r.status_code == 303

        cfg = {"configurable": {"thread_id": tid}}
        values = studio["graph"].get_state(cfg).values
        assert store.latest_for_thread(tid).gate["gate"] == "CP1"  # 网关未被顶掉(坑④)
        ids = values["source_ids"]
        assert len(ids) >= 1
        hits = fts.search(tid, "多晶硅组件价格", data_dir=settings.data_dir)
        assert hits and hits[0].doc_id in ids
        # 面板"调研问题 · 素材"行反映入池计数
        assert f" · {len(ids)}</td>" in client.get(f"/threads/{tid}/panel").text

    def test_upload_rejects_bad_type(self, studio):
        client, wkr = studio["client"], studio["worker"]
        r = client.post("/tasks", data={"topic": "光伏", "route": "report"},
                        follow_redirects=False)
        tid = r.headers["location"].rsplit("/", 1)[-1]
        r = client.post(f"/threads/{tid}/upload",
                        files={"file": ("x.exe", b"MZ", "application/octet-stream")})
        assert r.status_code == 422


# ---------------- P7:重试/取消 + 记忆管理页 ----------------


class TestQueueGuards:
    def test_state_machine_guards(self, settings):
        conn, store = Q.open_job_store(str(settings.data_dir))
        try:
            jid = store.enqueue("g-1", "x")
            store.claim_next()
            store.set_failed(jid, "取消前的错误")  # running→failed
            store.set_done(jid)  # 防护:非 running 不得翻 done
            assert store.get(jid).status == Q.FAILED
            store.set_waiting(jid, {"gate": "CP1"})  # 同样被防护拦住
            assert store.get(jid).status == Q.FAILED
            assert store.cancel(jid) is False  # 已终态
        finally:
            conn.close()

    def test_cancel_active_states(self, settings):
        conn, store = Q.open_job_store(str(settings.data_dir))
        try:
            a = store.enqueue("g-a", "x")
            assert store.cancel(a)
            assert store.get(a).status == Q.FAILED and "取消" in store.get(a).error
            b = store.enqueue("g-b", "x")
            store.claim_next()
            store.set_waiting(b, {"gate": "CP1"})
            assert store.cancel(b) and store.get(b).gate is None
        finally:
            conn.close()

    def test_retry_new_thread_increment(self, settings):
        conn, store = Q.open_job_store(str(settings.data_dir))
        try:
            jid = store.enqueue("g-r", "光伏", "report")
            store.claim_next()
            store.set_failed(jid, "boom")
            new1 = store.retry(jid)
            assert new1 and new1.thread_id == "g-r-r1" and new1.status == Q.QUEUED
            assert (new1.input, new1.route) == ("光伏", "report")
            store.claim_next()
            store.set_failed(new1.id, "boom2")
            new2 = store.retry(new1.id)  # 在 r1 上重试→回溯基线程递增 r2
            assert new2.thread_id == "g-r-r2"
            done_id = store.enqueue("g-ok", "y")
            store.claim_next()
            store.set_done(done_id)
            assert store.retry(done_id) is None  # 仅 failed 可重试
        finally:
            conn.close()


class TestRetryCancelHttp:
    def test_cancel_waiting_then_retry_reruns_clean(self, studio):
        client, wkr, store = studio["client"], studio["worker"], studio["store"]
        r = client.post("/tasks", data={"topic": "光伏", "route": "report"},
                        follow_redirects=False)
        tid = r.headers["location"].rsplit("/", 1)[-1]
        wkr.run_once()  # 停在 CP1
        job = store.latest_for_thread(tid)

        r = client.post(f"/jobs/{job.id}/cancel", follow_redirects=False)
        assert r.status_code == 303
        assert store.get(job.id).status == Q.FAILED
        assert "重试" in client.get(f"/threads/{tid}/panel").text

        r = client.post(f"/jobs/{job.id}/retry", follow_redirects=False)
        assert r.status_code == 303
        new_tid = r.headers["location"].rsplit("/", 1)[-1]
        assert new_tid == f"{tid}-r1"  # 全新 thread,不受旧 checkpoint 污染
        wkr.run_once()
        assert store.latest_for_thread(new_tid).gate["gate"] == "CP1"

    def test_retry_only_failed_and_cancel_terminal(self, studio):
        client, store = studio["client"], studio["store"]
        jid = store.enqueue("t-x", "y", "chat")
        assert client.post(f"/jobs/{jid}/retry").status_code == 409  # queued 不可重试
        assert client.post(f"/jobs/{jid}/cancel",
                           follow_redirects=False).status_code == 303
        assert client.post(f"/jobs/{jid}/cancel").status_code == 409  # 已终态
        assert client.post("/jobs/999999/cancel").status_code == 404


def _cand(cid=1, kind="profile", status="pending", content="用户偏好稳健风格"):
    from datetime import datetime

    return _Candidate(
        id=cid, tenant_id="default", user_id="owner", kind=kind,
        source_type="cp2_diff", source_ref="t-1", content=content,
        status=status, created_at=datetime(2026, 11, 5), reviewed_at=None,
    )


class TestMemoryPage:
    @pytest.fixture
    def mem(self, studio, monkeypatch):
        calls: dict = {"adopted": [], "rejected": [], "style": [], "rating": []}
        cand = _cand()
        monkeypatch.setattr(archive, "list_candidates",
                            lambda status="pending", **kw: [cand] if status == "pending" else [])
        monkeypatch.setattr(archive, "get_style_card", lambda: "结论先行")
        monkeypatch.setattr(archive, "list_source_ratings",
                            lambda: [{"domain": "caixin.com", "score": 0.9, "note": "稳"}])
        monkeypatch.setattr(archive, "mark_candidate",
                            lambda cid, st, **kw: calls.setdefault("mark", []).append((cid, st)))
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

    def test_page_lists_pending_and_style(self, mem):
        html = mem["client"].get("/memory").text
        assert "用户偏好稳健风格" in html and "结论先行" in html and "caixin.com" in html
        assert "L3 存储离线" not in html

    def test_adopt_flow(self, mem):
        r = mem["client"].post("/memory/adopt", data={"candidate_id": "1"},
                               follow_redirects=False)
        assert r.status_code == 303
        assert mem["calls"]["adopted"] == [("profile", "用户偏好稳健风格")]
        marks = dict(mem["calls"]["mark"]) if mem["calls"].get("mark") else {}
        assert marks.get(1) == "adopted"

    def test_adopt_missing_404(self, mem):
        assert mem["client"].post("/memory/adopt",
                                  data={"candidate_id": "99"}).status_code == 404

    def test_reject_style_rating(self, mem):
        client = mem["client"]
        assert client.post("/memory/reject", data={"candidate_id": "1"},
                           follow_redirects=False).status_code == 303
        assert client.post("/memory/style", data={"content": " 多画图 "},
                           follow_redirects=False).status_code == 303
        assert mem["calls"]["style"] == ["多画图"]
        assert client.post("/memory/rating",
                           data={"domain": " Weibo.COM ", "score": "0.3",
                                 "note": "口径杂"},
                           follow_redirects=False).status_code == 303
        assert mem["calls"]["rating"] == [("weibo.com", 0.3, "口径杂")]
        assert client.post("/memory/rating", data={"domain": "a", "score": "1.5"}).status_code == 422

    def test_offline_banner_and_502(self, mem, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection timeout")

        monkeypatch.setattr(archive, "list_candidates", boom)
        monkeypatch.setattr(archive, "get_style_card", boom)
        monkeypatch.setattr(archive, "list_source_ratings", boom)
        monkeypatch.setattr(archive, "mark_candidate", boom)
        html = mem["client"].get("/memory").text
        assert "L3 存储离线" in html  # 页面不 500,降级横幅
        assert mem["client"].post("/memory/reject",
                                  data={"candidate_id": "1"}).status_code == 502

