"""基础设施初始化:`deepsearch-initdb` = PG 迁移 + Milvus 建集合 + FTS 建库 + Redis 探活。

迁移器:按文件名顺序应用 `migrations/*.sql`,已应用的记录在 schema_migrations 跳过。
向量/全文/窗口三层均为幂等操作,可重复执行;单层不可达只告警不阻断其余层
(单机自用允许部分存储缺位,web 链路仅依赖本地 SQLite)。
用法:`uv run deepsearch-initdb`
"""

from __future__ import annotations

from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def apply_migrations(dsn: str) -> list[str]:
    """应用全部未执行迁移,返回本次应用的文件名列表。"""
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.execute(BOOTSTRAP_SQL)
        done = {
            row[0]
            for row in conn.execute("SELECT name FROM schema_migrations").fetchall()
        }
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (name) VALUES (%s)", [path.name]
            )
            applied.append(path.name)
        conn.commit()
    return applied


def init_all() -> dict[str, str]:
    """四层存储逐层初始化,返回 {层名: ok/错误摘要};单层失败不阻断其它层。"""
    from deepsearch.config import get_settings

    settings = get_settings()
    report: dict[str, str] = {}

    try:
        applied = apply_migrations(settings.postgres_dsn)
        report["postgres"] = "ok(应用迁移:" + (", ".join(applied) or "无待应用") + ")"
    except Exception as exc:
        report["postgres"] = f"FAIL {exc}"

    try:
        from deepsearch.memory import vectors

        cols = vectors.ensure_collections()
        report["milvus"] = "ok(集合:" + ", ".join(cols) + ")"
    except Exception as exc:
        report["milvus"] = f"FAIL {exc}"

    try:
        import redis as redis_lib

        client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        report["redis"] = "ok(PING)"
    except Exception as exc:
        report["redis"] = f"FAIL {exc}"

    try:
        from deepsearch.tools import fts

        tok = fts.ensure_schema()
        report["fts"] = f"ok(分词器:{tok})"
    except Exception as exc:
        report["fts"] = f"FAIL {exc}"

    return report


def main() -> None:
    report = init_all()
    for layer, status in report.items():
        print(f"[initdb] {layer:<10} {status}")
    if any(s.startswith("FAIL") for s in report.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
