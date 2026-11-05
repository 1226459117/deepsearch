"""Milvus L3 向量层:memory(fact/profile 双类别)+ conversations(滚动摘要)。

定稿口径:embedding=Dashscope text-embedding-v3(dim=1024, COSINE)。
集合按 metadata 过滤 tenant_id/user_id 保留多租户位(R42)。
向量本体只存 Milvus;结构化行(PG)持有本层返回的 milvus_id 做反向引用。
"""

from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass
from typing import Any

from pymilvus import MilvusClient

from deepsearch.config import get_settings
from deepsearch.memory.llm import embed_texts

MEMORY_COLLECTION = "ds_memory"  # kind: fact / profile
CONVERSATION_COLLECTION = "ds_conversations"  # 滚动摘要 embedding


@functools.lru_cache(maxsize=1)
def get_client() -> MilvusClient:
    settings = get_settings()
    return MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token or None)


@dataclass(frozen=True)
class VectorHit:
    id: str
    kind: str
    text: str
    score: float
    source_ref: str = ""


def _ensure_collection(name: str, extra_fields: list[dict[str, str]]) -> None:
    """幂等建集合:id(varchar PK)+ vector + 通用 metadata + 专属字段。"""
    client = get_client()
    if client.has_collection(name):
        return
    schema = {
        "collection_name": name,
        "auto_id": False,
        "fields": [
            {"field_name": "id", "datatype": "VarChar", "is_primary": True, "max_length": 64},
            {
                "field_name": "vector",
                "datatype": "FloatVector",
                "dim": get_settings().embedding_dim,
            },
            {"field_name": "tenant_id", "datatype": "VarChar", "max_length": 64},
            {"field_name": "user_id", "datatype": "VarChar", "max_length": 64},
            {"field_name": "text", "datatype": "VarChar", "max_length": 8192},
            *extra_fields,
        ],
    }
    client.create_collection(**schema)


def ensure_collections() -> list[str]:
    """启动/迁移时调用,返回保证存在的集合名列表。"""
    _ensure_collection(
        MEMORY_COLLECTION,
        [
            {"field_name": "kind", "datatype": "VarChar", "max_length": 16},
            {"field_name": "source_ref", "datatype": "VarChar", "max_length": 256},
        ],
    )
    _ensure_collection(
        CONVERSATION_COLLECTION,
        [
            {"field_name": "thread_id", "datatype": "VarChar", "max_length": 128},
            {"field_name": "turn_range", "datatype": "VarChar", "max_length": 32},
        ],
    )
    return [MEMORY_COLLECTION, CONVERSATION_COLLECTION]


def _insert(name: str, row: dict[str, Any]) -> str:
    row.setdefault("id", uuid.uuid4().hex)
    vector = embed_texts([row["text"]])[0]
    get_client().insert(collection_name=name, data=[{**row, "vector": vector}])
    return str(row["id"])


def upsert_memory(kind: str, text: str, source_ref: str = "") -> str:
    """已采纳记忆(fact/profile)入向量池,返回 milvus_id。"""
    settings = get_settings()
    return _insert(
        MEMORY_COLLECTION,
        {
            "kind": kind,
            "text": text,
            "source_ref": source_ref,
            "tenant_id": settings.tenant_id,
            "user_id": settings.user_id,
        },
    )


def upsert_conversation_summary(summary: str, thread_id: str, from_no: int, to_no: int) -> str:
    """滚动摘要 embedding 入池(语义召回历史话题),返回 milvus_id。"""
    settings = get_settings()
    return _insert(
        CONVERSATION_COLLECTION,
        {
            "text": summary,
            "thread_id": thread_id,
            "turn_range": f"{from_no}-{to_no}",
            "tenant_id": settings.tenant_id,
            "user_id": settings.user_id,
        },
    )


def search(
    collection: str,
    query: str,
    *,
    kind: str | None = None,
    limit: int = 5,
) -> list[VectorHit]:
    """向量检索,COSINE 阈值过滤低相关;kind 仅 memory 集合生效。"""
    settings = get_settings()
    filters = f'tenant_id == "{settings.tenant_id}" and user_id == "{settings.user_id}"'
    if kind and collection == MEMORY_COLLECTION:
        filters += f' and kind == "{kind}"'

    is_memory = collection == MEMORY_COLLECTION
    output_fields = ["text", "kind", "source_ref"] if is_memory else ["text"]
    hits = get_client().search(
        collection_name=collection,
        data=embed_texts([query]),
        limit=limit,
        filter=filters,
        output_fields=output_fields,
        search_params={"metric_type": "COSINE", "params": {}},
    )
    results: list[VectorHit] = []
    for hit in hits[0] if hits else []:
        entity = hit.get("entity", hit)
        score = float(hit.get("distance", 0.0))
        if score < 0.3:  # COSINE 低阈,噪声直接丢
            continue
        results.append(
            VectorHit(
                id=str(hit.get("id", "")),
                kind=str(entity.get("kind", "")),
                text=str(entity.get("text", "")),
                score=score,
                source_ref=str(entity.get("source_ref", "")),
            )
        )
    return results
