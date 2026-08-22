# Capybot Apply

Capybot Apply 是一个本地运行的 **BOSS 求职进度决策 Agent**。它解决的不是“再做一个聊天窗口”，而是用户同时联系大量 HR 后，难以持续判断每个岗位走到哪一步、现在该做什么、哪些机会更值得投入。

系统只读同步用户授权的 BOSS 聊天与岗位卡，并结合 PDF 简历和求职偏好，生成：

- 六阶段机会进度与事实摘要；
- 下一步行动、待确认任务和中文回复草稿；
- 岗位契合度、机会优先级和风险提示；
- 可回溯到原始消息、岗位快照和简历画像的证据；
- `Plan -> Tool -> Observation -> Replan -> Commit` Agent Trace。

Capybot 不自动投递、不自动打招呼，也不会向 BOSS 输入框写入或发送消息。

## 核心体验

- **真实增量同步**：专用 Chrome Profile 复用登录态，通过 CDP 只读读取近 30 天会话；按 `message_id` 或内容指纹去重。
- **机会管道**：将聊天整理为岗位机会，展示待我行动、等待反馈、面试中、已结束等状态。
- **动态 Agent**：首轮只输入 `goal + opportunity + delta`，证据不足时由 LLM 自主选择 Memory、Skill、候选人画像或 MCP。
- **证据级决策**：阶段、行动、任务、草稿和风险必须引用 Evidence Ledger 中的合法证据。
- **简历匹配**：上传 PDF 后通过 PaddleOCR/文本解析生成 Markdown 与候选人画像，再执行六维岗位契合度评估。
- **实时任务**：导入、分析和评分由 Celery 后台执行；Worker 提交一个机会后，WebSocket 局部刷新页面。
- **可复现演示**：没有 BOSS 账号也能一键加载隔离中文场景，运行与生产相同的导入、Agent、评分和提交链路。

## 为什么它是真 Agent

机会 Agent 使用 **OpenAI Agents SDK Runner** 执行有界、动态 Tool-Calling Loop，
而不是固定地把所有函数依次执行：

```text
Minimal Bootstrap
-> OpenAI Agents SDK Runner
-> LLM Plan
-> optional Tool Call
-> Observation
-> LLM Replan
-> Compact Decision
-> CommitGate
-> PostgreSQL
```

本轮证据充分时，模型可以零工具直接结束；信息缺失、过期或冲突时才调用工具。

### 本地 Typed Tools

- `memory_read`：按需读取未展示的 L1/L2 历史；
- `job_read`：读取本地岗位证据；
- `profile_read`：仅在 HR 要求自我介绍、项目或经历时读取脱敏候选人画像；
- `skill_grounded_candidate_communication`：生成有简历与历史依据的候选人回复；
- `skill_opportunity_due_diligence`：调查缺失、冲突或高风险的岗位事实；
- `skill_interview_preparation`：将面试安排、岗位要求和候选人项目组织为准备简报。

模型首轮只看到三个 Skill 的用途，选择调用后才获得 Markdown 正文；Skill 还会在现有
白名单和外部权限内解锁所需证据工具，不能绕过 MCP 安全边界。

### FastMCP 进程外工具

- `boss_refresh_opportunity`：补查当前机会的最新聊天证据；
- `boss_fetch_job_detail`：补全当前机会绑定的 BOSS 岗位详情；
- `research_company`：查询当前机会公司的公开背景。

Capybot harness 将工具绑定到当前 `opportunity_id`。模型不能替换账号、岗位 ID、公司实体或自由搜索词；Client allowlist 与 MCP Server 暴露面共同限制能力。

### CommitGate

模型不能直接写数据库。最终决策必须经过：

- Pydantic Schema 校验；
- `boss_message:`、`boss_job_snapshot:`、`web_source:`、`candidate_profile:` 证据存在性校验；
- 阶段与行动状态机一致性校验；
- 重复建议合并；
- 低置信度降级；
- 回复草稿占位符、自动发送语义和无证据断言检查。

非法输出只允许有限次修复，仍不合法则 fail closed。

## 架构

```mermaid
flowchart LR
    A["专用 Chrome Profile"] --> B["CDP 只读同步"]
    B --> C["Normalizer / Dedup"]
    C --> D["PostgreSQL L1"]
    D --> E{"DecisionRouter"}
    E -->|"无有效增量"| F["0 LLM 跳过"]
    E -->|"确定性冷联系"| G["冷机会投影"]
    E -->|"语义变化"| H["OpenAI Agents SDK Loop"]
    H <--> I["Memory / Job / Profile / Skill"]
    H <--> J["BOSS / Company MCP"]
    H --> K["CommitGate"]
    K --> L["L2/L3 + Suggestions"]
    D --> M["JobFitEvaluator"]
    M --> L
    L --> N["Redis Pub/Sub"]
    N --> O["WebSocket 局部刷新"]
```

- **PostgreSQL**：唯一事实库，保存 L1 原始证据、L2 事件、L3 摘要、任务、评分、Job 与 Trace。
- **Redis**：Celery Broker/Backend、Worker 心跳和实时失效事件，不保存业务事实。
- **Celery**：执行同步、Agent 分析、岗位评分和重建等长任务。
- **FastAPI + React**：提供 API、WebSocket 和六个中文业务页面。

详见 [技术架构](docs/ARCHITECTURE.md)、[面试讲解](docs/INTERVIEW_GUIDE.md)、[演示指南](docs/portfolio/DEMO_GUIDE.md) 和 [验证报告](docs/portfolio/CURRENT_VALIDATION.md)。

## 快速启动

前置条件：Python 3.11+、[uv](https://docs.astral.sh/uv/) 和 Docker Desktop。

```bash
uv sync --frozen --extra dev
uv run capybot apply configure
uv run capybot apply start
```

`apply start` 会启动或检查 PostgreSQL、Redis、Alembic、Celery Worker 与 FastAPI，并打开：

```text
http://127.0.0.1:8765/apply
```

进入“导入”页后：

1. 没有可用 BOSS 账号时，点击“一键中文演示”。
2. 使用真实数据时，先点击“打开 BOSS 专用浏览器”并完成登录。
3. 点击“导入近 30 天聊天”，随后在机会、任务和 Agent 页观察后台结果实时出现。

模型配置保存在本机 `~/.capybot/apply/local_settings.json`。不要提交 API Key。

## 验证

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

当前验证结果与实验边界见 [CURRENT_VALIDATION.md](docs/portfolio/CURRENT_VALIDATION.md)。核心真实 L1 动态回放包含 41 个源会话、267 条消息、57 个时间增量轮次；相比同模型全量 One-shot，Agent 减少 60.00% LLM 调用、66.05% Token 和 63.37% 模型处理时间。参考标签来自 LLM 双标注裁决集，不冒充人工 Gold Set。

## 隐私与边界

- 原始聊天、岗位、简历和 Cookie 保存在本机。
- 模型 Provider 会收到完成分析所需的脱敏片段，取决于用户配置。
- Trace 不保存完整 Prompt、完整简历或重复聊天正文。
- BOSS 页面接口并非官方开放 API，结构变化、岗位下架和会话超窗会导致只读补查失败。
- 项目不是 BOSS 官方产品，用户需自行承担账号使用风险。

## 来源与许可证

Capybot Apply 最初基于 [HKUDS/nanobot](https://github.com/HKUDS/nanobot) 探索并重构，现已删除原通用聊天 Agent、渠道、Provider 注册表、Shell/文件/Web 工具和兼容入口，形成独立的 Apply 运行闭包。原项目 attribution 与许可证保留于 [LICENSE](LICENSE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
