# Capybot Apply 面试讲解

## 30 秒介绍

Capybot Apply 是我基于真实 BOSS 求职数据做的机会决策 Agent。它只读增量同步聊天与岗位卡，把多段招聘对话整理成机会阶段、下一步行动、任务、草稿、风险和岗位契合度。复杂变化进入一个受限 Tool-Calling Agent：模型从最小上下文出发，按需调用 Memory、候选人画像、渐进式 Skill 或 BOSS/公司 MCP；输出必须通过证据账本和 CommitGate 才能写库。后台使用 PostgreSQL、Redis、Celery 和 WebSocket，页面能逐个显示 Agent 完成的机会。

## 用户价值

用户联系很多 HR 后真正需要的是：

- 谁正在等我回复；
- 哪些只是单向自荐，应该降温而不是反复催促；
- 简历或材料是否已发送；
- 面试时间是否需要确认；
- 哪些岗位更适合投入时间；
- 每个结论依据哪条原始聊天或岗位事实。

Capybot 给出辅助决策，但从不替用户发送消息。

## 一次运行发生什么

1. 专用 Chrome Profile 复用本地登录态，CDP 只读同步近 30 天证据；
2. Normalizer 识别真人消息、平台消息、附件和岗位卡并写入 PostgreSQL L1；
3. DecisionRouter 对无变化、平台消息和确定性冷联系执行 0 LLM 路由；
4. 真实 HR 语义变化进入 OpenAI Agents SDK 驱动的机会 Agent；
5. 首轮只有 `goal + opportunity + delta`，ToolBox 按信息缺口暴露能力；
6. LLM 可以结束，也可以调用工具，Observation 会成为下一轮输入；
7. Compact Decision 经过 Normalize、Validate、Commit；
8. Worker 发布 Redis 事件，WebSocket 通知当前页面局部刷新；
9. JobFitEvaluator 独立读取简历、偏好和岗位证据计算匹配与优先级。

## 每轮 LLM 输入是否相同

不同。

- **第一轮**：系统协议、精简机会投影、本次新增消息和当前可用工具 Schema；
- **工具轮**：保留上一轮上下文，并追加工具调用与结构化 Observation；
- **修复轮**：追加 CommitGate 的具体错误，只允许修复一次；
- **评分轮**：属于独立 Evaluator，输入候选人画像、岗位事实和聊天补充，使用固定维度、
  证据约束与硬性封顶策略，不属于机会 Agent 的 Skill。

完整聊天不会每轮重复塞入；早期历史使用 L2/L3，只有需要时回查 L1。

## 为什么不是固定 Workflow

固定的是安全协议，不是工具路径。LLM 可以：

- 证据足够时零工具结束；
- 缺历史时调用 `memory_read`；
- 缺岗位时调用本地 `job_read` 或 BOSS MCP；
- HR 要求自我介绍时调用 `profile_read`；
- 遇到模糊招聘场景时，直接选择当前已披露的 `skill_*` 工具加载对应领域规则；
- 用户要求公司背景时调用受限 `research_company`；
- 根据 Observation 继续补查或直接结束。

真实演示 Trace 已出现：

```text
Planner -> profile_read -> Observation -> Replan
-> CommitGate 拒绝“只有草稿没有任务”
-> LLM 修复 -> Commit
```

也出现过：

```text
Planner -> boss_fetch_job_detail
-> 新 boss_job_snapshot
-> Replan -> 最终引用新增证据
```

## MCP 与本地工具的边界

进程内、低延迟、已有状态读取使用 typed local tools：

```text
memory_read
job_read
profile_read
skill_grounded_candidate_communication
skill_opportunity_due_diligence
skill_interview_preparation
```

进程外、需要独立权限与失败边界的能力使用 FastMCP stdio：

```text
boss_refresh_opportunity
boss_fetch_job_detail
research_company
```

模型不能传账号、岗位 ID、公司名或自由查询词，Capybot harness 自动绑定当前机会。这样 MCP 是真实外部能力边界，而不是给普通数据库函数换名。

## 为什么只做一个 Agent

阶段、任务、草稿和风险都依赖同一份机会证据。拆成多个角色会重复读取上下文、增加 token 与状态冲突。因此当前设计是：

- 一个 OpenAI Agents SDK Loop 负责机会语义决策；
- 一个独立 JobFitEvaluator 负责简历与岗位评分；
- 一个确定性 DecisionRouter 负责 0 LLM 边界；
- 一个确定性 CommitGate 负责不可变安全门禁。

这是职责分离，不是用 Agent 数量制造复杂度。

## 为什么 SDK 和自研 Harness 同时存在

OpenAI Agents SDK 解决通用循环问题：模型调用、Function Tool、Observation 回填、重新规划
和最大轮次终止。Capybot 自研 Harness 解决招聘领域问题：哪些上下文首轮可见、哪些 Skill/MCP
可以暴露、证据 ID 是否真实存在、结论能否写入 L2/L3、草稿是否越权。这样既避免重复造
Agent Loop，也保留项目真正可被追问的领域工程价值。

## 最值得讲的四个取舍

### 1. 增量而非全量 Prompt

真实动态回放使用 41 个源会话、267 条消息和 57 个时间增量轮次。相比同模型全量 One-shot：

- LLM 调用减少 60.00%；
- Token 减少 66.05%；
- 模型处理时间减少 63.37%；
- 最终阶段+行动参考一致率为 100.00%，One-shot 为 62.50%。

参考标签来自 LLM 双标注裁决集，不宣称人工 Gold 准确率。

### 2. 工具必须产生价值

每个 Observation 记录事实数、新增证据数和最终采用证据数。Memory 初版有 10 次空结果；删除自由查询、限制单层一次读取后，工具执行从 21 降到 11、空结果清零、Token 再降 6.66%，质量不回退。

### 3. 模型不能直接写库

模型曾输出聊天原文而不是证据 ID，也曾生成 `[您的名字]` 占位符草稿。CommitGate 会拒绝：

- 不存在的 evidence ref；
- 阶段与行动冲突；
- 回复动作缺少任务或草稿；
- 重复任务；
- 无证据事实；
- 模板占位符和自动发送语义。

在有界预算内修复仍失败则保留旧状态，不能让低质量新结果覆盖有效历史。

### 4. 评分不是一个随手分数

模型只给六个维度的证据级判断；代码固定加权并复核城市、周期、薪资、排除行业与收费风险 Hard Cap。没有简历时不显示假分数，岗位事实不足时进入 `needs_review`。

## 常见拷打

**为什么不用 LangGraph？**

当前是单机会、短、有界循环，OpenAI Agents SDK 已负责通用 Tool Loop，Capybot harness
负责动态工具、证据账本、修复预算和 CommitGate。跨小时恢复、并行研究分支或复杂审批图
出现后再引入 LangGraph。

**为什么不用向量数据库？**

单机会历史规模有限，L1/L2/L3 已能压缩与精确回查。现在引入向量库会增加索引一致性和评测成本，却没有证明能提升结果。

**MCP 调用越多越好吗？**

不是。拒绝、材料请求等结论已有直接聊天证据，强制外查只增加延迟。系统衡量的是新证据是否进入最终决策，而不是调用率。

**这个项目是不是爬虫？**

数据入口具备浏览器自动化与页面数据读取特征，但项目核心不是批量抓取，而是用户授权、本机、只读、低频的个人数据同步，以及后续增量决策 Agent。非官方页面协议不稳定是明确边界。

**100% 是否可信？**

它是 40 个有效真实会话最终时间点相对 LLM 双标注裁决参考集的一致率，不是生产准确率，更不是求职成功率。报告同时给出样本、标注方法、基线和限制。

**如果 BOSS 页面或模型失败怎么办？**

BOSS 超窗、岗位下架和页面变化返回结构化失败，不删除本地 L1；模型非法结果不写库；Worker 中断通过持久 Job 与 advisory lock 安全重投。旧有效状态始终优先保留。

## 演示顺序

1. 导入页点击“一键中文演示”；
2. 总览展示待我行动、高契合机会和风险；
3. 机会详情展示阶段、评分、草稿和原始证据；
4. Agent 页打开 `profile_read` 或 BOSS MCP run；
5. 逐步说明 Plan、Tool、Observation、CommitGate 拒绝与修复；
6. 最后展示验证报告中的真实 L1 A/B 和边界。
