"""FastAPI web 层:Vue SPA(JSON API)+ 遗留 HTMX 表单端点并存。

- 前端:frontend/ 的 Vue3 + Element Plus + Vite SPA,构建产物 frontend/dist
  由 / 与 /assets 托管;数据走 /api/* JSON 接口,SSE 复用 /threads/{tid}/events
- 选题输入页:/            SPA 首页(未构建时回退 Jinja 模板)+ 最近任务列表
- 任务队列:   web.queue   SQLite jobs 表;同进程 asyncio worker 串行消费(web.worker)
- 进度流式:   GET /threads/{tid}/events SSE(graph.stream 逐超步事件,含重放);
              GET /threads/{tid}/panel   HTMX 轮询片段(读 checkpoint 快照,断流兜底)
- 网关接线:  waiting_human 时按 gate payload 渲染 CP1/CP2/COST 确认表单,
              POST /threads/{tid}/gate → gates.parse_gate_decision → worker.submit_decision
              → Command(resume) 恢复图执行
- 产物浏览:  GET /reports 列 reports/ 目录;GET /files/{dir}/{path} 白名单下载

生产装配:lifespan 打开 SqliteSaver + jobs 库并启动 worker;
测试装配:create_app(state=WebState(...), start_worker=False) 注入替身,手动 run_once。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from deepsearch import __version__
from deepsearch.config import get_settings
from deepsearch.graph.build import build_graph
from deepsearch.graph.checkpoint import open_checkpointer
from deepsearch.graph.state import ALL_ROUTES
from deepsearch.memory import archive, vectors
from deepsearch.tools import ingest
from deepsearch.web.gates import parse_gate_decision
from deepsearch.web.queue import WAITING, open_job_store
from deepsearch.web.worker import EventBus, WebState, Worker

TEMPLATES_DIR = Path(__file__).parent / "templates"
SAFE_SUFFIXES = {".md", ".html", ".pdf", ".svg", ".png", ".txt"}
# Vue SPA 构建产物(frontend/ 下 npm run build);未构建时首页回退 Jinja 模板
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _js_json(value: Any) -> str:
    """模板内把结构化数据渲染成 JSON 文本(textarea 预填/事件序列化)。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


templates.env.filters["js_json"] = _js_json


def new_thread_id() -> str:
    return uuid.uuid4().hex[:12]


def _active_job(store, tid: str):
    """面板/事件流宿主:优先挂起人工确认的 job,否则回退最近一条。

    同 thread 后入队的 steering job 会先完成,若只看 latest 会把
    研报 job 的 waiting_human 状态和 gate 表单顶掉。"""
    return store.waiting_for_thread(tid) or store.latest_for_thread(tid)


def reports_root(data_dir: str | None = None) -> Path:
    settings = get_settings()
    root = Path(data_dir or settings.data_dir).parent / "reports"
    root.mkdir(parents=True, exist_ok=True)
    return root


STORAGE_TIMEOUT_S = 8.0  # 自备实例离线时 psycopg 池建连会挂 ~30s,页面层止损


async def _pg(fn, *args, **kw):
    """记忆管理页的同步存储调用桥:返回 (结果或 None, 错误摘要)。"""
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kw),
                                      STORAGE_TIMEOUT_S), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


class TaskIn(BaseModel):
    topic: str
    route: str = ""


class RouteIn(BaseModel):
    route: str


class SteerIn(BaseModel):
    text: str


class CandidateIn(BaseModel):
    candidate_id: int


class StyleIn(BaseModel):
    content: str = ""


class RatingIn(BaseModel):
    domain: str
    score: float = 0.5
    note: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    state: WebState | None = getattr(app.state, "deps", None)
    cm = None
    if state is None:  # 生产模式:自装配 SqliteSaver + 队列 + 串行 worker
        cm = open_checkpointer()
        cp = cm.__enter__()
        conn, store = open_job_store()
        graph = build_graph(cp)
        bus = EventBus()
        state = WebState(graph, store, Worker(graph, store, bus), bus, conn,
                         reports_root())
        app.state.deps = state
    if getattr(app.state, "start_worker", True):
        task = asyncio.create_task(state.worker.run_forever())
        app.state.worker_task = task
    try:
        yield
    finally:
        worker = state.worker
        worker.stop()
        task = getattr(app.state, "worker_task", None)
        if task:
            await asyncio.wait_for(task, timeout=5)
        if cm is not None:
            state.conn.close()
            cm.__exit__(None, None, None)


def create_app(state: WebState | None = None, *, start_worker: bool = True) -> FastAPI:
    app = FastAPI(title="deepsearch", version=__version__, lifespan=lifespan)
    app.state.deps = state
    app.state.start_worker = start_worker
    if (FRONTEND_DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")),
                  name="spa-assets")

    def S() -> WebState:
        deps = getattr(app.state, "deps", None)
        if deps is None:  # pragma: no cover - lifespan 已装配
            raise HTTPException(503, "web 层尚未完成装配")
        return deps

    # ---------- ① 选题输入页 ----------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        spa = FRONTEND_DIST / "index.html"
        if spa.is_file():
            return FileResponse(spa)
        s = S()
        jobs = s.store.list_recent(30)
        return templates.TemplateResponse(
            request, "index.html",
            {"version": __version__, "jobs": jobs, "counts": s.store.count_by_status(),
             "now": time.time()},
        )

    @app.post("/tasks")
    async def create_task(topic: str = Form(...), route: str = Form("")) -> RedirectResponse:
        topic = topic.strip()
        if not topic:
            raise HTTPException(422, "请输入选题或问题")
        tid = new_thread_id()
        S().store.enqueue(tid, topic, route)
        return RedirectResponse(f"/threads/{tid}", status_code=303)

    # ---------- ②③④ 任务详情 / 进度面板 / 网关表单 ----------

    @app.get("/threads/{tid}", response_class=HTMLResponse)
    async def thread_page(request: Request, tid: str) -> HTMLResponse:
        s = S()
        job = _active_job(s.store, tid)
        if job is None:
            raise HTTPException(404, f"没有任务 thread={tid}")
        return templates.TemplateResponse(
            request, "thread.html", {"job": job, "thread_id": tid}
        )

    @app.get("/threads/{tid}/panel", response_class=HTMLResponse)
    async def thread_panel(request: Request, tid: str) -> HTMLResponse:
        """HTMX 轮询片段:状态 + 进度 + 网关表单 + 产物链接(整块外替换)。"""
        s = S()
        job = _active_job(s.store, tid)
        if job is None:
            raise HTTPException(404, f"没有任务 thread={tid}")
        values = _snapshot_values(s, tid)
        progress = _progress_with_state(s, job, values)
        return templates.TemplateResponse(
            request, "partials/panel.html",
            {"job": job, "thread_id": tid, "progress": progress, "values": values},
        )

    @app.post("/threads/{tid}/gate")
    async def submit_gate(tid: str, request: Request) -> RedirectResponse:
        s = S()
        job = s.store.waiting_for_thread(tid)
        if job is None or not job.gate:
            raise HTTPException(409, "该任务当前没有待确认的网关")
        raw = await request.form()
        form = {str(k): str(v) for k, v in raw.items()}
        form.setdefault("action", "approve")
        try:
            decision = parse_gate_decision(job.gate, form)
        except ValueError as exc:
            raise HTTPException(422, f"表单解析失败:{exc}") from exc
        s.worker.submit_decision(job.id, decision)
        return RedirectResponse(f"/threads/{tid}", status_code=303)

    @app.post("/threads/{tid}/reroute")
    async def reroute(tid: str, route: str = Form(...)) -> RedirectResponse:
        """低置信分诊人工改档(Q34 web 兜底):用原输入+用户选档重新入队。
        triage 节点尊重预置 route,不会二次模型判定。"""
        s = S()
        if route not in ALL_ROUTES or route == "steering":
            raise HTTPException(422, f"非法档位:{route}")
        job = s.store.latest_for_thread(tid)
        if job is None:
            raise HTTPException(404, f"没有任务 thread={tid}")
        if s.store.thread_has_active(tid):
            raise HTTPException(409, "任务仍在进行,无需改档")
        s.store.enqueue(tid, job.input, route)
        return RedirectResponse(f"/threads/{tid}", status_code=303)

    @app.post("/threads/{tid}/upload")
    async def upload(tid: str, file: UploadFile = File(...)) -> RedirectResponse:
        """上传私有资料:解析入 L2 任务池,doc_id 经 update_state 累加进
        source_ids(坑④:挂起网关的 thread 不可投 input)。"""
        await _ingest_upload(S(), tid, file)
        return RedirectResponse(f"/threads/{tid}", status_code=303)

    @app.post("/threads/{tid}/steer")
    async def steer(tid: str, text: str = Form(...)) -> RedirectResponse:
        """task_steering 投递:同 thread 入一个 steering 任务,下一网关消费。"""
        s = S()
        if s.store.latest_for_thread(tid) is None:
            raise HTTPException(404, f"没有任务 thread={tid}")
        s.store.enqueue(tid, text.strip(), "steering")
        return RedirectResponse(f"/threads/{tid}", status_code=303)

    @app.get("/threads/{tid}/events")
    async def thread_events(request: Request, tid: str) -> EventSourceResponse:
        """SSE 进度流:重放该任务历史事件再跟增量;断连即退订。"""
        s = S()
        job = _active_job(s.store, tid)
        if job is None:
            raise HTTPException(404, f"没有任务 thread={tid}")
        q = s.bus.subscribe(job.id)

        async def gen() -> AsyncIterator[dict[str, str]]:
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield {"event": "ping", "data": "{}"}
                        continue
                    yield {"event": ev.get("type", "info"),
                           "data": json.dumps(ev, ensure_ascii=False)}
                    if ev.get("type") in ("done", "error", "gate"):
                        # 网关/终态后任务行会被 resume 复用为同一 job,
                        # gate 停流让前端靠 panel 轮询继续兜底
                        if ev.get("type") != "gate":
                            break
            finally:
                s.bus.unsubscribe(job.id, q)

        return EventSourceResponse(gen())

    # ---------- 任务重试 / 取消(P7) ----------

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: int) -> RedirectResponse:
        """failed 任务重跑:全新 thread 同输入同档重新入队。"""
        s = S()
        job = s.store.retry(job_id)
        if job is None:
            raise HTTPException(409, "仅失败任务可重试")
        return RedirectResponse(f"/threads/{job.thread_id}", status_code=303)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int) -> RedirectResponse:
        s = S()
        job = s.store.get(job_id)
        if job is None:
            raise HTTPException(404, f"没有 job={job_id}")
        if not s.store.cancel(job_id):
            raise HTTPException(409, "任务已是终态,无需取消")
        s.worker.bus.publish(job_id, {"type": "cancelled", "job_id": job_id})
        return RedirectResponse(f"/threads/{job.thread_id}", status_code=303)

    # ---------- 记忆闭环管理页(P7:定稿 R4/Q39/Q44 人工采纳) ----------

    @app.get("/memory", response_class=HTMLResponse)
    async def memory_page(request: Request) -> HTMLResponse:
        snap = await _memory_snapshot()
        return templates.TemplateResponse(request, "memory.html", snap)

    @app.post("/memory/adopt")
    async def adopt_candidate(candidate_id: int = Form(...)) -> RedirectResponse:
        await _adopt_candidate(candidate_id)
        return RedirectResponse("/memory", status_code=303)

    @app.post("/memory/reject")
    async def reject_candidate(candidate_id: int = Form(...)) -> RedirectResponse:
        await _reject_candidate(candidate_id)
        return RedirectResponse("/memory", status_code=303)

    @app.post("/memory/style")
    async def save_style_card(content: str = Form("")) -> RedirectResponse:
        await _save_style(content)
        return RedirectResponse("/memory", status_code=303)

    @app.post("/memory/rating")
    async def save_rating(domain: str = Form(...), score: float = Form(0.5),
                          note: str = Form("")) -> RedirectResponse:
        await _save_rating(domain, score, note)
        return RedirectResponse("/memory", status_code=303)

    # ---------- ⑤ reports 产物浏览下载 ----------

    @app.get("/reports", response_class=HTMLResponse)
    async def reports(request: Request) -> HTMLResponse:
        listing = _report_listing(S())
        return templates.TemplateResponse(
            request, "reports.html", {"dirs": listing["dirs"], "root": listing["root"]}
        )

    @app.get("/files/{dir_name}/{file_path:path}")
    async def download(dir_name: str, file_path: str,
                       inline: bool = False) -> FileResponse:
        s = S()
        target = (s.reports_root / dir_name / file_path).resolve()
        root = s.reports_root.resolve()
        if root not in target.parents and target != root:
            raise HTTPException(403, "路径越界")
        if not target.is_file() or target.suffix.lower() not in SAFE_SUFFIXES:
            raise HTTPException(404, "文件不存在或类型不允许")
        return FileResponse(target, filename=target.name,
                            content_disposition_type="inline" if inline else "attachment")

    # ---------- 健康探测(P0 起保留,扩展队列计数) ----------

    @app.get("/healthz")
    async def healthz() -> dict:
        settings = get_settings()
        s = getattr(app.state, "deps", None)
        return {
            "version": __version__,
            "configured": {
                "primary_model": bool(settings.primary_api_key),
                "utility_model": bool(settings.utility_api_key),
                "embedding": bool(settings.embedding_api_key),
                "bocha": bool(settings.bocha_api_key),
                "tavily": bool(settings.tavily_api_key),
            },
            "jobs": s.store.count_by_status() if s else {},
        }

    # ---------- JSON API(Vue SPA 前端,与 HTMX 表单端点共享同一 WebState) ----------

    @app.get("/api/overview")
    async def api_overview() -> dict:
        s = S()
        return {"version": __version__, "jobs": s.store.list_recent(30),
                "counts": s.store.count_by_status(), "now": time.time()}

    @app.post("/api/tasks")
    async def api_create_task(body: TaskIn) -> dict:
        topic = body.topic.strip()
        if not topic:
            raise HTTPException(422, "请输入选题或问题")
        if body.route not in ("", "chat", "factual", "report"):
            raise HTTPException(422, f"非法档位:{body.route}")
        tid = new_thread_id()
        job_id = S().store.enqueue(tid, topic, body.route)
        return {"thread_id": tid, "job_id": job_id}

    @app.get("/api/threads/{tid}")
    async def api_thread(tid: str) -> dict:
        s = S()
        job = _active_job(s.store, tid)
        if job is None:
            raise HTTPException(404, f"没有任务 thread={tid}")
        values = _snapshot_values(s, tid)
        return {"job": job, "progress": _progress_with_state(s, job, values)}

    @app.post("/api/threads/{tid}/gate")
    async def api_gate(tid: str, form: dict[str, str]) -> dict:
        s = S()
        job = s.store.waiting_for_thread(tid)
        if job is None or not job.gate:
            raise HTTPException(409, "该任务当前没有待确认的网关")
        form.setdefault("action", "approve")
        try:
            decision = parse_gate_decision(job.gate, form)
        except ValueError as exc:
            raise HTTPException(422, f"表单解析失败:{exc}") from exc
        s.worker.submit_decision(job.id, decision)
        return {"ok": True}

    @app.post("/api/threads/{tid}/reroute")
    async def api_reroute(tid: str, body: RouteIn) -> dict:
        s = S()
        if body.route not in ALL_ROUTES or body.route == "steering":
            raise HTTPException(422, f"非法档位:{body.route}")
        job = s.store.latest_for_thread(tid)
        if job is None:
            raise HTTPException(404, f"没有任务 thread={tid}")
        if s.store.thread_has_active(tid):
            raise HTTPException(409, "任务仍在进行,无需改档")
        s.store.enqueue(tid, job.input, body.route)
        return {"ok": True}

    @app.post("/api/threads/{tid}/steer")
    async def api_steer(tid: str, body: SteerIn) -> dict:
        s = S()
        if s.store.latest_for_thread(tid) is None:
            raise HTTPException(404, f"没有任务 thread={tid}")
        job_id = s.store.enqueue(tid, body.text.strip(), "steering")
        return {"ok": True, "job_id": job_id}

    @app.post("/api/threads/{tid}/upload")
    async def api_upload(tid: str, file: UploadFile = File(...)) -> dict:
        doc = await _ingest_upload(S(), tid, file)
        return {"ok": True, "doc_id": doc.doc_id}

    @app.post("/api/jobs/{job_id}/retry")
    async def api_retry(job_id: int) -> dict:
        job = S().store.retry(job_id)
        if job is None:
            raise HTTPException(409, "仅失败任务可重试")
        return {"ok": True, "thread_id": job.thread_id}

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_cancel(job_id: int) -> dict:
        s = S()
        job = s.store.get(job_id)
        if job is None:
            raise HTTPException(404, f"没有 job={job_id}")
        if not s.store.cancel(job_id):
            raise HTTPException(409, "任务已是终态,无需取消")
        s.worker.bus.publish(job_id, {"type": "cancelled", "job_id": job_id})
        return {"ok": True}

    @app.get("/api/memory")
    async def api_memory() -> dict:
        return await _memory_snapshot()

    @app.post("/api/memory/adopt")
    async def api_adopt(body: CandidateIn) -> dict:
        await _adopt_candidate(body.candidate_id)
        return {"ok": True}

    @app.post("/api/memory/reject")
    async def api_reject(body: CandidateIn) -> dict:
        await _reject_candidate(body.candidate_id)
        return {"ok": True}

    @app.post("/api/memory/style")
    async def api_style(body: StyleIn) -> dict:
        await _save_style(body.content)
        return {"ok": True}

    @app.post("/api/memory/rating")
    async def api_rating(body: RatingIn) -> dict:
        await _save_rating(body.domain, body.score, body.note)
        return {"ok": True}

    @app.get("/api/reports")
    async def api_reports() -> dict:
        return _report_listing(S())

    return app


# ---------- 面板辅助 ----------

async def _ingest_upload(s: WebState, tid: str, file: UploadFile):
    """上传私有资料公共逻辑(HTML 表单与 JSON API 共用),返回入池文档。"""
    if s.store.latest_for_thread(tid) is None:
        raise HTTPException(404, f"没有任务 thread={tid}")
    name = Path(file.filename or "upload.bin").name  # 剪掉路径部分防注入
    if Path(name).suffix.lower() not in ingest.SUPPORTED_SUFFIXES:
        raise HTTPException(422, f"不支持的文件类型:{Path(name).suffix}")
    blob = await file.read()
    max_bytes = get_settings().upload_max_mb * 1024 * 1024
    if len(blob) > max_bytes:
        raise HTTPException(413, f"超过上传上限 {get_settings().upload_max_mb}MB")
    dest_dir = Path(get_settings().data_dir) / "uploads" / tid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(time.time())}-{name}"
    dest.write_bytes(blob)
    try:
        doc = await asyncio.to_thread(ingest.ingest_file, tid, dest)
    except ValueError as exc:
        raise HTTPException(422, f"解析失败:{exc}") from exc
    if doc is None:
        raise HTTPException(422, "文档无正文内容,未入池")
    try:
        s.graph.update_state(
            {"configurable": {"thread_id": tid}},
            {"source_ids": [doc.doc_id]},
            as_node="triage",  # 固定写入源:挂起/已结束都能合法写累加通道
        )
    except Exception as exc:  # 无 checkpoint 等
        raise HTTPException(409, f"无法写入任务状态:{exc}") from exc
    return doc


async def _memory_snapshot() -> dict[str, Any]:
    # 并发拉取:离线时单页只付一次超时,而非 5 段串行的 5 倍
    (pending, err1), (adopted, _), (rejected, _), (card, err2), \
        (ratings, err3) = await asyncio.gather(
            _pg(archive.list_candidates, "pending"),
            _pg(archive.list_candidates, "adopted"),
            _pg(archive.list_candidates, "rejected"),
            _pg(archive.get_style_card),
            _pg(archive.list_source_ratings),
        )
    offline = err1 or err2 or err3
    return {"pending": pending or [], "adopted": (adopted or [])[:20],
            "rejected": (rejected or [])[:20], "style_card": card or "",
            "ratings": ratings or [], "offline": bool(offline),
            "offline_err": offline}


async def _adopt_candidate(candidate_id: int) -> None:
    """采纳:候选→adopted 结构行;profile/fact 同步入 Milvus 向量池
    (向量写失败不阻断采纳,milvus_id 留空可后续重建)。"""
    cand, err = await _pg(_get_candidate_obj, candidate_id)
    if err:
        raise HTTPException(502, f"PG 不可达:{err}")
    if cand is None:
        raise HTTPException(404, f"候选 {candidate_id} 不存在")
    if cand.status != "pending":
        raise HTTPException(409, f"候选已是 {cand.status} 状态")
    milvus_id, _v_err = await _pg(vectors.upsert_memory, cand.kind,
                                  cand.content, f"candidate:{cand.id}")
    await _pg(_finish_adopt, cand, milvus_id)


async def _reject_candidate(candidate_id: int) -> None:
    _, err = await _pg(archive.mark_candidate, candidate_id, "rejected")
    if err:
        raise HTTPException(502, f"PG 不可达:{err}")


async def _save_style(content: str) -> None:
    _, err = await _pg(archive.upsert_style_card, content.strip())
    if err:
        raise HTTPException(502, f"PG 不可达:{err}")


async def _save_rating(domain: str, score: float, note: str) -> None:
    domain = domain.strip().lower()
    if not domain or not (0.0 <= score <= 1.0):
        raise HTTPException(422, "域名非空且 score 在 0~1")
    _, err = await _pg(archive.upsert_source_rating, domain, score, note.strip() or None)
    if err:
        raise HTTPException(502, f"PG 不可达:{err}")


def _report_listing(s: WebState) -> dict[str, Any]:
    root: Path = s.reports_root
    dirs = []
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        if not d.is_dir():
            continue
        files = {p.name.lower().split(".")[-1]: p.name
                 for p in sorted(d.rglob("*")) if p.is_file()}
        dirs.append({"name": d.name, "files": files,
                     "rels": [str(p.relative_to(d)).replace("\\", "/")
                              for p in sorted(d.rglob("*"))
                              if p.is_file()
                              and p.suffix.lower() in SAFE_SUFFIXES]})
    return {"root": str(root), "dirs": dirs}


def _get_candidate_obj(candidate_id: int):
    """按 id 取单条候选(archive 未留单查接口,列表窗口内找)。"""
    for c in archive.list_candidates("pending", limit=200):
        if c.id == candidate_id:
            return c
    return None


def _finish_adopt(cand, milvus_id: str | None) -> None:
    archive.mark_candidate(cand.id, "adopted")
    archive.record_adopted(cand.kind, cand.content, candidate_id=cand.id,
                           milvus_id=milvus_id)


def _snapshot_values(s: WebState, tid: str) -> dict[str, Any]:
    """从 checkpoint 读当前 state 快照(面板进度真相,独立于事件流)。"""
    try:
        snap = s.graph.get_state({"configurable": {"thread_id": tid}})
        return dict(snap.values or {})
    except Exception:
        return {}


def _progress_with_state(s: WebState, job, values: dict[str, Any]) -> dict[str, Any]:
    from deepsearch.web.worker import progress_event

    ev = progress_event(values, job.gate)
    ev["job_id"] = job.id
    ev["status"] = job.status
    ev["error"] = job.error
    ev["answer"] = values.get("answer", "")
    ev["needs_human"] = bool(values.get("needs_human"))
    ev["confidence"] = float(values.get("triage_confidence") or 0.0)
    ev["route"] = values.get("route", "")
    # 分诊复核闸:自动分档且置信偏低才提示改档;人工显式选档(job.route 非空)不骚扰
    from deepsearch.graph.nodes.triage import CONFIDENCE_FLOOR

    ev["low_conf"] = (
        not job.route
        and ev["confidence"] < CONFIDENCE_FLOOR
        and ev["route"] in ("chat", "factual")
        and not values.get("report_topic")
    )
    report_dir = values.get("report_dir") or ""
    ev["report_name"] = Path(report_dir).name if report_dir else ""
    # chat/factual 终态:answer 直出
    ev["is_chat"] = values.get("route") in ("chat", "factual") and bool(values.get("answer"))
    return ev


app = create_app()
