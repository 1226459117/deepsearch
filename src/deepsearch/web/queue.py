"""SQLite 任务队列(定稿 R7:单机自用工装,同进程串行 worker 消费)。

jobs 表即任务真相:入队 queued → worker 领取 running → 停在 human gate
waiting_human(gate 列存 payload JSON,页面据此渲染确认表单)→ 人工提交
decision 重新入队 → done / failed。同 thread 可多次入队(report 之后追加
steering),thread_id 非唯一,主键用自增 rowid。

独立于 LangGraph checkpoint 的 checkpoints.sqlite:队列只管调度,
工作记忆一律在 checkpoint(R6 双真相源,不交叉)。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JOB_FILENAME = "jobs.sqlite"

QUEUED = "queued"
RUNNING = "running"
WAITING = "waiting_human"
DONE = "done"
FAILED = "failed"

# 终态与可领取状态集合(路由层判断按钮可用性用)
ACTIVE_STATES = (QUEUED, RUNNING, WAITING)


def job_db_path(data_dir: str | None = None) -> Path:
    from deepsearch.config import get_settings

    root = Path(data_dir or get_settings().data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / JOB_FILENAME


@dataclass
class Job:
    id: int
    thread_id: str
    input: str
    route: str
    status: str
    gate: dict[str, Any] | None
    error: str
    created_at: float
    updated_at: float

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATES


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   TEXT   NOT NULL,
    input       TEXT   NOT NULL DEFAULT '',
    route       TEXT   NOT NULL DEFAULT '',
    status      TEXT   NOT NULL DEFAULT 'queued',
    gate        TEXT,
    error       TEXT   NOT NULL DEFAULT '',
    created_at  REAL   NOT NULL,
    updated_at  REAL   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_thread ON jobs (thread_id);
"""


class JobStore:
    """任务队列 DAO。单 worker 串行消费,写全在主线程,无需跨线程锁。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 写 ----

    def enqueue(self, thread_id: str, input_text: str = "", route: str = "") -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO jobs (thread_id, input, route, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, input_text, route, QUEUED, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_next(self) -> Job | None:
        """原子领取最早的 queued 任务置 running;无任务返回 None。"""
        cur = self.conn.execute(
            "UPDATE jobs SET status=?, updated_at=?"
            " WHERE id=(SELECT id FROM jobs WHERE status=? ORDER BY created_at LIMIT 1)"
            " RETURNING id",
            (RUNNING, time.time(), QUEUED),
        )
        row = cur.fetchone()
        self.conn.commit()
        return self.get(int(row[0])) if row else None

    def set_waiting(self, job_id: int, gate: dict[str, Any]) -> None:
        # 状态机防护:已被人工取消(failed)的 job 不再回到 waiting
        self.conn.execute(
            "UPDATE jobs SET status=?, gate=?, updated_at=?"
            " WHERE id=? AND status=?",
            (WAITING, json.dumps(gate, ensure_ascii=False), time.time(), job_id, RUNNING),
        )
        self.conn.commit()

    def set_done(self, job_id: int) -> None:
        self.conn.execute(
            "UPDATE jobs SET status=?, gate=NULL, error='', updated_at=?"
            " WHERE id=? AND status=?",
            (DONE, time.time(), job_id, RUNNING),
        )
        self.conn.commit()

    def set_failed(self, job_id: int, error: str) -> None:
        # WHERE 排除已取消行:cancel 先落"人工取消",稍后的执行异常不覆盖其语境
        self.conn.execute(
            "UPDATE jobs SET status=?, gate=NULL, error=?, updated_at=?"
            " WHERE id=? AND status != ?",
            (FAILED, error[:2000], time.time(), job_id, FAILED),
        )
        self.conn.commit()

    def clear_gate(self, job_id: int) -> None:
        """人工决策已提交、重新入队时清掉旧 gate payload。"""
        self.conn.execute(
            "UPDATE jobs SET gate=NULL, updated_at=? WHERE id=?",
            (time.time(), job_id),
        )
        self.conn.commit()

    def requeue(self, job_id: int) -> None:
        self.conn.execute(
            "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
            (QUEUED, time.time(), job_id),
        )
        self.conn.commit()

    def abort_stuck(self, job_id: int, msg: str = "任务被人工终止") -> None:
        """running/waiting 任务作废(取消或图内 abort 的兜底)。"""
        self.conn.execute(
            "UPDATE jobs SET status=?, gate=NULL, error=?, updated_at=? WHERE id=?",
            (FAILED, msg, time.time(), job_id),
        )
        self.conn.commit()
    
    def cancel(self, job_id: int) -> bool:
        """未终态(queued/running/waiting)任务置 failed。running 中的无法
        中途打断(graph 在节点边界才可中断),本轮超步跑完后不再继续。"""
        cur = self.conn.execute(
            "UPDATE jobs SET status=?, gate=NULL, error=?, updated_at=?"
            " WHERE id=? AND status IN (?,?,?)",
            (FAILED, "任务被人工取消", time.time(), job_id, QUEUED, RUNNING, WAITING),
        )
        self.conn.commit()
        return cur.rowcount > 0
    
    def retry(self, job_id: int) -> "Job | None":
        """failed 任务重跑:同输入同档入一个**全新 thread**。
        基线程剥离历史 -rN 后缀后递增(在 r1 上再重试得到 r2);
        不复用原 thread:旧 checkpoint 残留的章节/素材会污染重跑结果。"""
        import re

        job = self.get(job_id)
        if job is None or job.status != FAILED:
            return None
        base = re.sub(r"-r\d+$", "", job.thread_id)
        seq = 0
        for r in self.conn.execute(
            "SELECT thread_id FROM jobs WHERE thread_id LIKE ?", (base + "-r%",)
        ).fetchall():
            m = re.search(r"-r(\d+)$", r["thread_id"])
            if m:
                seq = max(seq, int(m.group(1)))
        new_id = self.enqueue(f"{base}-r{seq + 1}", job.input, job.route)
        return self.get(new_id)

    # ---- 读 ----

    def get(self, job_id: int) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def latest_for_thread(self, thread_id: str) -> Job | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE thread_id=? ORDER BY updated_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return _row_to_job(row) if row else None

    def waiting_for_thread(self, thread_id: str) -> Job | None:
        """该 thread 当前挂起人工确认的 job(gate 提交目标准则:同 thread
        可能后入队的 steering job 已完成,不能拿 latest 当网关宿主)。"""
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE thread_id=? AND status=?"
            " ORDER BY updated_at DESC LIMIT 1",
            (thread_id, WAITING),
        ).fetchone()
        return _row_to_job(row) if row else None

    def thread_has_active(self, thread_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE thread_id=? AND status IN (?,?,?) LIMIT 1",
            (thread_id, QUEUED, RUNNING, WAITING),
        ).fetchone()
        return row is not None

    def list_recent(self, limit: int = 50) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}


def _row_to_job(row: sqlite3.Row | tuple) -> Job:
    return Job(
        id=row[0],
        thread_id=row[1],
        input=row[2],
        route=row[3],
        status=row[4],
        gate=json.loads(row[5]) if row[5] else None,
        error=row[6] or "",
        created_at=row[7],
        updated_at=row[8],
    )


def open_job_store(data_dir: str | None = None) -> tuple[sqlite3.Connection, JobStore]:
    """建连接 + 建表;调用方负责 close。WAL 与 checkpoint 库同策略。"""
    conn = sqlite3.connect(str(job_db_path(data_dir)), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn, JobStore(conn)
