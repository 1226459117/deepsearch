"""记忆子系统(P1 落地):
- llm.py       OpenAI 兼容 provider 抽象(主档/廉价档/embedding)
- window.py    L1 Redis 活跃窗口 + 30 轮滚动摘要触发
- archive.py   L1 全量原文 PG 归档 + L3 候选/采纳结构行
- vectors.py   Milvus memory(fact/profile)与 conversations 集合读写
- profile.py   蒸馏→候选→人工采纳治理(fact 自动生效,profile 人审)
- intercept.py 定点拦截:新会话首轮 + triage/plan_queries 注入
- service.py   跨域协调门面:post_turn / chat_context(三域禁交叉写)
"""
