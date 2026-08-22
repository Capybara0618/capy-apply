# Capybot Apply 面试演示指南

## 1. 一键启动

前置条件：Docker Desktop 已运行。

```powershell
cd D:\Capybot
uv run capybot apply start
```

该命令会检查或启动 PostgreSQL、Redis，执行 Alembic migration，再启动 Celery
Worker、FastAPI 和 React WebUI：

```text
http://127.0.0.1:8765/apply
```

健康检查：

```powershell
uv run capybot apply doctor
```

## 2. 载入隔离中文演示

面试现场优先使用可复现演示，不依赖 BOSS 当天登录态、30 天窗口或历史岗位是否下架：

在“导入”页点击“一键中文演示”，或运行：

```powershell
uv run capybot apply demo
```

演示数据使用独立账号 `capybot_demo`，不会覆盖真实账号。它经过与生产相同的
`SnapshotImporter -> DecisionRouter -> OpenAI Agents SDK Loop -> JobFitEvaluator`
链路，并实际写入 PostgreSQL、进入 Celery 队列、通过 WebSocket 刷新页面。

页面顶部会明确显示“演示数据”，不能把这些对话表述为真实 HR 数据。

## 3. 推荐演示顺序

### 总览：先讲产品价值

展示“待我行动、等待反馈、面试中、高风险机会和最近变化”。说明核心对象是岗位机会，
不是聊天窗口；系统的目标是帮助用户在大量 HR 对话中决定“现在先处理什么”。

### 机会：展示可执行输出

依次打开材料请求、面试邀请和收费风险场景，展示：

- 当前阶段和下一步行动。
- 待确认任务与回复草稿。
- 岗位契合度和机会优先级。
- 原始消息、岗位卡及简历证据。

强调阶段是已发生事实，Agent 建议在用户确认前不会伪装成事实。

### Agent：展示动态 Loop

选择不同 run 对比：

```text
Minimal Bootstrap
-> Plan
-> optional Tool Call
-> Observation
-> Replan
-> Normalize / Validate / Commit
```

重点指出：

- 无价值增量由 `DecisionRouter` 以 0 LLM 跳过。
- 证据足够时 Agent 可以零工具早停。
- 存在隐藏历史时才披露 `memory_read`。
- HR 要求自我介绍或项目经历时，才披露脱敏 `profile_read`。
- Bootstrap 首轮只披露三个 `skill_*` 的用途；模型仅在复杂沟通、岗位尽调或面试准备
  场景中调用，下一轮才获得对应 Markdown 正文和该 Skill 允许的证据工具。
- 缺少当前岗位或公司事实时，Planner 才调用独立 FastMCP Server。
- Trace 记录参数摘要、Observation、证据采用、Token 和耗时，不保存思维链或完整
  Prompt。

### 简历：展示严谨评分

上传 PDF 简历或编辑解析后的 Markdown，再填写目标岗位、城市、薪资、实习时间和排除项。
说明岗位评分与进度 Agent 分离：

- LLM 只负责六维证据匹配。
- 总分由代码按固定权重计算。
- 城市、周期、薪资、非实习和收费风险由确定性策略复核并封顶。
- 简历、偏好或岗位上下文变化后，旧评分失效并后台重算。

### 任务：展示安全边界

展示接受、拒绝、编辑、完成和延期。系统只生成草稿，不存在 BOSS 发送、打招呼、
自动投递或输入框写入工具。

### 后台任务：展示工程闭环

打开任务中心，展示导入、路由、Agent 分析和评分任务状态。解释：

```text
FastAPI 入队
-> Redis / Celery Worker
-> PostgreSQL Commit
-> Redis Pub/Sub
-> WebSocket 局部刷新
```

## 4. 可选真实账号演示

只有网络和账号条件稳定时再演示：

1. 打开“导入”页。
2. 点击“打开 BOSS 专用浏览器”。
3. 在独立 Chrome Profile 中扫码登录。
4. 点击“导入近 30 天”。
5. 展示 `message_id`/内容指纹去重、增量报告和后台分析。

真实导入依赖 BOSS 非官方网页结构，历史记录会保留在 PostgreSQL，不会因当前列表为空
而删除。

## 5. 演示前验收

```powershell
$env:CAPYBOT_APPLY_TEST_DATABASE_URL="postgresql+psycopg://capybot:capybot@127.0.0.1:15432/capybot_apply_test"
uv run pytest -q
uv run ruff check capybot tests scripts
uvx vulture capybot scripts --min-confidence 80
uv run capybot apply eval

cd webui
npm test -- --run
npm run build
npm audit --omit=dev
```

最新可复现结果与已知限制见 [CURRENT_VALIDATION.md](CURRENT_VALIDATION.md)。

## 6. 不夸大的边界

- 双标注裁决参考集不是人工 Gold Set。
- 受控演示和合成压力集不是真实 HR 对话。
- Redis/Celery 改善请求响应和任务可靠性，不会加速模型本身。
- BOSS 内部页面协议不是官方稳定 API。
- MCP 调用率不是越高越好；应同时报告新证据和最终采用证据。
