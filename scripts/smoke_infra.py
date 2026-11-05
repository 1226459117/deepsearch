"""P6 基础设施 smoke:Redis / PG / Milvus / embedding 真实连通与最小往返。

前提:.env 已配好用户自备实例(先跑过 `uv run deepsearch-initdb`)。
验证内容(全部用 smoke-<ts> 专有键位,结束即清,不污染真实记忆):
1. initdb 四层初始化报告;
2. L1 Redis:窗口 push/context/drop 往返;
3. L3 PG:append_turn 归档 + 记忆候选 add/list/mark 往返;
4. L3 Milvus + Dashscope embedding:fact 向量 upsert → 语义检索命中;
5. L2 checkpoint:SqliteSaver 开库写读快照。

用法:`uv run python scripts/smoke_infra.py`(任一层 FAIL 退出码 1)
"""

from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 控制台兼容
sys.path.insert(0, "src")

FAILS: list[str] = []


def check(name: str, fn) -> None:
    try:
        detail = fn()
        print(f"[smoke-infra] PASS {name}" + (f" — {detail}" if detail else ""))
    except Exception as exc:
        FAILS.append(name)
        print(f"[smoke-infra] FAIL {name} — {type(exc).__name__}: {exc}")


def main() -> int:
    from deepsearch.config import get_settings
    from deepsearch.storage.init_db import init_all

    settings = get_settings()
    stamp = int(time.time())
    thread = f"smoke-{stamp}"

    print("[smoke-infra] ① initdb 分层初始化:")
    for layer, status in init_all().items():
        print(f"    {layer:<10} {status}")
        if status.startswith("FAIL"):
            FAILS.append(f"initdb:{layer}")
    if FAILS:
        print("[smoke-infra] 存储不可达,后续往返无意义;请先修 .env/实例连接。")
        return 1

    def redis_window():
        from deepsearch.memory.window import ConversationWindow

        w = ConversationWindow(thread)
        w.push_turn("smoke 用户提问", "smoke 助手回复")
        assert w.turn_count() == 1 and w.build_context()[-1]["content"] == "smoke 助手回复"
        w.drop()
        assert w.turn_count() == 0
        return "push/context/drop 往返 ok"

    def pg_roundtrip():
        import psycopg

        from deepsearch.memory import archive

        turn = archive.append_turn(thread, "smoke 问", "smoke 答")
        cid = archive.add_candidate("fact", f"smoke 事实 {stamp}", "manual", thread)
        got = [c for c in archive.list_candidates("pending", limit=200) if c.id == cid]
        assert got and got[0].content == f"smoke 事实 {stamp}"
        archive.mark_candidate(cid, "adopted")
        # 清理 smoke 行(测试数据不留库)
        with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM conversation_messages WHERE thread_id=%s", (thread,))
            conn.execute("DELETE FROM memory_candidates WHERE id=%s", (cid,))
        return f"append_turn(turn={turn}) + 候选采纳往返 ok"

    def milvus_semantic():
        from deepsearch.memory import vectors

        vectors.ensure_collections()
        text = f"编号{stamp}的测试事实:用户长期关注光伏产业链的产能与价格周期"
        vid = vectors.upsert_memory("fact", text, source_ref=thread)
        hits = vectors.search(vectors.MEMORY_COLLECTION, "光伏产能价格周期", kind="fact", limit=3)
        assert any(h.id == vid for h in hits), f"刚写入的向量未被检索命中: {hits}"
        vectors.get_client().delete(collection_name=vectors.MEMORY_COLLECTION, ids=[vid])
        return f"upsert→语义命中({len(hits)} hit)→删向量 ok"

    def checkpoint_sqlite():
        from deepsearch.graph.checkpoint import open_checkpointer

        with open_checkpointer() as cp:
            tables = {
                r[0] for r in cp.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "checkpoints" in tables, f"缺表: {sorted(tables)}"
        return "SqliteSaver 开库 + checkpoints 表就绪 ok"

    print("[smoke-infra] ② 三域记忆真实往返:")
    check("L1 redis.window", redis_window)
    check("L3 pg.archive", pg_roundtrip)
    check("L3 milvus.vectors+embedding", milvus_semantic)
    check("L2 checkpoint.sqlite", checkpoint_sqlite)

    if FAILS:
        print(f"[smoke-infra] 共 {len(FAILS)} 项失败: {', '.join(FAILS)}")
        return 1
    print("[smoke-infra] 全部通过 ✔(Redis/PG/Milvus/embedding/checkpoint 连通)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
