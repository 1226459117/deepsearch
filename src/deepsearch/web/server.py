"""uvicorn 启动入口:`uv run deepsearch-serve`。"""

from __future__ import annotations

import uvicorn

from deepsearch.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "deepsearch.web.main:app",
        host=settings.web_host,
        port=settings.web_port,
    )


if __name__ == "__main__":
    main()
