"""全局配置:pydantic-settings 读取 `.env`,按记忆三域与模型双档分组。

真相源边界(定稿 R6):Redis=L1 窗口唯一源;SQLite checkpoint=L2 唯一源;
PG+Milvus=L3 唯一源。本模块只负责把连接参数交给各存储层。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 应用 ----
    app_name: str = "deepsearch"
    data_dir: str = "data"  # SQLite checkpoint / 任务队列 / reports 落盘根目录
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    # ---- 租户与用户(单机自用恒为 default/owner,服务化留位) ----
    tenant_id: str = "default"
    user_id: str = "owner"

    # ---- 模型:主档(DeepSeek-V3.x)/ 廉价档(Qwen-Turbo 级),OpenAI 兼容协议 ----
    primary_base_url: str = "https://api.deepseek.com/v1"
    primary_api_key: str = ""
    primary_model: str = "deepseek-chat"

    utility_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    utility_api_key: str = ""
    utility_model: str = "qwen-turbo"

    # ---- Embedding:Dashscope text-embedding-v3(与廉价档同供应商,同一 .env) ----
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024

    # ---- 存储三域 ----
    redis_url: str = "redis://127.0.0.1:6379/0"  # L1 活跃窗口
    postgres_dsn: str = "postgresql://deepsearch:deepsearch@127.0.0.1:5432/deepsearch"  # L3 结构化/归档
    milvus_uri: str = "http://127.0.0.1:19530"  # L3 向量
    milvus_token: str = ""

    # ---- 记忆窗口参数(定稿 R2) ----
    window_rounds: int = 30  # Redis 活跃窗口轮数上限
    summary_batch_rounds: int = 20  # 满窗口时摘要压缩前 N 轮
    active_keep_rounds: int = 10  # 压缩后保留原文的最近轮数
    inject_top_k: int = 5  # 拦截检索 Top-K(定稿 R5)
    inject_max_tokens: int = 1000  # 注入 token 预算上限

    # ---- 检索与成本护栏 ----
    bocha_api_key: str = ""
    tavily_api_key: str = ""
    web_search_budget_per_report: int = 60  # 单篇网络检索次数上限
    cost_limit_cny_per_report: float = 5.0  # 成本护栏:超限自动暂停询问

    # ---- 调研流水线(P3) ----
    plan_min_questions: int = 5  # plan_queries 生成的调研问题数区间
    plan_max_questions: int = 8
    search_count_per_query: int = 5  # 单查询单次网络检索条数
    dual_search_retry: int = 3  # 节点内 Bocha 重试次数,耗尽走降级边
    supplement_search_max_rounds: int = 2  # assess_coverage 补搜上限
    chat_search_limit: int = 1  # 闲聊档自主搜索上限(Q36)
    factual_search_limit: int = 2  # 小型事实题检索上限(Q34)
    factual_akshare_limit: int = 1  # 小型事实题财务查询上限
    tool_loop_max_steps: int = 6  # 自主搜索 tool-loop 最大轮数

    # ---- 研报写作段(P4) ----
    outline_min_chapters: int = 6  # 大纲章数区间
    outline_max_chapters: int = 10
    chapters_group_size: int = 3  # 分组混合并行:组内 Send 并行、组间串行传摘要
    chapter_target_words: int = 2000  # 单章目标字数(10k–20k 总量按章分摊)
    chapter_supplement_searches: int = 3  # 单章限量补查上限
    chapter_material_min_hits: int = 2  # 池内命中不足此数才触发章内补查
    review_max_rounds: int = 2  # consistency_review 定向重写循环上限
    export_pdf_enabled: bool = True  # weasyprint 不可用时自动降级只出 HTML

    # ---- 资料上传(P6) ----
    mineru_api_url: str = ""  # MinerU HTTP 服务(如 http://127.0.0.1:8002);留空=直接用 pymupdf 抽文本
    upload_max_mb: int = 40  # 单文件上传上限

    # ---- 模型计价(元/千 token,成本护栏核算用;换供应商只改 .env) ----
    primary_price_in_cny_per_1k: float = 0.002  # DeepSeek-V3.x 输入
    primary_price_out_cny_per_1k: float = 0.008  # DeepSeek-V3.x 输出
    utility_price_in_cny_per_1k: float = 0.0003  # Qwen-Turbo 级输入
    utility_price_out_cny_per_1k: float = 0.0006  # Qwen-Turbo 级输出


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
