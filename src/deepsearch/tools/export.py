"""研报导出:Markdown 权威源 → HTML(+weasyprint PDF,best-effort)。

定稿 Q5:MD 是唯一权威源,HTML/PDF 只是渲染产物;reports/ 目录即真相。
weasyprint 在 Windows 依赖 Pango 原生库,import/渲染任一步失败都只降级
为"无 PDF",绝不抛穿主链路。
"""

from __future__ import annotations

import markdown as md_lib
from pathlib import Path

from deepsearch.config import get_settings

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: "Microsoft YaHei", serif; max-width: 860px;
         margin: 3rem auto; line-height: 1.8; color: #1a1a1a; }}
  h1, h2 {{ border-bottom: 2px solid #0a7d33; padding-bottom: .3rem; }}
  table {{ border-collapse: collapse; }}
  th, td {{ border: 1px solid #bbb; padding: .35rem .7rem; }}
  blockquote {{ border-left: 4px solid #0a7d33; margin-left: 0;
               padding-left: 1rem; color: #444; }}
  .disclaimer {{ background: #f6f6f6; border: 1px dashed #999;
                padding: 1rem; font-size: .85em; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def md_to_html(md_text: str, title: str) -> str:
    body = md_lib.markdown(
        md_text, extensions=["tables", "toc", "footnotes", "sane_lists"]
    )
    return _HTML_TEMPLATE.format(title=title, body=body)


def try_pdf(html: str, pdf_path: Path) -> bool:
    """weasyprint 出 PDF;环境不支持则 False(降级不报错)。"""
    if not get_settings().export_pdf_enabled:
        return False
    try:
        from weasyprint import HTML

        HTML(string=html).write_pdf(str(pdf_path))
        return pdf_path.exists()
    except Exception:
        return False


def write_outputs(report_dir: Path, md_text: str, title: str) -> dict[str, str | None]:
    """落 report.html(+ 可选 report.pdf),返回各产物路径;MD 由上游已写。"""
    report_dir.mkdir(parents=True, exist_ok=True)
    html_path = report_dir / "report.html"
    html = md_to_html(md_text, title)
    html_path.write_text(html, encoding="utf-8")

    pdf_path = report_dir / "report.pdf"
    ok = try_pdf(html, pdf_path)
    return {
        "md": str(report_dir / "report.md"),
        "html": str(html_path),
        "pdf": str(pdf_path) if ok else None,
    }
