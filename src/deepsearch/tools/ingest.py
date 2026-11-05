"""上传资料解析入池(P6:定稿 Q9 双链检索的私有语料侧)。

流程:web 上传 → 落盘 `data/uploads/<thread>/` → parse_document 抽正文
→ fts.index_document 入 L2 任务池 → graph.update_state 把 doc_id 累加进
source_ids(实测坑④:挂起网关的 thread 只能用 update_state 写通道,不能投 input)。

PDF 解析两级:配了 MINERU_API_URL 优先走 MinerU HTTP(复杂表格还原好,
定稿选型);服务不可用/未配置则 pymupdf 抽纯文本兜底。表格转 Markdown 由
MinerU 负责,兜底级不保证表格结构——这是"后置可插拔"的落地形态。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from deepsearch.config import get_settings
from deepsearch.tools import fts

SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".markdown"}
MINERU_TIMEOUT_S = 120  # MinerU 解析大 PDF 可达分钟级


@dataclass(frozen=True)
class ParsedDoc:
    title: str
    text: str
    parser: str  # mineru / pymupdf / plain


def parse_document(path: str | Path) -> ParsedDoc:
    """按扩展名分发解析;不支持的后缀抛 ValueError。"""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        parsed = _parse_pdf(p)
        return ParsedDoc(title=p.stem, text=parsed[0], parser=parsed[1])
    if suffix in (".txt", ".md", ".markdown"):
        return ParsedDoc(title=p.stem, text=_read_plain(p), parser="plain")
    raise ValueError(f"不支持的文件类型: {suffix or '(无扩展名)'}")


def _read_plain(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace").strip()


def _parse_pdf(p: Path) -> tuple[str, str]:
    """MinerU 优先,失败降级 pymupdf;两级都空则报错(空文档不入池)。"""
    api_url = get_settings().mineru_api_url.strip()
    if api_url:
        try:
            md = _mineru_parse(p, api_url)
            if md.strip():
                return md, "mineru"
        except Exception:
            pass  # 服务不可达等一律降级,不让上传链路因解析器挂掉
    text = _pymupdf_extract(p)
    if not text.strip():
        raise ValueError("PDF 无可抽取文本(可能是纯扫描件,请配 MinerU)")
    return text, "pymupdf"


def _mineru_parse(p: Path, api_url: str) -> str:
    """调 MinerU HTTP 服务取 markdown。兼容两套常见端点布局。"""
    import requests  # 延迟导入,与 search.py 同风格

    base = api_url.rstrip("/")
    with open(p, "rb") as fh:
        files = {"files": (p.name, fh)}
        # mineru-api>=1.3: /file_parse(task 模式);layout-parser 旧版: /v1/with_file
        for endpoint, data in (("/file_parse", {"return_md": "true"}),
                               ("/v1/with_file", {"method": "ocr"})):
            try:
                resp = requests.post(
                    f"{base}{endpoint}", files=files, data=data,
                    timeout=MINERU_TIMEOUT_S,
                )
            except requests.RequestException:
                continue
            if resp.status_code in (200, 201):
                md = _extract_md(resp.json() if resp.headers.get(
                    "content-type", "" ).startswith("application/json")
                    else resp.text)
                if md:
                    return md
    raise RuntimeError("MinerU 服务未返回有效 markdown")


def _extract_md(payload) -> str:
    """从 MinerU 各版本响应形态里取 markdown 文本;取不到返回空串。"""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, dict):
            for r in results.values():  # {filename: {md_content: ...}}
                if isinstance(r, dict):
                    md = r.get("md_content") or r.get("markdown") or r.get("md")
                    if md:
                        return str(md)
        for key in ("markdown", "md_content", "md"):
            if payload.get(key):
                return str(payload[key])
    return ""


def _pymupdf_extract(p: Path) -> str:
    try:
        import pymupdf  # 新版包名
    except ImportError:  # pragma: no cover - 旧环境回退
        import fitz as pymupdf

    parts: list[str] = []
    with pymupdf.open(p) as doc:
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(x for x in parts if x.strip())


def ingest_file(
    thread_id: str,
    path: str | Path,
    *,
    data_dir: str | None = None,
) -> fts.CorpusDoc | None:
    """解析并索引到任务池,返回带 doc_id 的文档;空正文返回 None(不入池)。"""
    parsed = parse_document(path)
    text = parsed.text.strip()
    if not text:
        return None
    doc_id = fts.index_document(
        thread_id,
        title=f"{parsed.title}[{parsed.parser}]",
        content=text,
        url=str(path),
        source=f"upload_{parsed.parser}",
        data_dir=data_dir,
    )
    return fts.CorpusDoc(
        doc_id=doc_id, title=parsed.title, url=str(path),
        content=text, source=parsed.parser,
    )
