"""记忆拦截:定点检索 L3 并组装注入块(Top-K≤5,≤1k token,定稿 R45)。

触发点收敛在三个槽位,不做统一末尾拼接:
- triage 节点:近期关注域(profile 命中即用户关注领域);
- plan_queries 节点:用户偏好 + 事实池(影响选题角度);
- 新会话首轮:完整注入块(facts+profile+风格卡)。

降级原则:Milvus/PG 任一不可用时返回空串——记忆是增强项,永不阻塞主链路。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from deepsearch.config import get_settings
from deepsearch.memory import archive, vectors
from deepsearch.memory.vectors import VectorHit


@dataclass
class MemoryContext:
    query: str
    facts: list[VectorHit] = field(default_factory=list)
    profiles: list[VectorHit] = field(default_factory=list)
    style_card: str | None = None
    injected_chars: int = 0

    def is_empty(self) -> bool:
        return not (self.facts or self.profiles or self.style_card)


def is_new_session(thread_id: str) -> bool:
    """Redis 窗口无任何轮次即视为新会话(首轮拦截条件)。"""
    from deepsearch.memory.window import ConversationWindow

    try:
        return ConversationWindow(thread_id).turn_count() == 0
    except Exception:  # Redis 不可用时保守判旧,避免重复全量注入
        return False


def retrieve(query: str, *, with_style: bool = True) -> MemoryContext:
    """双集合各取 Top-K,Milvus/PG 故障静默降级为空上下文。"""
    settings = get_settings()
    ctx = MemoryContext(query=query)
    try:
        ctx.facts = vectors.search(
            vectors.MEMORY_COLLECTION, query, kind="fact", limit=settings.inject_top_k
        )
        ctx.profiles = vectors.search(
            vectors.MEMORY_COLLECTION, query, kind="profile", limit=settings.inject_top_k
        )
    except Exception:
        pass
    if with_style:
        try:
            ctx.style_card = archive.get_style_card()
        except Exception:
            pass
    return ctx


def _estimate_tokens(text: str) -> int:
    """无 tokenizers 依赖的粗估:中文约 1.6 字/token,ASCII 4 字符/token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.6 + other / 4) + 1


def build_injection(ctx: MemoryContext, *, budget_tokens: int | None = None) -> str:
    """结构化槽位注入块;超出预算按 score 低分先弃(facts/profiles 交替裁剪)。"""
    settings = get_settings()
    budget = budget_tokens or settings.inject_max_tokens
    if ctx.is_empty():
        return ""

    # 按相关性合并裁剪:score 降序逐条加入,直到预算耗尽
    pooled = sorted(
        [("fact", h) for h in ctx.facts] + [("profile", h) for h in ctx.profiles],
        key=lambda item: item[1].score,
        reverse=True,
    )
    kept_facts: list[str] = []
    kept_profiles: list[str] = []
    used = 0
    for kind, hit in pooled:
        line = f"- {hit.text}"
        cost = _estimate_tokens(line)
        if used + cost > budget:
            continue
        (kept_facts if kind == "fact" else kept_profiles).append(line)
        used += cost

    sections: list[str] = []
    if kept_profiles:
        sections.append("〈用户画像〉\n" + "\n".join(kept_profiles))
    if kept_facts:
        sections.append("〈相关事实〉\n" + "\n".join(kept_facts))
    if ctx.style_card and used + _estimate_tokens(ctx.style_card) <= budget:
        sections.append(f"〈写作风格〉\n{ctx.style_card}")
    if not sections:
        return ""
    return "〈长期记忆〉\n" + "\n".join(sections)


def injection_for_node(node: str, query: str, *, new_session: bool = False) -> str:
    """节点名 → 注入策略。未知节点不注入(白名单制,防滥挂)。"""
    if node not in ("triage", "plan_queries", "chat_first_turn", "chapter_write"):
        return ""
    if node == "chapter_write":
        # 定稿三注入点:逐章写作只挂风格卡,不重复注入事实/画像
        ctx = retrieve(query, with_style=True)
        ctx.facts = []
        ctx.profiles = []
        return build_injection(ctx)
    if node == "chat_first_turn" and not new_session:
        return ""
    ctx = retrieve(query, with_style=node != "triage")  # triage 只要关注域不要风格
    if node == "triage":
        ctx.facts = []  # 分诊只需要画像信号
    return build_injection(ctx)
