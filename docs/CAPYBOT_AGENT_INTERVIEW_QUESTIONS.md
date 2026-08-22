# Capybot Apply Agent 开发面试问题清单

更新时间：2026-07-30

## 使用说明

这份清单用于准备 Agent 开发实习面试。问题按照面试官理解项目的正常顺序排列：

```text
项目价值
-> 数据获取与增量同步
-> Agent Runtime
-> Tool 与 MCP
-> Skill 与 Memory
-> 输出可靠性与评测
```

建议先准备标有“重点”的问题，再逐步补全其余问题。回答时优先使用以下结构：

```text
先讲为什么设计
-> 再讲怎么实现
-> 最后讲真实效果、边界和不足
```

---

## 一、项目价值

### 1. 重点：请你用一分钟介绍 Capybot Apply。它解决了什么实际问题？

准备重点：

- 用户联系大量 HR 后，难以记住每个岗位的当前进展。
- 系统将聊天记录整理为岗位机会，生成阶段、下一步行动、任务、草稿和风险。
- 强调它是求职进度决策 Agent，不是 BOSS 聊天展示页或自动发送工具。

### 2. 重点：用户从点击“导入 BOSS 聊天”到前端看到机会阶段和下一步建议，数据完整经历了什么流程？

准备重点：

- Chrome CDP 复用登录态。
- BOSS 只读导入并写入 PostgreSQL L1。
- 增量检测和 DecisionRouter。
- Opportunity Agent 动态调用 Tool、Skill 与 MCP。
- Self-check、CommitGate 和本地提交。
- Celery 后台执行，WebSocket 通知前端更新。

### 3. 用户实际能体验到哪些功能？这些功能如何帮助用户管理大量求职机会？

准备重点：

- 导入近 30 天聊天和岗位卡。
- 查看机会阶段、下一步行动和证据。
- 管理任务、回复草稿和风险。
- 上传简历并计算岗位契合度。
- 查看 Agent Tool Trace。
- 面试准备、岗位尽调和有依据的候选人回复。

### 4. 重点：这个项目中哪一步必须使用 Agent？为什么不能全部用规则或固定 Workflow 实现？

准备重点：

- 数据去重、增量判断、权限和提交校验适合确定性代码。
- 招聘语言理解、阶段推断、信息缺口判断和动态补证据需要 Agent。
- Agent 的价值不是生成文本，而是根据当前证据选择不同工具路径。

---

## 二、数据获取与建模

### 5. 系统如何登录 BOSS？Chrome CDP、Cookie、独立 Browser Profile 分别有什么作用？

准备重点：

- 使用专门的可见 Chrome，不读取系统默认浏览器 Profile。
- Cookie 和登录态保存在独立 Profile。
- CDP 用于连接和控制该浏览器，复用用户已经完成的登录。
- 登录态可能存在但聊天页不可读，需要区分不同状态。

### 6. BOSS 没有公开聊天 API，系统如何获取聊天记录和岗位信息？这算不算爬虫？

准备重点：

- 用户主动登录后，通过浏览器会话调用网页内部只读数据接口。
- 读取会话列表、历史消息和岗位详情，不使用官方开放 API 的名义。
- 属于浏览器自动化和只读数据采集。
- 不自动发送、不自动打招呼，不提供 BOSS 写操作。

### 7. `bossId`、会话 ID、岗位 ID 和 `message_id` 分别从哪里获得？

准备重点：

- 来源是 BOSS 页面和内部接口返回的原始结构。
- 会话和联系人通过 BOSS UID、conversation ID 关联。
- 岗位优先使用 platform job ID 建立机会身份。
- 消息优先使用服务端 message ID。

### 8. 消息如何去重？如果 `message_id` 缺失或异常怎么办？

准备重点：

- `message_id` 是第一优先级。
- 无可靠 ID 时使用发送方、时间、类型、正文或附件生成内容指纹。
- 数据库唯一约束和导入逻辑共同保证幂等。
- 每条消息记录首次出现的 import run。

### 9. 首次导入和后续导入有什么区别？为什么后续导入只分析新增消息？

准备重点：

- 首次导入建立 L1 和机会对象，并分批构建派生分析。
- 后续导入通过 message ID 或内容指纹计算 Delta。
- 无新增消息直接跳过，不调用 LLM。
- 只对变化机会执行轻量或完整 Agent 分析。

### 10. 为什么以 Opportunity 为核心对象，而不是直接以聊天会话作为核心对象？

准备重点：

- 用户真正管理的是岗位机会，不是聊天窗口。
- 一个会话可能出现多个岗位卡。
- 同一岗位可能关联联系人、阶段、任务、草稿、评分和风险。
- Opportunity 是聊天证据与求职决策之间的业务聚合对象。

---

## 三、Agent Runtime

### 11. 重点：Opportunity Agent 第一轮 LLM 输入包含哪些内容？为什么要保持精简？

准备重点：

- System Policy。
- 当前 Opportunity 的最小状态投影。
- 本次新增消息 Delta。
- 当前可用工具的名称、描述和 JSON Schema。
- 不预装完整聊天、整份简历、所有岗位详情和 Skill 正文。

### 12. 为什么不直接把完整聊天记录、岗位信息、简历和所有 Skill 一次性塞给 LLM？

准备重点：

- 减少 Token、延迟和无关噪声。
- 避免旧信息压过本次新增变化。
- 让工具调用具备可观测价值。
- 保留长期 Memory，但只在信息不足时读取。

### 13. 重点：请用一个具体例子解释 `Plan -> Tool Call -> Observation -> Replan -> Commit` 的完整循环。

建议案例：

```text
HR 对岗位职责描述模糊
-> LLM 加载 opportunity-due-diligence Skill
-> 调用 job_read
-> 发现本地岗位证据不完整
-> 调用 boss_fetch_job_detail
-> Observation 返回完整岗位要求
-> 如用工主体仍不明确，再调用 research_company
-> Agent 重新判断风险和下一步
-> CommitGate 校验证据后写入
```

### 14. 重点：当前系统为什么是真正的 Agent，而不是套了一层 LLM 的固定 Workflow？

准备重点：

- 固定的是安全协议和输出 Schema。
- LLM 可以零工具结束，也可以选择不同的 Memory、Skill 或 MCP。
- 工具结果会进入下一轮上下文并改变决策。
- 不同机会的工具路径和迭代轮数并不相同。

### 15. 为什么选择受限 Tool-Calling Agent，而不是完全自由 Agent？限制了哪些能力？

准备重点：

- 求职数据涉及隐私和账号安全。
- 只允许 Apply 白名单工具。
- 禁止 shell、文件通用操作、任意网页搜索和 BOSS 写操作。
- 限制工具调用预算与外部调用预算。
- 最终写入必须经过 Self-check 和 CommitGate。

### 16. 重点：`Delta Router + Delta Agent + Full Tool Loop` 分别负责什么？为什么需要三级增量架构？

准备重点：

- Delta Router 使用确定性信号处理无变化和明显低价值增量。
- Delta Agent 用一次轻量 LLM 分析简单新增消息。
- Full Tool Loop 处理真实 HR 回复、面试、风险和证据不足场景。
- 避免所有机会都承担完整 Agent 的延迟和成本。

---

## 四、Tool 与 MCP

### 17. 当前 Opportunity Agent 有哪些本地 Typed Tool？它们分别读取什么信息？

```text
memory_read：读取当前机会尚未展示的 L1 消息或 L2 事件
job_read：读取 PostgreSQL 中已有的岗位卡和版本化岗位快照
profile_read：读取脱敏候选人画像、技能和项目
```

需要说明：这些工具读取进程内已有事实，因此不包装成 MCP。

### 18. 重点：当前系统有哪些外部 MCP Tool？为什么是两个 MCP Server、三个 MCP Tool？

```text
BOSS MCP Server
- boss_refresh_opportunity
- boss_fetch_job_detail

Company Intel MCP Server
- research_company
```

准备重点：

- BOSS Server 负责当前机会的 BOSS 只读数据。
- Company Intel Server 负责公开公司信息。
- 不同权限、失败模式和数据来源使用独立 Server。

### 19. 本地 Typed Tool 和 MCP Tool 有什么区别？为什么 `memory_read` 不需要做成 MCP？

准备重点：

- Typed Tool 是进程内、低延迟、已有状态读取。
- MCP 是进程外能力边界，具有独立协议、权限和失败状态。
- 是否使用 MCP 取决于能力边界，而不是为了增加技术名词。

### 20. `boss_fetch_job_detail` 和 `research_company` 真的会获得新的外部信息吗？如何证明调用有价值？

准备重点：

- Tool Observation 返回新的岗位快照或 web source。
- Trace 记录新增 evidence ref。
- 最终结论必须引用新增外部证据，否则 Commit 前进行修复或拒绝提交。
- Tool Utility 记录空结果、新证据、证据是否被最终采用。

### 21. MCP 调用失败、超时或返回空结果时，Agent 如何继续运行和降级？

准备重点：

- 捕获工具异常并转为结构化 Observation。
- 保留本地 L1/L2/L3，不因 MCP 失败丢失已有分析。
- 证据不足时返回 `needs_review` 或 `insufficient_evidence`。
- 工具失败本身不能被写成岗位风险。

---

## 五、Skill 与 Memory

### 22. 重点：Skill 和普通 Prompt、MCP Tool 有什么区别？当前三个 Skill 分别解决什么问题？

```text
grounded-candidate-communication
处理需要简历、历史承诺或岗位依据的复杂 HR 回复

opportunity-due-diligence
处理岗位事实缺失、冲突和风险尽调

interview-preparation
处理面试确认和针对性准备
```

准备重点：

- Skill 是选择性加载的专业任务方法。
- MCP 是获得外部事实的执行能力。
- Skill 可以协调 Memory、Profile 和 MCP，但不替代它们。

### 23. 重点：Skill 如何实现渐进式披露？模型如何决定加载哪个 Skill，加载后为什么会看到新的工具？

准备重点：

- 第一轮只提供三个 Skill 的名称、用途和空参数 Schema。
- LLM 根据 HR 消息判断是否需要专业方法。
- 调用后返回对应 `SKILL.md` 正文。
- Runtime 在现有 allowlist 和 `allow_external` 权限内解锁该 Skill 所需工具。
- Skill 无法绕过外部权限或获取其他机会数据。

### 24. L1/L2/L3 三层记忆分别保存什么？首次分析和后续增量分析时如何生成与更新？

准备重点：

- L1：原始聊天、岗位卡和外部证据。
- L2：材料请求、面试邀请、状态变化和风险等结构化事件。
- L3：联系人和机会的长期摘要。
- 首次导入先写 L1，再由 Agent 构建 L2/L3。
- 后续只根据新增 Delta 更新相关事件和摘要。

---

## 六、输出、可靠性与评测

### 25. Agent 最终生成哪些结果？阶段、下一步行动、任务、回复草稿、风险和岗位契合度分别依据什么？

准备重点：

- Opportunity Agent 生成阶段、摘要、下一步、变化和建议。
- 每个关键结论必须引用聊天、岗位、简历或外部来源。
- JobFitEvaluator 独立计算岗位契合度。
- PriorityCalculator 确定性结合契合度、HR 互动、阶段、时效和风险。
- 今日行动台使用结构化阶段、任务和优先级，不再调用全局排序 Skill。

### 26. 重点：如何证明 Agent 的结论可靠且优于单次 LLM？

准备重点：

- 使用人工 Gold Set 或银标数据验证阶段和动作。
- 与一次性输入全部上下文的 One-shot 基线比较。
- 观察准确率、证据引用有效率、工具调用价值、Token、延迟和增量跳过率。
- Self-check 验证证据、阶段、重复任务和草稿安全。
- CommitGate 是不可变代码门禁，拒绝不合法或无证据输出。
- Trace 保留 Tool Call、Observation、证据采用、Token 和耗时，不保存完整思维链。

---

## 优先准备顺序

### 第一优先级

```text
1、2、4、11、13、14、16、18、22、23、26
```

这些问题决定面试官是否认可项目是一个真正的 Agent 系统。

### 第二优先级

```text
5、9、10、15、19、20、24、25
```

这些问题主要考察架构边界、工程设计和可靠性。

### 第三优先级

```text
3、6、7、8、12、17、21
```

这些问题用于补充用户价值、数据链路和异常处理细节。

## 常见追问

- 为什么不用 LangChain 或 LangGraph？
- 为什么需要 PostgreSQL，而不是 SQLite？
- Redis、Celery 和 WebSocket 分别解决什么问题？
- 如果 MCP 返回了错误信息，模型会不会被误导？
- 如果 Skill 每次都被调用，是否说明它的触发设计失败？
- 为什么岗位评分不是 Opportunity Agent 的一个 Skill？
- 如果有十万条机会，当前架构如何扩展？
- 当前系统最经不起面试官追问的地方是什么？
- 你在项目中做过的最大一次优化是什么？
- 如果重新设计一次，你会删除或修改什么？

## 回答原则

1. 不把所有普通函数都称为 MCP。
2. 不把固定 Prompt 包装成 Agent。
3. 不声称系统实现了 BOSS 官方 API。
4. 不夸大当前真实数据量。
5. 能区分已经验证的结果与未来计划。
6. 每个技术选择都先说明解决的问题，再说明实现。
7. 主动说明安全边界、失败降级和当前不足。
