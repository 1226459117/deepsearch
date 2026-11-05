"""网络检索工具:Bocha(中文主源)+ Tavily(兜底降级边)。

定稿(Q10/Q27):Bocha 为主,节点内 tenacity 重试 ×N;耗尽或 Bocha key 缺失
即沿"供应商降级边"自动切 Tavily;两源皆不可用 → 返回空列表(优雅降级,
调用方据此判证据缺口,绝不抛穿主链路)。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from deepsearch.config import get_settings

BOCHA_ENDPOINT = "https://api.bochaai.com/v1/web-search"
TAVILY_ENDPOINT = "https://api.tavily.com/search"
_TIMEOUT = 20.0


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    site: str
    published: str  # 尽量取发布时间,缺失留空(供 merge 取新弃旧)
    provider: str  # bocha / tavily

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")


@dataclass(frozen=True)
class SearchBundle:
    results: list[SearchResult]
    provider_used: str  # bocha / tavily / none
    fell_back: bool  # 是否触发了降级边


def web_search(query: str, *, count: int | None = None) -> SearchBundle:
    """主入口:Bocha 优先,失败降级 Tavily。永不抛异常。"""
    settings = get_settings()
    count = count or settings.search_count_per_query

    if settings.bocha_api_key:
        try:
            results = _bocha_search(query, count)
            if results:
                return SearchBundle(results, "bocha", fell_back=False)
        except Exception:
            pass  # Bocha 彻底失败 → 走降级边
        # 降级边
        if settings.tavily_api_key:
            try:
                return SearchBundle(_tavily_search(query, count), "tavily", fell_back=True)
            except Exception:
                return SearchBundle([], "none", fell_back=True)
        return SearchBundle([], "none", fell_back=True)

    # 无 Bocha key:直接尝试 Tavily
    if settings.tavily_api_key:
        try:
            return SearchBundle(_tavily_search(query, count), "tavily", fell_back=False)
        except Exception:
            return SearchBundle([], "none", fell_back=False)
    return SearchBundle([], "none", fell_back=False)


def _bocha_search(query: str, count: int) -> list[SearchResult]:
    data = _bocha_request({"query": query, "count": count, "summary": True}, _bocha_headers())
    pages = data.get("data", {}).get("webPages", {}).get("value", [])
    out: list[SearchResult] = []
    for item in pages:
        out.append(
            SearchResult(
                title=item.get("name", ""),
                url=item.get("url", ""),
                snippet=item.get("summary") or item.get("snippet", ""),
                site=item.get("siteName", ""),
                published=item.get("datePublished") or item.get("dateLastCrawled", ""),
                provider="bocha",
            )
        )
    return out


def _tavily_search(query: str, count: int) -> list[SearchResult]:
    payload = {
        "api_key": get_settings().tavily_api_key,
        "query": query,
        "max_results": count,
        "include_answer": False,
    }
    data = _tavily_request(payload)
    out: list[SearchResult] = []
    for item in data.get("results", []):
        out.append(
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                site=urlparse(item.get("url", "")).netloc,
                published=item.get("published_date", ""),
                provider="tavily",
            )
        )
    return out


@retry(
    retry=retry_if_exception_type((httpx.HTTPError,)),
    # stop 为可调用:每次重试读当前配置,避免 import 期固化 dual_search_retry
    stop=lambda rs: rs.attempt_number >= get_settings().dual_search_retry,
    wait=wait_exponential(multiplier=0.5, max=4),
    reraise=True,
)
def _bocha_request(payload: dict, headers: dict) -> dict:
    resp = httpx.post(BOCHA_ENDPOINT, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


@retry(
    retry=retry_if_exception_type((httpx.HTTPError,)),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, max=4),
    reraise=True,
)
def _tavily_request(payload: dict) -> dict:
    resp = httpx.post(
        TAVILY_ENDPOINT, json=payload, headers={"Content-Type": "application/json"}, timeout=_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def _bocha_headers() -> dict:
    return {"Authorization": f"Bearer {get_settings().bocha_api_key}", "Content-Type": "application/json"}
