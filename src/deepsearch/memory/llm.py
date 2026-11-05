"""OpenAI 兼容协议的 provider 抽象:主档 / 廉价档 / embedding 三通道。

定稿口径:PRIMARY=DeepSeek-V3.x(生成主力),UTILITY=Qwen-Turbo 级
(滚动摘要、记忆蒸馏等廉价批量任务),EMBEDDING=Dashscope text-embedding-v3。
三通道均只依赖 base_url + api_key + model,换供应商只改 .env。
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI

from deepsearch.config import get_settings


@dataclass(frozen=True)
class ChatResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[dict] | None = None  # 函数调用回合:OpenAI tool_calls 序列化

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# Settings 不可哈希,lru_cache 不适用;按 (kind, base_url, api_key) 缓存客户端,
# 换 key/端点自动新建,进程内连接池复用。
_clients: dict[tuple[str, str, str], OpenAI] = {}


def _get_client(kind: str, base_url: str, api_key: str) -> OpenAI:
    key = (kind, base_url, api_key)
    if key not in _clients:
        _clients[key] = OpenAI(base_url=base_url, api_key=api_key)
    return _clients[key]


def _resolve_tier(tier: str) -> tuple[OpenAI, str]:
    settings = get_settings()
    if tier == "primary":
        client = _get_client("primary", settings.primary_base_url, settings.primary_api_key)
        return client, settings.primary_model
    if tier == "utility":
        client = _get_client("utility", settings.utility_base_url, settings.utility_api_key)
        return client, settings.utility_model
    raise ValueError(f"未知模型档位: {tier!r}(可用 primary/utility)")


def estimate_cost_cny(result: ChatResult, tier: str) -> float:
    """按 .env 计价表折算单次调用成本(元)。未知档位按主档价估。"""
    settings = get_settings()
    if tier == "utility":
        pin = settings.utility_price_in_cny_per_1k
        pout = settings.utility_price_out_cny_per_1k
    else:
        pin = settings.primary_price_in_cny_per_1k
        pout = settings.primary_price_out_cny_per_1k
    return (
        result.prompt_tokens / 1000 * pin + result.completion_tokens / 1000 * pout
    )


def chat(
    messages: list[dict[str, str]],
    *,
    tier: str = "primary",
    temperature: float = 0.3,
    max_tokens: int | None = None,
) -> ChatResult:
    """调用对话模型。tier: primary(主力,默认)/ utility(廉价批量任务)。"""
    client, model = _resolve_tier(tier)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        temperature=temperature,
        max_tokens=max_tokens,
    )
    usage = resp.usage
    return ChatResult(
        content=resp.choices[0].message.content or "",
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
    )


def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    *,
    tier: str = "primary",
    temperature: float = 0.3,
) -> ChatResult:
    """带函数调用的对话回合。返回体若含 tool_calls 则序列化供上层调度。"""
    client, model = _resolve_tier(tier)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        tools=tools,  # type: ignore[arg-type]
        tool_choice="auto",
        temperature=temperature,
    )
    msg = resp.choices[0].message
    usage = resp.usage
    tool_calls: list[dict] | None = None
    if msg.tool_calls:
        tool_calls = [
            {
                "id": tc.id,
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            }
            for tc in msg.tool_calls
        ]
    return ChatResult(
        content=msg.content or "",
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        tool_calls=tool_calls,
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量向量化(Dashscope text-embedding-v3,维度=settings.embedding_dim)。

    单次上限 10 条(兼容端点限制),超出自动分批。
    """
    settings = get_settings()
    client = _get_client(
        "embedding", settings.embedding_base_url, settings.embedding_api_key
    )
    vectors: list[list[float]] = []
    for i in range(0, len(texts), 10):
        batch = texts[i : i + 10]
        resp = client.embeddings.create(
            model=settings.embedding_model,
            input=batch,
            dimensions=settings.embedding_dim,  # type: ignore[call-arg]
            encoding_format="float",
        )
        vectors.extend(item.embedding for item in resp.data)
    return vectors
