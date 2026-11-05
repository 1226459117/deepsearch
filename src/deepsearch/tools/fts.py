"""L2 任务级资料池:SQLite FTS5 全文检索(定稿 Q9/Q21)。

- 单文件 `data/fts.db`,按 task_id 分区(任务级隔离,预留持久语料库扩展位);
- 存素材卡原文,对外只暴露 doc_id(state 只存 ID,不搬运原文);
- 中文全文检索用 FTS5 `trigram` 分词(SQLite≥3.34);不支持时降级 `unicode61`
  并在建库时记住所用分词器;
- pdf_ingest 用 pymupdf 抽文本(MinerU 表格解析后置到后续)。

本模块属 L2 任务工作记忆域,与 L1 PG 归档 / L3 Milvus 不交叉写。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from deepsearch.config import get_settings

DB_FILENAME = "fts.db"
_TOKENIZER_CANDIDATES = ("trigram", "unicode61")


@dataclass(frozen=True)
class CorpusHit:
    doc_id: str
    title: str
    url: str
    snippet: str  # FTS 高亮片段或正文前缀
    score: float


@dataclass(frozen=True)
class CorpusDoc:
    doc_id: str
    title: str
    url: str
    content: str
    source: str  # bocha / tavily / private_pdf / akshare


def db_path(data_dir: str | None = None) -> Path:
    root = Path(data_dir or get_settings().data_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / DB_FILENAME


def _connect(data_dir: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path(data_dir)))
    conn.row_factory = sqlite3.Row
    return conn


def _pick_tokenizer(conn: sqlite3.Connection) -> str:
    for tok in _TOKENIZER_CANDIDATES:
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE tmp_fts_probe USING fts5(x, tokenize='{tok}')"
            )
            conn.execute("DROP TABLE tmp_fts_probe")
            return tok
        except sqlite3.OperationalError:
            continue
    return "unicode61"


def ensure_schema(data_dir: str | None = None) -> str:
    """建库建表,返回实际使用的分词器。幂等。"""
    conn = _connect(data_dir)
    with conn:
        existing = conn.execute(
            "SELECT value FROM fts_meta WHERE key='tokenizer'"
        ).fetchone() if _has_meta(conn) else None
        if existing:
            return existing["value"]
        tokenizer = _pick_tokenizer(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fts_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS corpus USING fts5(
                doc_id UNINDEXED,
                task_id UNINDEXED,
                title,
                content,
                url UNINDEXED,
                source UNINDEXED,
                tokenize='{tokenizer}'
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO fts_meta(key, value) VALUES('tokenizer', ?)",
            (tokenizer,),
        )
    conn.close()
    return tokenizer


def _has_meta(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fts_meta'"
        ).fetchone()
        is not None
    )


def index_document(
    task_id: str,
    *,
    title: str,
    content: str,
    url: str = "",
    source: str = "",
    doc_id: str | None = None,
    data_dir: str | None = None,
) -> str:
    """索引一份素材,返回 doc_id(state.source_ids 记录之)。"""
    ensure_schema(data_dir)
    doc_id = doc_id or uuid.uuid4().hex
    conn = _connect(data_dir)
    with conn:
        conn.execute(
            "INSERT INTO corpus(doc_id, task_id, title, content, url, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, task_id, title, content, url, source),
        )
    conn.close()
    return doc_id


def search(
    task_id: str,
    query: str,
    *,
    limit: int = 5,
    data_dir: str | None = None,
) -> list[CorpusHit]:
    """任务内全文检索。query 空或无命中的语法错误一律返回空(不抛穿)。"""
    if not query.strip():
        return []
    tokenizer = ensure_schema(data_dir)
    # trigram 分词要求词长≥3;两字级短词(中文常见)查不到,降级 LIKE 子串匹配
    if tokenizer == "trigram" and _short_terms(query):
        return _like_search(task_id, query, limit=limit, data_dir=data_dir)
    conn = _connect(data_dir)
    try:
        rows = conn.execute(
            """
            SELECT doc_id, title, url,
                   snippet(corpus, 3, '[', ']', '…', 12) AS snip,
                   bm25(corpus) AS rank
            FROM corpus
            WHERE corpus MATCH ? AND task_id = ?
            ORDER BY rank
            LIMIT ?
            """,
            (_match(query), task_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return [
        CorpusHit(
            doc_id=r["doc_id"],
            title=r["title"],
            url=r["url"],
            snippet=r["snip"] or "",
            score=-float(r["rank"]),  # bm25 越小越好,取负使"越大越相关"
        )
        for r in rows
    ]


def ingest_pdf(
    task_id: str,
    pdf_path: str | Path,
    *,
    data_dir: str | None = None,
) -> list[str]:
    """pymupdf 抽全文并按文档索引(私有 PDF 入任务池)。返回 doc_id 列表。"""
    import fitz  # PyMuPDF

    path = Path(pdf_path)
    text_parts: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            text_parts.append(page.get_text())
    full = "\n".join(p for p in text_parts if p.strip())
    if not full:
        return []
    return [
        index_document(
            task_id,
            title=path.stem,
            content=full,
            url=str(path),
            source="private_pdf",
            data_dir=data_dir,
        )
    ]


def get_documents(
    doc_ids: list[str], *, data_dir: str | None = None
) -> list[CorpusDoc]:
    """按 doc_id 取回原文(merge 阶段需读回素材文本做冲突比对)。"""
    if not doc_ids:
        return []
    ensure_schema(data_dir)
    conn = _connect(data_dir)
    placeholders = ",".join("?" for _ in doc_ids)
    rows = conn.execute(
        f"SELECT doc_id, title, url, content, source FROM corpus "
        f"WHERE doc_id IN ({placeholders})",
        doc_ids,
    ).fetchall()
    conn.close()
    return [
        CorpusDoc(
            doc_id=r["doc_id"],
            title=r["title"],
            url=r["url"],
            content=r["content"],
            source=r["source"],
        )
        for r in rows
    ]


def _match(query: str) -> str:
    """把自由查询转成 FTS5 安全 MATCH:转义双引号,按词加引号做前缀匹配。"""
    cleaned = query.replace('"', " ")
    terms = [t for t in cleaned.split() if t]
    if not terms:
        return query
    return " ".join(f'"{t}"' for t in terms)


def _short_terms(query: str) -> bool:
    """所有词都短于 3 字符(trigram 无法命中)时为 True。"""
    terms = [t for t in query.replace('"', " ").split() if t]
    return bool(terms) and all(len(t) < 3 for t in terms)


def _like_search(
    task_id: str, query: str, *, limit: int, data_dir: str | None
) -> list[CorpusHit]:
    """短词降级路:子串包含匹配,相关性无法用 bm25,统一给满分保顺序稳定。"""
    conn = _connect(data_dir)
    pattern = f"%{query}%"
    try:
        rows = conn.execute(
            """
            SELECT doc_id, title, url, substr(content, 1, 120) AS snip
            FROM corpus
            WHERE task_id = ? AND (content LIKE ? OR title LIKE ?)
            ORDER BY rowid DESC
            LIMIT ?
            """,
            (task_id, pattern, pattern, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return [
        CorpusHit(
            doc_id=r["doc_id"], title=r["title"], url=r["url"],
            snippet=r["snip"] or "", score=1.0,
        )
        for r in rows
    ]
