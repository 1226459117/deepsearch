"""L1 归档 + L3 结构化记忆的 PG 读写层(定稿 R43/R44)。

职责边界:PG 是对话全量原文、滚动摘要行、记忆候选队列、已采纳记忆的
唯一结构化真相源;向量本体在 Milvus,活跃窗口在 Redis,本模块都不碰。
所有函数同步(psyycopg 连接池),Web 层如需可经 asyncio.to_thread 包装。
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from deepsearch.config import get_settings


@functools.lru_cache(maxsize=1)
def get_pool() -> ConnectionPool:
    settings = get_settings()
    pool = ConnectionPool(
        conninfo=settings.postgres_dsn,
        min_size=1,
        max_size=5,
        timeout=5,  # 存储离线时快速失败(service 门面告警降级,不拖挂节点)
        kwargs={"row_factory": dict_row, "autocommit": True, "connect_timeout": 3},
    )
    return pool


@dataclass(frozen=True)
class Candidate:
    id: int
    tenant_id: str
    user_id: str
    kind: str  # fact / profile
    source_type: str  # task_end / cp2_diff / chat_digest / manual
    source_ref: str | None
    content: str
    status: str  # pending / adopted / rejected
    created_at: datetime
    reviewed_at: datetime | None


def append_turn(
    thread_id: str,
    user_content: str,
    assistant_content: str,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> int:
    """归档一轮对话(user+assistant 两条),返回本轮 turn_no。"""
    settings = get_settings()
    tenant_id = tenant_id or settings.tenant_id
    user_id = user_id or settings.user_id
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(turn_no), 0) + 1 AS next_no
            FROM conversation_messages
            WHERE tenant_id=%s AND user_id=%s AND thread_id=%s
            """,
            (tenant_id, user_id, thread_id),
        ).fetchone()
        turn_no: int = row["next_no"]  # type: ignore[index]
        conn.execute(
            """
            INSERT INTO conversation_messages
                (tenant_id, user_id, thread_id, turn_no, role, content)
            VALUES (%s, %s, %s, %s, 'user', %s),
                   (%s, %s, %s, %s, 'assistant', %s)
            ON CONFLICT (tenant_id, user_id, thread_id, turn_no) DO NOTHING
            """,
            (
                tenant_id, user_id, thread_id, turn_no, user_content,
                tenant_id, user_id, thread_id, turn_no, assistant_content,
            ),
        )
    return turn_no


def record_summary(
    thread_id: str,
    from_turn: int,
    to_turn: int,
    summary: str,
    milvus_id: str | None = None,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """落一行滚动摘要(向量本体在 Milvus,此处存文本与引用)。"""
    settings = get_settings()
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO conversation_summaries
                (tenant_id, user_id, thread_id, from_turn, to_turn, summary, milvus_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id or settings.tenant_id,
                user_id or settings.user_id,
                thread_id,
                from_turn,
                to_turn,
                summary,
                milvus_id,
            ),
        )


def add_candidate(
    kind: str,
    content: str,
    source_type: str,
    source_ref: str | None = None,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> int:
    """写入记忆候选队列(profile 类必须经人工采纳才进检索池)。返回候选 id。"""
    settings = get_settings()
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO memory_candidates
                (tenant_id, user_id, kind, source_type, source_ref, content)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id or settings.tenant_id,
                user_id or settings.user_id,
                kind,
                source_type,
                source_ref,
                content,
            ),
        ).fetchone()
        return int(row["id"])  # type: ignore[index, return-value]


def list_candidates(
    status: str = "pending",
    *,
    limit: int = 50,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> list[Candidate]:
    """按状态列候选(记忆管理页用)。"""
    settings = get_settings()
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM memory_candidates
            WHERE tenant_id=%s AND user_id=%s AND status=%s
            ORDER BY id DESC LIMIT %s
            """,
            (tenant_id or settings.tenant_id, user_id or settings.user_id, status, limit),
        ).fetchall()
    return [_row_to_candidate(r) for r in rows]  # type: ignore[arg-type]


def mark_candidate(
    candidate_id: int, status: str, *, reviewed: bool = True
) -> None:
    """更新候选状态 pending→adopted/rejected。"""
    if status not in ("pending", "adopted", "rejected"):
        raise ValueError(f"非法状态: {status}")
    with get_pool().connection() as conn:
        conn.execute(
            """
            UPDATE memory_candidates
            SET status=%s, reviewed_at=%s WHERE id=%s
            """,
            (status, datetime.now(timezone.utc) if reviewed else None, candidate_id),
        )


def record_adopted(
    kind: str,
    content: str,
    candidate_id: int | None = None,
    milvus_id: str | None = None,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> int:
    """已采纳记忆落结构行(向量由 vectors.py 负责)。返回 adopted 行 id。"""
    settings = get_settings()
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO adopted_memories
                (tenant_id, user_id, kind, content, milvus_id, candidate_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                tenant_id or settings.tenant_id,
                user_id or settings.user_id,
                kind,
                content,
                milvus_id,
                candidate_id,
            ),
        ).fetchone()
        return int(row["id"])  # type: ignore[index, return-value]


def upsert_style_card(content: str) -> None:
    """风格卡单行覆盖(plan_queries/chapter_write 注入点消费)。"""
    settings = get_settings()
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO style_cards (tenant_id, user_id, content, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (tenant_id, user_id)
            DO UPDATE SET content=EXCLUDED.content, updated_at=now()
            """,
            (settings.tenant_id, settings.user_id, content),
        )


def get_style_card() -> str | None:
    settings = get_settings()
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT content FROM style_cards WHERE tenant_id=%s AND user_id=%s",
            (settings.tenant_id, settings.user_id),
        ).fetchone()
    return row["content"] if row else None  # type: ignore[index, return-value]


def upsert_source_rating(domain: str, score: float, note: str | None = None) -> None:
    """数据源可靠性域名级轻评分(Q38-C)。"""
    settings = get_settings()
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO source_ratings (tenant_id, domain, score, note, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (tenant_id, domain)
            DO UPDATE SET score=EXCLUDED.score, note=EXCLUDED.note, updated_at=now()
            """,
            (settings.tenant_id, domain, score, note),
        )


def list_source_ratings() -> list[dict[str, Any]]:
    """域名级评分列表(记忆管理页展示,低分域名在 merge 降权用)。"""
    settings = get_settings()
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT domain, score, note, updated_at FROM source_ratings
            WHERE tenant_id=%s ORDER BY score ASC, domain
            """,
            (settings.tenant_id,),
        ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[arg-type]


def _row_to_candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(
        id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        kind=row["kind"],
        source_type=row["source_type"],
        source_ref=row["source_ref"],
        content=row["content"],
        status=row["status"],
        created_at=row["created_at"],
        reviewed_at=row["reviewed_at"],
    )
