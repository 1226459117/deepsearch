"""P1 记忆子系统 smoke 检查:对用户自备的真实 Redis/PG/Milvus 实例跑通全链路。

用法(需 .env 已配置且实例可达):
    uv run python scripts/smoke_memory.py

检查顺序:PG 迁移表 → Redis 窗口一轮 → Milvus 建集合 → (有 key 时)
embedding 维度 → (有 key 时)utility 档连通。任一步失败即退出并打印原因;
模型 key 未配置时对应步骤跳过,不算失败。
"""

from __future__ import annotations

import sys
import uuid

import redis as redis_lib


def main() -> int:
    from deepsearch.config import get_settings
    from deepsearch.memory import archive, intercept, service, vectors
    from deepsearch.memory.window import ConversationWindow

    settings = get_settings()
    thread = f"smoke-{uuid.uuid4().hex[:8]}"

    # 1. PG:迁移已应用 + 归档可写
    try:
        with archive.get_pool().connection() as conn:
            n_tables = conn.execute(
                "SELECT COUNT(*) AS c FROM information_schema.tables "
                "WHERE table_name IN ('conversation_messages','memory_candidates')"
            ).fetchone()["c"]
        assert n_tables == 2, "请先运行: uv run deepsearch-initdb"
        turn_no = archive.append_turn(thread, "smoke提问", "smoke回答")
        assert turn_no == 1
        print(f"[1/5] PG  OK  归档写入 {thread} turn_no={turn_no}")
    except Exception as exc:
        print(f"[1/5] PG  FAIL  {exc}\n提示:检查 POSTGRES_DSN 并执行 uv run deepsearch-initdb")
        return 1

    # 2. Redis:窗口推入一轮并读回
    try:
        client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        result = service.post_turn(thread, "q", "a", client=client)
        win = ConversationWindow(thread, client=client)
        assert result["turn_no"] == 1 and win.get_turns()[0]["user"] == "q"
        win.drop()  # 清理 smoke 残留(L1 域内自清,不碰 PG 归档)
        print(f"[2/5] Redis  OK  key=dp:{settings.tenant_id}:{settings.user_id}:{thread}")
    except Exception as exc:
        print(f"[2/5] Redis  FAIL  {exc}\n提示:检查 REDIS_URL")
        return 1

    # 3. Milvus:幂等建集合
    try:
        created = vectors.ensure_collections()
        print(f"[3/5] Milvus  OK  集合 {created}")
    except Exception as exc:
        print(f"[3/5] Milvus  FAIL  {exc}\n提示:检查 MILVUS_URI")
        return 1

    # 4. Embedding(未配 key 则跳过)
    if settings.embedding_api_key:
        try:
            vec = vectors.embed_texts(["smoke"])[0]
            assert len(vec) == settings.embedding_dim, f"维度 {len(vec)} != {settings.embedding_dim}"
            print(f"[4/5] Embedding  OK  dim={len(vec)}")
        except Exception as exc:
            print(f"[4/5] Embedding  FAIL  {exc}")
            return 1
    else:
        print("[4/5] Embedding  SKIP  (未配置 EMBEDDING_API_KEY)")

    # 5. 拦截链路降级 + 主档探测(可选)
    ctx = intercept.retrieve("smoke 查询")
    block = intercept.build_injection(ctx)
    print(f"[5/5] Intercept  OK  (空池注入块={len(block)} 字符,降级正常)")
    if settings.primary_api_key:
        try:
            from deepsearch.memory import llm

            r = llm.chat([{"role": "user", "content": "回复:ok"}], tier="utility")
            print(f"      Utility 档 OK  回包 {r.content[:20]!r}")
        except Exception as exc:
            print(f"      Utility 档 FAIL  {exc}")
            return 1
    print(f"\n✅ P1 smoke 全部通过(thread={thread})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
