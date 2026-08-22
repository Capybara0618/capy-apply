# Capybot 独立化与精简审计

## 当前运行闭包

```text
CLI / FastAPI / Celery
-> Apply Domain
-> OpenAI Agents SDK Loop + Capybot Apply Harness
-> JobFitEvaluator + CommitGate
-> typed local tools + 2 MCP Servers
-> PostgreSQL / Redis
```

## 已删除

- `nanobot` 包名、CLI 和配置兼容入口。
- 原通用聊天 AgentRunner、Provider 注册表、多渠道消息、Session、Cron 和通用工具。
- Shell、文件、通用网页搜索和 BOSS 写操作。
- 旧规则分析器、旧 Tool Loop、Delta Agent/Full Agent 双运行时。
- 自研 `OpportunityAgent` 循环；模型迭代与工具执行统一交给 OpenAI Agents SDK Runner。
- SQLite 运行、回退和迁移代码。
- `OpportunityHarness`、旧 Context 和 policy 文件。
- `analysis_runs` 与旧 tasks/drafts/review 三表。
- SQLAlchemy metadata 中已下线表定义。
- 旧 `pursuit recommendation` 和重复评分字段。

## 当前保留理由

- `DecisionRouter`：0 LLM 增量过滤与可证明的冷会话投影，不处理真实 HR 语义。
- OpenAI Agents SDK Runner：唯一模型与工具迭代核心。
- Capybot Apply Harness：领域上下文、工具权限、证据门禁和事务提交。
- `JobFitEvaluator`：评分与进度决策需要不同证据和校验。
- `CommitGate`：代码级不可变边界，不让模型直接写库。
- 历史 Alembic revisions：保证已有 PostgreSQL 可以升级，不属于运行时业务逻辑。

## MCP 边界

- 机会 Agent 的 Memory/Job/Skill 使用 typed local tools；仅在 HR 要求介绍经历时按需暴露
  脱敏 `profile_read`。完整简历与岗位评分仍属于独立 Fit 边界。
- BOSS 会话/岗位详情和公司公开信息使用 FastMCP stdio。
- Client allowlist 与 Server 暴露工具形成双重限制。
- 不存在发送、打招呼、输入框写入或点击发送工具。

## 质量门

- PostgreSQL 测试库全量 Apply tests。
- Ruff 扫描 `capybot`、`tests/apply` 和 `scripts`。
- 空 PostgreSQL 从零升级到唯一 Alembic head。
- 当前 schema 不存在旧三表。
- Vitest 与 TypeScript production build。
- 离线安全契约与 GPT-4o-mini 小样本 live eval。
- 真实账号只读导入、Opportunity Agent、JobFitEvaluator smoke。
