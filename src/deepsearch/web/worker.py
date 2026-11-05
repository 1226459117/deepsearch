"""同进程串行 worker(定稿 R7):asyncio 循环消费 jobs 队列,graph.stream
逐超步发布进度事件(SSE 流式展示的数据源);停在 interrupt 时把 gate
payload 落进队列等人工表单提交 resume 决策。

线程模型:graph.stream 是同步迭代,放 asyncio.to_thread 执行;事件发布经
loop.call_soon_threadsafe 回主 loop,订阅者(asyncio.Queue)只在主 loop 读。
同一时刻只跑一个 job(串行),避免自建并发。
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from collections import deque
from typing import Any

from langgraph.types import Command

from deepsearch.graph.nodes.steering import _ack, _contains_stop
from deepsearch.graph.state import new_state
from deepsearch.web.queue import Job, JobStore


class EventBus:
    """每 job 一个环形历史 + 多订阅队列。SSE 新连接先重放历史再跟增量。"""

    HISTORY = 200

    def __init__(self) -> None:
        self._hist: dict[int, deque[dict[str, Any]]] = {}
        self._subs: dict[int, list[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """worker 启动时捕获主 event loop,供工作线程回调发布。"""
        self._loop = loop

    def publish(self, job_id: int, event: dict[str, Any]) -> None:
        self._hist.setdefault(job_id, deque(maxlen=self.HISTORY)).append(event)
        loop = self._loop
        if loop is None:  # 无 loop(同步测试/worker 未启动):只留历史
            for q in self._subs.get(job_id, []):
                q.put_nowait(event)
            return
        if loop.is_running():
            try:
                running_on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                running_on_loop = False
            if running_on_loop:
                self._deliver(job_id, event)
            else:
                loop.call_soon_threadsafe(self._deliver, job_id, event)
        # loop 未绑定/已停止:事件仅存历史,新订阅靠 replay 补齐

    def _deliver(self, job_id: int, event: dict[str, Any]) -> None:
        for q in self._subs.get(job_id, []):
            q.put_nowait(event)

    def subscribe(self, job_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for ev in self._hist.get(job_id, []):
            q.put_nowait(ev)
        self._subs.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: int, q: asyncio.Queue) -> None:
        subs = self._subs.get(job_id, [])
        if q in subs:
            subs.remove(q)

    def history(self, job_id: int) -> list[dict[str, Any]]:
        return list(self._hist.get(job_id, []))


def stage_of(values: dict[str, Any]) -> str:
    """从 state 快照推阶段文案(进度面板用,纯函数)。"""
    if values.get("report_dir"):
        return "已装配导出"
    outline = values.get("outline") or []
    chapters = values.get("chapters") or {}
    if outline:
        if len(chapters) < len(outline):
            return f"撰写中 {len(chapters)}/{len(outline)} 章"
        if values.get("dirty_chapters"):
            return f"审稿重写中(第 {values.get('review_rounds', 0)} 轮)"
        return f"审稿完成({values.get('review_rounds', 0)} 轮)"
    if values.get("research_plan"):
        rounds = values.get("coverage_rounds", 0)
        gaps = values.get("coverage_gaps") or []
        return f"调研中(计划 {len(values['research_plan'])} 问,覆盖复核 {rounds} 轮,缺口 {len(gaps)})"
    if values.get("answer"):
        return "已结束"
    return "启动"


def progress_event(values: dict[str, Any], gate: dict | None = None) -> dict[str, Any]:
    """把 state 快照压成轻量进度事件(绝不整传 chapters 正文)。"""
    return {
        "type": "progress",
        "stage": stage_of(values),
        "route": values.get("route", ""),
        "topic": values.get("report_topic", ""),
        "plan_n": len(values.get("research_plan") or []),
        "sources": len(values.get("source_ids") or []),
        "outline_n": len(values.get("outline") or []),
        "chapters_done": len(values.get("chapters") or {}),
        "review_rounds": values.get("review_rounds", 0),
        "dirty": list(values.get("dirty_chapters") or []),
        "cost_cny": round(float(values.get("cost_accrued_cny", 0.0)), 4),
        "cost_limit_cny": float(values.get("cost_limit_cny") or 0.0),
        "search_used": values.get("search_calls_used", 0),
        "directives_open": len(values.get("pending_directives") or [])
        - int(values.get("consumed_directives", 0)),
        "report_dir": values.get("report_dir", ""),
        "gate": (gate or {}).get("gate"),
    }


class Worker:
    """串行消费队列的执行器。run_forever 用于生产;run_once 用于测试/手动驱动。"""

    def __init__(self, graph, store: JobStore, bus: EventBus | None = None, *,
                 idle_sleep: float = 0.5):
        self.graph = graph
        self.store = store
        self.bus = bus or EventBus()
        self.idle_sleep = idle_sleep
        self._stop = threading.Event()
        # resume 决策暂存:job_id -> decision dict(worker 线程读)
        self._decisions: dict[int, dict[str, Any]] = {}

    # ---- 生产入口 ----

    async def run_forever(self) -> None:
        self.bus.bind_loop(asyncio.get_running_loop())
        while not self._stop.is_set():
            job = self.store.claim_next()
            if job is None:
                await asyncio.sleep(self.idle_sleep)
                continue
            try:
                await asyncio.to_thread(self.execute, job)
            except Exception:  # 兜底:执行器自身炸也不停循环
                self.store.set_failed(job.id, traceback.format_exc()[-2000:])

    def stop(self) -> None:
        self._stop.set()

    # ---- 同步执行体(to_thread 目标;测试可直接调 run_once) ----

    def run_once(self) -> Job | None:
        """同步领取并执行一个任务;无任务返回 None。测试与 CLI 驱动用。"""
        job = self.store.claim_next()
        if job is None:
            return None
        self.execute(job)
        return self.store.get(job.id)

    def submit_decision(self, job_id: int, decision: dict[str, Any]) -> None:
        """web 表单提交网关决策;重新入队由 worker 以 Command(resume) 恢复。"""
        assert decision, "resume 决策必须是非空 dict(P4 坑:空 dict 不恢复网关)"
        self._decisions[job_id] = decision
        self.store.clear_gate(job_id)
        self.store.requeue(job_id)
        self.bus.publish(job_id, {"type": "resumed"})

    def execute(self, job: Job) -> None:
        bid = job.id
        config = {"configurable": {"thread_id": job.thread_id}}
        decision = self._decisions.pop(bid, None)
        if decision is not None:
            payload: Any = Command(resume=decision)
        elif job.route == "steering":
            self._apply_steering(job)
            return
        else:
            state = new_state(job.thread_id, job.input)
            if job.route:
                state["route"] = job.route
            payload = state

        gate: dict | None = None
        last_values: dict[str, Any] = {}
        try:
            for chunk in self.graph.stream(payload, config, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    intr = chunk["__interrupt__"]
                    first = intr[0] if isinstance(intr, (list, tuple)) else intr
                    gate = getattr(first, "value", first)
                    self.bus.publish(bid, {"type": "gate", "gate": gate})
                    continue
                for node, upd in chunk.items():
                    if isinstance(upd, dict):
                        for k, v in upd.items():
                            if isinstance(v, (list, dict, str, int, float, bool)):
                                last_values[k] = v
                        self.bus.publish(
                            bid,
                            {
                                "type": "node",
                                "node": node,
                                "progress": progress_event(last_values, gate),
                            },
                        )
            if gate is not None:
                self.store.set_waiting(bid, gate)
                return
            final = self.graph.get_state(config)
            values = dict(final.values or {})
            self.store.set_done(bid)
            self.bus.publish(
                bid,
                {"type": "done", "progress": progress_event(values),
                 "answer": values.get("answer", "")},
            )
        except Exception:
            err = traceback.format_exc()
            self.store.set_failed(bid, err)
            self.bus.publish(bid, {"type": "error", "error": err[-800:]})
            raise

    def _apply_steering(self, job: Job) -> None:
        """steering 不走图执行:实测 interrupt 挂起时普通 input 会顶掉 pending
        网关(next 变空、resume 失效),而 update_state 只写累加通道、网关存活,
        指令在下一个 human gate 被消费 —— 正是定稿 R46 的不抢占语义。"""
        text = (job.input or "").strip()
        is_stop = _contains_stop(text)
        update: dict[str, Any] = {
            "pending_directives": [{"text": text, "is_stop": is_stop}],
            "answer": _ack(text, is_stop),
            "needs_human": False,
        }
        if is_stop:
            update["stop_requested"] = True
        try:
            self.graph.update_state(
                {"configurable": {"thread_id": job.thread_id}}, update
            )
            self.store.set_done(job.id)
            self.bus.publish(job.id, {"type": "steered", "text": text})
        except Exception:
            err = traceback.format_exc()
            self.store.set_failed(job.id, err)
            self.bus.publish(job.id, {"type": "error", "error": err[-800:]})
            raise


# ---- 应用级装配 ----

class WebState:
    """create_app 挂到 app.state 的依赖袋(测试可整体替换)。"""

    def __init__(self, graph, store: JobStore, worker: Worker, bus: EventBus,
                 conn, reports_root):
        self.graph = graph
        self.store = store
        self.worker = worker
        self.bus = bus
        self.conn = conn
        self.reports_root = reports_root


def build_worker(graph, store: JobStore, bus: EventBus) -> Worker:
    return Worker(graph, store, bus)


__all__ = [
    "EventBus", "Job", "JobStore", "WebState", "Worker",
    "build_worker", "progress_event", "stage_of",
]
