# Capybot Apply 技术架构

## 1. 产品边界

Capybot Apply 将分散的招聘对话转成可追踪、可解释、可执行的求职机会决策。核心对象是 `Opportunity`，不是聊天框：一个会话可以关联多个岗位；没有岗位卡时建立低置信度“待补全机会”。

系统只读 BOSS，不实现发送消息、自动打招呼、自动投递、输入框写入、Shell、文件操作或自由网页搜索。

## 2. 运行组件

| 组件 | 职责 |
| --- | --- |
| FastAPI | HTTP API、React 静态资源、WebSocket 广播 |
| Celery Worker | BOSS 同步、增量路由、Agent、评分和重建 |
| PostgreSQL | 唯一事实库与后台任务事实记录 |
| Redis | Celery Broker/Backend、Worker 心跳、Pub/Sub |
| Chrome + CDP | 复用独立登录态并执行只读页面请求 |
| OpenAI Agents SDK Runner | 模型轮次、Tool Call、Observation 与停止条件 |
| Capybot Apply Harness | 最小上下文、工具披露、证据账本、CommitGate 与 Trace |
| JobFitEvaluator | 简历与岗位的六维证据评分 |
| CommitGate | Schema、证据、状态一致性与安全门禁 |
| React WebUI | 总览、机会、任务、Agent、简历、导入 |

`capybot apply start` 依次检查 Docker 基础设施、执行 Alembic、启动 Worker 和 FastAPI。长任务不会在 HTTP 请求内执行：

```text
FastAPI 创建 apply_job
-> Redis Queue
-> Celery Worker
-> PostgreSQL Transaction
-> Redis apply.events
-> FastAPI WebSocket
-> React 局部刷新
```

`apply_jobs` 是任务状态事实。Redis 队列或缓存丢失不会改变已提交的业务事实。

## 3. BOSS 登录与证据同步

Capybot 使用独立 Chrome Profile：

```text
~/.capybot/browser/boss-profile
```

Cookie 与缓存由 Chrome 自己维护。系统不读取默认 Chrome/Edge Profile，也不要求用户粘贴 Cookie。

CDP 负责连接专用 Chrome、导航、执行只读页面请求和读取响应；`BossConnector` 负责理解会话列表、`boss_uid`、history message、岗位卡和登录状态。同步流程：

```text
识别当前账号
-> 扫描近 30 天会话
-> 按 boss_uid 分页读取消息
-> 提取岗位卡
-> Normalizer
-> PostgreSQL L1 upsert
-> import_run / import_run_items
```

消息优先按 BOSS `message_id` 去重；无可靠 ID 时使用发送方、时间、类型、文本或附件指纹。标准类型为：

```text
text image file job_card platform_card auto_followup system unknown
```

平台卡、VIP 自动追问、附件和真实 HR 文本不会再被统一误标为 `system`。一次空导入只有在页面明确返回“近 30 天无会话”时才算合法；当前页面为空不会删除 PostgreSQL 中的历史证据。

## 4. PostgreSQL 与三层记忆

- **L1**：原始消息、岗位卡、版本化岗位快照和公开网页来源；
- **L2**：阶段变化、材料、面试、等待反馈和风险等结构化事件；
- **L3**：机会摘要与联系人摘要。

Agent 首轮只接收最新机会投影和本次增量。更早信息通过 L3 压缩；确实需要原始细节时再读取 L1/L2。证据抽屉始终可以从结论回查 L1。

统一 `suggestions` 保存 task、draft、risk 和 stage 建议，并按机会、类型、内容与证据指纹去重。用户接受、编辑或拒绝后的建议不会被相同建议覆盖。

## 5. 增量入口：DecisionRouter

Router 只处理确定性边界，不替模型理解 HR 语义：

- 无新增消息，或只有 `system/platform_card/auto_followup`：跳过，0 LLM；
- 首次导入只有候选人单向联系：投影为 `discovered + wait`，不制造回复或催促任务；
- 出现真实 HR 消息、用户手动研究或语义不确定性：进入机会 Agent。

这让系统保留 Agent 处理复杂语义的价值，同时避免每次登录都重复分析全部历史。

## 6. OpenAI Agents SDK Loop 与领域 Harness

OpenAI Agents SDK 的 `Runner` 负责模型轮次、Function Tool 调用、Observation 回填、
重新规划和最大轮次终止。Capybot 不再维护第二套手写循环；它保留的自研部分是求职场景
Harness：Bootstrap Context、工具可见性、MCP 权限、Evidence Ledger、CommitGate、
DecisionCommitter 和本地 Trace。模型 Provider 通过适配层继续兼容用户配置的
OpenAI-compatible 中转站。

### 最小 Bootstrap

初始输入只有三个顶层对象：

```json
{
  "goal": "判断当前机会进度并给出有证据的下一步",
  "opportunity": {},
  "delta": {}
}
```

不会预装完整聊天、整份简历、所有岗位资料或全部 Skill。`OpportunityBootstrapBuilder` 同时计算当前信息缺口和本轮可见能力。

### 动态工具披露

本地 typed tools：

- `memory_read`：读取首轮未展示的当前机会 L1/L2；
- `job_read`：读取已有岗位证据；
- `profile_read`：仅当 HR 要求自我介绍、项目或经历时读取脱敏画像；
- `skill_grounded_candidate_communication`：HR 追问项目、技术、经历或个人条件时，
  指导模型基于 Memory、候选人画像和岗位证据生成不夸大的回复；
- `skill_opportunity_due_diligence`：岗位职责、用工主体或条件缺失、冲突或有风险时，
  指导模型交叉验证岗位详情和必要的公司公开信息；
- `skill_interview_preparation`：明确面试邀请或时间确认时，指导模型核对历史安排、
  补查岗位要求并生成确认草稿与针对性准备任务。

每个 Skill 都是包含 YAML 元数据和正文的独立 `SKILL.md` 目录。模型先看到名称、
用途和空参数 Schema，第一轮根据 HR 消息决定是否调用，第二轮才读取正文；普通状态
变化可以不调用 Skill。加载 Skill 只会在现有 allowlist 和 `allow_external` 权限内
披露它需要的 Memory、Profile 或 MCP 工具。岗位契合度由独立 `JobFitEvaluator`
按照固定维度、证据约束和硬性封顶策略计算，不再包装成 Skill。

FastMCP stdio tools：

- `boss_refresh_opportunity`：补查当前机会最新聊天；
- `boss_fetch_job_detail`：补全当前机会绑定岗位详情；
- `research_company`：查询当前公司公开背景。

工具仅在可能增加信息时出现；执行成功后会从后续目录隐藏，防止重复调用。Memory 不接受自由查询词，同一层每个 run 最多执行一次。

### MCP 安全边界

进程内状态读取使用 typed tools，进程外 BOSS/公开情报能力才使用 MCP。Capybot harness 自动绑定当前 `opportunity_id`，模型无法传入任意账号、岗位、公司或搜索词。MCP Server 再次校验机会归属、页面实体、URL/SSRF 和来源等级。

每个 Observation 记录：

```text
fact_count
novel_evidence_count
used_evidence_count
utility
duration_ms
```

工具取回了新证据但最终决策未引用时，Validator 会要求模型修复；预算耗尽仍未引用则拒绝提交。

### 循环与停止

```text
Plan
-> optional Tool Call
-> Observation
-> Replan
-> Compact Decision
```

循环是有界的，但路径不固定。证据充分时可以一轮直接结束；工具失败时可以基于本地证据降级；达到预算仍不能形成合法结论时 fail closed。

## 7. Compact Decision 与 CommitGate

Agent 最终只输出：

```text
status
stage
summary
next
changes
suggestions
confidence
```

`next` 包含行动、负责人、时机、理由和证据；`suggestions` 只包含 task、draft 或 risk。

写库前依次经过：

1. `DecisionNormalizer`：只做可解释的确定性规范化；
2. `DecisionValidator`：校验 Schema、Evidence Ledger、阶段/行动一致性、任务重复、低置信度和草稿安全；
3. `DecisionCommitter`：在事务中更新 L2/L3 和建议；
4. `AgentRunRepository`：保存 Trace、token、耗时和工具效用。

合法证据命名空间：

```text
boss_message:
boss_job_snapshot:
web_source:
candidate_profile:
```

模型不能直接写数据库。非法决策最多修复一次；仍失败则不覆盖已有有效状态。

## 8. 岗位契合度

进度决策与简历评分使用不同输入和校验，因此由独立 `JobFitEvaluator` 完成。

1. PDF 先提取文本；扫描件通过 PaddleOCR，转成 Markdown 后保存；
2. 生成候选人画像和五项求职偏好；
3. LLM 对目标方向、核心技能、项目经历、Agent/LLM 相关性、地点/时间、薪资/风险提供证据级判断；
4. 代码按 `15/25/25/15/10/10` 固定权重聚合；
5. 代码复核排除项、非实习、城市、周期、薪资和收费风险 Hard Cap；
6. 机会优先级再结合 HR 互动、紧迫度、活跃度和风险。

没有简历画像时不生成假分数；岗位事实不足或证据冲突时状态为 `needs_review`。

## 9. Trace 与前端

Trace 保存：

- Bootstrap 摘要与信息缺口；
- 每轮 Planner 选择；
- 工具参数摘要与 Observation；
- 最终结构化决策；
- CommitGate 错误、修复与提交结果；
- 工具新增证据是否被最终采用；
- LLM/tool/总耗时与 token。

Trace 不保存完整 Prompt、完整简历或重复聊天正文。WebUI 的 Agent 页可展开以上步骤，机会页通过证据抽屉展示原始消息。

## 10. 故障策略

- PostgreSQL 不可用：Apply 操作被阻止；
- Redis 不可用：只允许查看已保存事实，禁止创建长任务；
- Worker 离线：任务不入队，并给出中文修复提示；
- BOSS 登录过期：保留旧数据，要求重新登录后再同步；
- 会话超出 30 天或岗位下架：MCP 返回结构化失败，不伪造新证据；
- 模型输出非法：在有界预算内修复，仍失败则保留旧状态并记录 Trace；
- Worker 中断：持久 Job、晚确认和 PostgreSQL advisory lock 防止重复执行。

## 11. 为什么没有 LangGraph、向量库和多 Agent

当前核心是一个机会内的短、有界循环。OpenAI Agents SDK 负责通用 Tool Loop，
Capybot harness 控制动态工具目录、证据账本、修复次数和提交门禁；再引入 LangGraph
不会自动提升质量。

单个机会的历史已被 L1/L2/L3 压缩，暂无需要向量检索的大规模语料。阶段、草稿和风险共享同一上下文，拆成多个角色会增加 token、延迟和状态冲突。因此当前使用一个 Opportunity Agent、一个独立 Fit Evaluator 和确定性 Router/CommitGate。

当出现跨小时中断恢复、并行研究分支或多角色审批时，再引入图编排或多 Agent 更合理。
