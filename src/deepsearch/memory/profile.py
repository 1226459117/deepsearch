"""L3 记忆治理:蒸馏产候选 → 人工采纳/驳回 → 入检索池。

定稿信任模型(R44 双库切分):
- fact(事实归档):蒸馏即自动生效——直接进 adopted + Milvus,无需人审;
- profile(偏好画像):只产 pending 候选,必须在记忆管理页人工采纳后
  才进入检索池。蒸馏失败静默降级(候选可有可无,不阻塞主流程)。
"""

from __future__ import annotations

from deepsearch.memory import archive, llm, vectors

DISTILL_SYSTEM = (
    "你是记忆蒸馏器。从给定材料中提取值得跨任务长期记住的信息,"
    "分两类逐行输出,每行以 FACT: 或 PROFILE: 开头。"
    "FACT 为客观结论/数据口径/信源事实;PROFILE 为用户表现出的偏好"
    "(选题角度、写作风格、行业关注、评价口径)。"
    "只提取明确成立的信息,禁止推测;没有就只输出 NONE。"
)


def distill(
    text: str,
    source_type: str,
    source_ref: str,
    *,
    max_items: int = 5,
) -> dict[str, list[int]]:
    """utility 档蒸馏材料 → 候选。fact 自动采纳入库,Milvus 双向量入池;
    profile 仅入 pending 队列。返回 {'facts': [adopted_id...], 'profiles': [candidate_id...]}。
    """
    result = llm.chat(
        [
            {"role": "system", "content": DISTILL_SYSTEM},
            {"role": "user", "content": text[:8000]},
        ],
        tier="utility",
        temperature=0.1,
    )
    facts: list[int] = []
    profiles: list[int] = []
    for line in result.content.splitlines():
        line = line.strip()
        if line in ("", "NONE"):
            continue
        if line.startswith("FACT:"):
            content = line[5:].strip()
            if not content or len(facts) >= max_items:
                continue
            candidate_id = archive.add_candidate("fact", content, source_type, source_ref)
            milvus_id = vectors.upsert_memory("fact", content, source_ref)
            facts.append(
                archive.record_adopted("fact", content, candidate_id, milvus_id)
            )
        elif line.startswith("PROFILE:"):
            content = line[8:].strip()
            if not content or len(profiles) >= max_items:
                continue
            profiles.append(
                archive.add_candidate("profile", content, source_type, source_ref)
            )
    return {"facts": facts, "profiles": profiles}


def adopt(candidate_id: int) -> int:
    """人工采纳候选:pending→adopted,结构化行+向量同时入池。返回 adopted 行 id。"""
    candidate = _get_pending(candidate_id)
    if candidate.kind != "profile":
        raise ValueError(f"候选 {candidate_id} 是 {candidate.kind},fact 无需人审")
    milvus_id = vectors.upsert_memory("profile", candidate.content, candidate.source_ref or "")
    adopted_id = archive.record_adopted(
        "profile", candidate.content, candidate_id, milvus_id
    )
    archive.mark_candidate(candidate_id, "adopted")
    return adopted_id


def reject(candidate_id: int) -> None:
    """人工驳回:不入检索池,候选行保留可追溯。"""
    _get_pending(candidate_id)
    archive.mark_candidate(candidate_id, "rejected")


def pending_profiles(limit: int = 50) -> list[archive.Candidate]:
    """记忆管理页待审列表。"""
    return archive.list_candidates("pending", limit=limit)


def _get_pending(candidate_id: int) -> archive.Candidate:
    from psycopg.rows import dict_row  # 局部导入,复用 archive 连接池

    with archive.get_pool().connection() as conn:
        conn.row_factory = dict_row
        row = conn.execute(
            "SELECT * FROM memory_candidates WHERE id=%s", (candidate_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"候选 {candidate_id} 不存在")
    candidate = archive._row_to_candidate(row)
    if candidate.status != "pending":
        raise ValueError(f"候选 {candidate_id} 状态为 {candidate.status},不可再审")
    return candidate
