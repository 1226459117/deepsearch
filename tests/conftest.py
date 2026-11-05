"""共享 fixture:FakeSettings 覆写记忆窗口参数,注入各模块的 get_settings。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class FakeSettings:
    tenant_id: str = "default"
    user_id: str = "owner"
    data_dir: str = "data"
    redis_url: str = "redis://127.0.0.1:6379/0"
    postgres_dsn: str = "postgresql://x/x"
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: str = ""
    embedding_dim: int = 1024
    # 模型 keys(healthz 配置探测用)
    primary_api_key: str = ""
    utility_api_key: str = ""
    embedding_api_key: str = ""
    window_rounds: int = 30
    summary_batch_rounds: int = 20
    active_keep_rounds: int = 10
    inject_top_k: int = 5
    inject_max_tokens: int = 1000
    # 检索与成本护栏
    bocha_api_key: str = ""
    tavily_api_key: str = ""
    web_search_budget_per_report: int = 60
    cost_limit_cny_per_report: float = 5.0
    # 调研流水线(P3)
    plan_min_questions: int = 5
    plan_max_questions: int = 8
    search_count_per_query: int = 5
    dual_search_retry: int = 3
    supplement_search_max_rounds: int = 2
    chat_search_limit: int = 1
    factual_search_limit: int = 2
    factual_akshare_limit: int = 1
    tool_loop_max_steps: int = 6
    # 研报写作段(P4)
    outline_min_chapters: int = 6
    outline_max_chapters: int = 10
    chapters_group_size: int = 3
    chapter_target_words: int = 2000
    chapter_supplement_searches: int = 3
    chapter_material_min_hits: int = 2
    review_max_rounds: int = 2
    export_pdf_enabled: bool = False
    # 资料上传(P6)
    mineru_api_url: str = ""
    upload_max_mb: int = 40
    # 模型计价
    primary_price_in_cny_per_1k: float = 0.002
    primary_price_out_cny_per_1k: float = 0.008
    utility_price_in_cny_per_1k: float = 0.0003
    utility_price_out_cny_per_1k: float = 0.0006


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """把 FakeSettings 注入所有顶层 import get_settings 的模块。

    data_dir 指向 tmp_path,测试绝不写真实 data/ 目录(FTS 任务池等)。
    """
    fake = FakeSettings(data_dir=str(tmp_path / "data"))
    for module_name in (
        "deepsearch.config",
        "deepsearch.memory.window",
        "deepsearch.memory.archive",
        "deepsearch.memory.vectors",
        "deepsearch.memory.intercept",
        "deepsearch.memory.llm",
        "deepsearch.tools.search",
        "deepsearch.tools.fts",
        "deepsearch.tools.akshare_data",
        "deepsearch.tools.agent_search",
        "deepsearch.tools.ingest",
        "deepsearch.web.main",
        "deepsearch.graph.nodes.chat",
        "deepsearch.graph.nodes.research",
        "deepsearch.graph.nodes.writing",
        "deepsearch.tools.export",
    ):
        monkeypatch.setattr(f"{module_name}.get_settings", lambda f=fake: f)
    return fake
