"""P7 联调:intercept 三个注入点在真实 Redis/PG/Milvus 下的端到端命中验证。

链路:候选入库 → 采纳(adopted 行 + Milvus 向量)→ injection_for_node 在
triage / plan_queries / chapter_write 三个槽位分别命中画像、注入块、风格卡。
这验证的是"记忆闭环管理页采纳 → 写作链路真正吃到记忆"的最后一公里。

存储不可达时逐项 SKIP(退出码 0):研报主链路对降级免疫,本脚本只测增强面。
用法:`uv run python scripts/smoke_intercept.py`
"""

from __future__ import annotations

import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, "src")


def main() -> int:
    from deepsearch.config import get_settings
    from deepsearch.storage.init_db import init_all

    settings = get_settings()
    report = init_all()
    needed = ("postgres", "milvus", "redis")
    down = [k for k in needed if not report[k].startswith("ok")]
    if down:
        print(f"[smoke-intercept] SKIP:{'、'.join(down)} 不可达,联调需自备实例在线。")
        return 0

    ts = int(time.time())
    thread = f"smoke-int-{ts}"
    profile_text = f"编号{ts}画像:用户长期关注新能源产业链的储能与光伏环节"
    ok = True

    import psycopg

    from deepsearch.memory import archive, intercept, vectors
    from deepsearch.memory.vectors import MEMORY_COLLECTION

    old_card = archive.get_style_card()
    cand_id = adopted_id = None
    milvus_id = None
    try:
        # 1) 候选 → 采纳(与 /memory/adopt 路由同一调用序)
        cand_id = archive.add_candidate("profile", profile_text, "manual", thread)
        archive.mark_candidate(cand_id, "adopted")
        milvus_id = vectors.upsert_memory("profile", profile_text, f"candidate:{cand_id}")
        adopted_id = archive.record_adopted("profile", profile_text,
                                            candidate_id=cand_id, milvus_id=milvus_id)
        print(f"[smoke-intercept] PASS 采纳链路 candidate={cand_id} milvus={milvus_id[:8]}…")

        # 2) triage 槽位:只注入画像信号
        hint = intercept.injection_for_node("triage", "新能源储能行业怎么看")
        assert "储能" in hint, f"triage 注入未命中画像: {hint!r}"
        print("[smoke-intercept] PASS triage 注入命中画像")

        # 3) plan_queries 槽位:完整注入块(〈长期记忆〉结构)
        block = intercept.injection_for_node("plan_queries", "光伏产业链调研")
        assert "〈长期记忆〉" in block and "储能" in block, block
        print("[smoke-intercept] PASS plan_queries 注入块结构正确")

        # 4) chapter_write 槽位:仅风格卡
        archive.upsert_style_card(f"smoke风格{ts}:结论先行少形容词")
        inject = intercept.injection_for_node("chapter_write", "第一章 行业格局")
        assert f"smoke风格{ts}" in inject, f"chapter_write 未注入风格卡: {inject!r}"
        print("[smoke-intercept] PASS chapter_write 注入风格卡")

        # 5) token 预算:注入块不超 settings.inject_max_tokens 的粗估上限
        big = intercept.build_injection(
            intercept.MemoryContext(
                query="x",
                facts=[vectors.VectorHit(id=str(i), kind="fact", text="长" * 300, score=0.9 - i * 0.01)
                       for i in range(20)]))
        est = intercept._estimate_tokens(big)
        assert est <= settings.inject_max_tokens + 60, f"注入超预算: {est}"
        print(f"[smoke-intercept] PASS 注入预算裁剪({est} ≤ {settings.inject_max_tokens})")
    except Exception as exc:
        ok = False
        print(f"[smoke-intercept] FAIL {type(exc).__name__}: {exc}")
    finally:
        # 清理:不留 smoke 数据,风格卡还原
        try:
            with psycopg.connect(settings.postgres_dsn, autocommit=True) as conn:
                conn.execute("DELETE FROM memory_candidates WHERE id=%s", (cand_id,))
                conn.execute("DELETE FROM adopted_memories WHERE id=%s", (adopted_id,))
                conn.execute(
                    "INSERT INTO style_cards (tenant_id, user_id, content, updated_at)"
                    " VALUES (%s, %s, %s, now()) ON CONFLICT (tenant_id, user_id)"
                    " DO UPDATE SET content=EXCLUDED.content",
                    (settings.tenant_id, settings.user_id, old_card or ""))
            if milvus_id:
                vectors.get_client().delete(collection_name=MEMORY_COLLECTION,
                                            ids=[milvus_id])
            print("[smoke-intercept] 清理完成(候选/adopted/向量/风格卡还原)")
        except Exception as exc:
            print(f"[smoke-intercept] WARN 清理失败(手工核对 {thread}): {exc}")

    print("[smoke-intercept] 全部通过 ✔" if ok else "[smoke-intercept] 存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
