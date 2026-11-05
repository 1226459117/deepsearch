-- 001_init_memory.sql —— L1 归档 / L3 长期记忆 PG 初始 schema(定稿 v6-final)
-- 约定:所有表带 tenant_id/user_id 以保留多租户键位(单机自用恒为 default/owner)。

CREATE TABLE IF NOT EXISTS schema_migrations (
    name        text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- L1 对话全量归档:Redis 只删活跃窗口,原文永久在此(R43 定稿)
CREATE TABLE IF NOT EXISTS conversation_messages (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL,
    user_id     text NOT NULL,
    thread_id   text NOT NULL,
    turn_no     int  NOT NULL,
    role        text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, user_id, thread_id, turn_no)
);
CREATE INDEX IF NOT EXISTS idx_conv_msg_thread
    ON conversation_messages (tenant_id, user_id, thread_id, turn_no);

-- 滚动摘要行:压缩前 N 轮的产物,embedding 向量本体在 Milvus,此处存摘要文本与引用
CREATE TABLE IF NOT EXISTS conversation_summaries (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL,
    user_id     text NOT NULL,
    thread_id   text NOT NULL,
    from_turn   int  NOT NULL,
    to_turn     int  NOT NULL,
    summary     text NOT NULL,
    milvus_id   text,             -- Milvus facts 集合中的向量主键
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conv_sum_thread
    ON conversation_summaries (tenant_id, user_id, thread_id);

-- 记忆候选队列:自动蒸馏只产候选,人工在管理页采纳(定稿 R4/Q39/Q44)
CREATE TABLE IF NOT EXISTS memory_candidates (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL,
    user_id     text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('fact', 'profile')),
                -- fact=事实归档(自动生效无需人审);profile=偏好画像(必须人审)
    source_type text NOT NULL CHECK (source_type IN ('task_end', 'cp2_diff', 'chat_digest', 'manual')),
    source_ref  text,             -- 溯源引用:task_id / thread_id / 报告目录
    content     text NOT NULL,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'adopted', 'rejected')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    reviewed_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_cand_status ON memory_candidates (tenant_id, user_id, status);

-- 已采纳的用户画像库:仅此类内容进入拦截检索的 profile 集合(Q44 双库切分)
CREATE TABLE IF NOT EXISTS adopted_memories (
    id            bigserial PRIMARY KEY,
    tenant_id     text NOT NULL,
    user_id       text NOT NULL,
    kind          text NOT NULL CHECK (kind IN ('profile', 'fact')),
    content       text NOT NULL,
    milvus_id     text,
    candidate_id  bigint REFERENCES memory_candidates (id),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_adopted_user
    ON adopted_memories (tenant_id, user_id, kind);

-- 风格卡:当前生效的写作风格约束单行文本(plan/chapter 注入点使用)
CREATE TABLE IF NOT EXISTS style_cards (
    tenant_id   text NOT NULL,
    user_id     text NOT NULL,
    content     text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

-- 数据源可靠性:域名级轻评分表(Q38-C 极轻形式)
CREATE TABLE IF NOT EXISTS source_ratings (
    tenant_id   text NOT NULL,
    domain      text NOT NULL,   -- 如 finance.caixin.com
    score       numeric NOT NULL DEFAULT 0.5 CHECK (score BETWEEN 0 AND 1),
    note        text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, domain)
);
