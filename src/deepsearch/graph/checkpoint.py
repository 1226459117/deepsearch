"""SQLite checkpoint 装配(定稿 Q16 / R6:L2 研报工作记忆的唯一真相源)。

checkpointer 落 `{data_dir}/ checkpoints.sqlite`:每完成一个节点即持久化
state 快照,进程重启用同 thread_id 恢复续跑。chat 不走此路(Redis 唯一源),
故只有研报主线节点的状态进 checkpoint。

WAL + check_same_thread=False:单进程 asyncio worker 串行消费,但仍可能跨
线程调用;WAL 让读写并发更稳。测试用 MemorySaver,无需磁盘。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from deepsearch.config import get_settings

CHECKPOINT_FILENAME = "checkpoints.sqlite"


def checkpoint_db_path(data_dir: str | None = None) -> Path:
    root = Path(data_dir or get_settings().data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / CHECKPOINT_FILENAME


@contextmanager
def open_checkpointer(data_dir: str | None = None) -> Iterator[SqliteSaver]:
    """打开持久 checkpointer;退出时关连接。适合应用生命周期内长期持有。"""
    path = checkpoint_db_path(data_dir)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield SqliteSaver(conn)
    finally:
        conn.close()


def memory_checkpointer() -> BaseCheckpointSaver:
    """内存 checkpointer:离线单测 / 无盘环境下用,行为与 SqliteSaver 一致。"""
    return MemorySaver()
