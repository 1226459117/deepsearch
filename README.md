# deepsearch · 深度研报智能体系统

单机自用的投研助手:输入选题,自动完成 **分诊 → 调研 → 大纲 → 逐章写作 → 一致性审稿 → 人工确认 → 装配导出** 的全流水线,产出 1–2 万字带引用的中文深度研报(Markdown 权威源 + HTML + PDF),全程受人工网关与 ¥5/篇成本护栏保护。

## 架构一览

```
                 ┌─ 四档分诊 triage(utility 档模型,低置信 web 弹窗人工改档)
  FastAPI+Vue SPA├─ chat        闲聊/通用问答(Redis 窗口 + 自主搜索≤1)
  选题输入页  →  ├─ factual     小型事实题(检索≤2 + AkShare≤1)
  SQLite 任务队列├─ steering    在跑任务的转向指令(update_state 写通道,不抢占)
  串行 worker    └─ report      研报 14 节点流水线:
        plan_queries → dual_search(双链检索)→ gather → merge_align(冲突比对)
        → assess_coverage(≤2 轮补搜)→ outline_gen → ⏸CP1 大纲闸
        → chapter_write(Send 分组并行,组内 3 章并行/组间串行传摘要)
        → consistency_review(五维度审稿,≤2 轮定向重写)→ ⏸CP2 成稿闸
        → revise_dirty → assemble_final → render_export
```

- **三层记忆**:L1 Redis 活跃窗口(30 轮,满窗压缩 20 轮为滚动摘要落 PG+Milvus);L2 SQLite checkpoint(研报工作记忆唯一真相源)+ FTS5 任务级素材池;L3 PG 结构化归档 + Milvus 向量(fact/profile 双库,记忆候选人工采纳)。
- **人工网关**:CP1 大纲确认(可改纲/终止)、CP2 成稿确认(可逐章改稿/分歧定夺)、COST 成本暂停(¥5 超限自动 interrupt,可抬限或止损)。
- **中途转向**:任务运行中随时投递 steering 指令,在下一个网关被消费,不打断当前执行。
- 存储不可达时记忆注入静默降级为空(PG/Redis/Milvus 任一离线在 `memory/service` 门面层告警降级,快速失败超时保护,chat/研报主链路不受影响)。

## 快速开始

```bash
uv sync                                   # 安装依赖(默认走阿里云镜像)
copy .env.example .env                    # 填模型 keys 与存储连接(自备实例)
uv run deepsearch-initdb                  # PG 迁移 + Milvus 建集合 + FTS 建库 + Redis 探活
cd frontend && npm install && npm run build && cd ..   # 构建 Vue 前端(产物由后端托管)
uv run deepsearch-serve                   # http://127.0.0.1:8000
```

前端为 Vue3 + Element Plus + Vite SPA(`frontend/`),构建产物 `frontend/dist` 由 FastAPI 在 `/` 直接托管;未构建时首页回退遗留 Jinja 模板。前端开发模式:

```bash
uv run deepsearch-serve                   # 先起后端(8000)
cd frontend && npm run dev                # Vite 开发服(5173),/api、SSE、/files 代理到 8000
```

`.env` 关键项:`PRIMARY_*`(DeepSeek 级主档)、`UTILITY_*`(Qwen-Turbo 级廉价档)、`EMBEDDING_*`(Dashscope text-embedding-v3)、`REDIS_URL` / `POSTGRES_DSN` / `MILVUS_URI`(三层存储)、`BOCHA_API_KEY` / `TAVILY_API_KEY`(双链检索)、`COST_LIMIT_CNY_PER_REPORT`(默认 5.0)、`MINERU_API_URL`(可选,PDF 表格解析)。

## Web 使用流程

1. **首页**输入选题,可选"自动分档"或显式指定档位(研报/快查/闲聊)提交;任务列表可对未终态任务**取消**、对失败任务**重试**(全新 thread 干净重跑);
2. **任务页**实时进度(SSE 事件流 + 3s 轮询兜底),状态徽章与阶段/章进度/成本/检索次数均在轮询面板内实时刷新;
3. **CP1 弹窗**确认大纲:认可 / 编辑 JSON 改纲 / 终止;
4. 写作期间可随时 **投递指令**(steering)与 **上传私有资料**(PDF/TXT/MD ≤40MB,入任务检索池);
5. **CP2 弹窗**确认定稿:逐章改写、分歧定夺、认可或终止;
6. 完成后在 `/reports` 浏览产物,`report.md` 为权威源;成本超限会先弹 **COST 网关**(抬限或止损)。

低置信分诊的结果页会附"分诊复核"表单,一键用原输入改档重跑。

## 记忆闭环管理页(`/memory`)

- **候选采纳**:任务结束蒸馏、CP2 人工改稿 diff 自动产出记忆候选;fact 事实类自动生效,profile 画像类必须在此页人工采纳才进拦截检索池(定稿 R4/Q44);采纳同时写 PG 结构行 + Milvus 向量;
- **风格卡**:一行文本,注入大纲/逐章写作提示词;
- **数据源评分**:域名级 0~1 轻评分(定稿 Q38-C),供检索源可靠性排序。

L3 存储(PG/Milvus/Redis)离线时本页显示降级横幅,其余功能不受影响。

## 任务调度取舍(定稿回顾)

单机自用场景选择 **SQLite 队列 + 同进程 asyncio 串行 worker**,而非多 worker 并行:

- **为什么串行**:研报任务的重型 IO 在模型与检索的外呼等待上,并行多篇会把 ¥5/篇 护栏的失败半径和多线程下 SQLite checkpoint 的写竞争同时放大;单篇内部已有 chapter_write Send 分组并行(组内 3 章并发),吞吐瓶颈不在任务级并行度。
- **抢占与中断**:graph 只能在节点边界/interrupt 处停,running 任务无法中途真打断——取消按钮按"超步边界失效"语义实现(置 failed,跑完当前超步后不再继续);steering 用 `update_state` 写通道投递,不顶掉挂起网关(见 LangGraph 坑④)。
- **retry 语义**:失败任务在**全新 thread**(-rN 递增后缀)上干净重跑,复用原 checkpoint 会让旧章节/素材污染结果。
- **何时该升级**:若要多机/多 worker,队列需换 PG LISTEN-NOTIFY 或 Redis Streams,worker 需按 thread 分片;当前 `claim_next` 的 `UPDATE...RETURNING` 已是原子领取,迁移时只换领取层。

## 开发与测试

```bash
uv run pytest                             # 全量离线测试(模型/网络/存储全替身)
uv run python scripts/smoke_tools.py      # 检索工具冒烟
uv run python scripts/smoke_graph.py      # 主图冒烟
uv run python scripts/smoke_writing.py    # 研报段流水线(SqliteSaver)冒烟
uv run python scripts/smoke_web.py        # web 五件套 + 后台 worker 冒烟
uv run python scripts/smoke_infra.py      # Redis/PG/Milvus/embedding 真实连通往返
uv run python scripts/smoke_intercept.py  # intercept 三注入点真实存储联调(离线自动 SKIP)
uv run python scripts/live_report.py [选题]  # 真模型端到端实跑一篇(¥5 护栏演练)
```

目录:`src/deepsearch/graph`(主图与节点)、`memory`(三层记忆)、`tools`(检索/FTS/解析/导出)、`web`(队列/worker/网关/路由,含 `/api` JSON 接口与遗留 HTMX 端点)、`storage`(PG 迁移)、`scripts`(冒烟与实跑)、`frontend`(Vue3 + Element Plus + Vite SPA)。

## 验收状态(P0–P7 最终交付)

- **离线测试**:143 项全绿(模型/网络/存储全替身,含存储离线降级契约测试与 SPA JSON API 测试);
- **冒烟**:graph / tools / writing / web 四套全部通过;
- **真实连通**:smoke_infra / smoke_intercept 在自备实例离线时告警降级/自动 SKIP,符合预期;
- **真模型实跑**:零替身完整走通一篇研报(9 章/298 素材/62 检索/¥0.42,report.md+html 落盘);
- **浏览器全流程走查**(2026-11-06):首页提交→thread 页 3s 轮询面板实时刷新→网关表单→/memory 离线降级横幅不卡死→/reports 浏览下载均正常;走查中发现并修复"Redis 离线炸穿 chat 链路"缺陷(补 service 门面逐段 best-effort 降级 + 连接快速失败超时,含 3 项回归测试);控制台无报错。

## 已知边界

- PDF 导出依赖 weasyprint(缺 Pango 时自动降级只出 HTML);MinerU 未部署时 PDF 上传走 pymupdf 纯文本抽取,复杂表格还原受限。
- 图表为 matplotlib 静态 SVG/PNG;行情数据依赖 AkShare 公开接口,时效性以抓取时间为准。
- 产出内容为 AI 生成,附免责声明,不构成投资建议;单机工装,无鉴权,勿暴露公网。
