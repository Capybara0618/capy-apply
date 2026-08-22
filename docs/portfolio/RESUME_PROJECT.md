# Capybot Apply 简历项目文本

## 推荐版本

**Capybot Apply：BOSS 求职进度决策 Agent**　2026.04 - 至今

**项目描述：** 面向“同时联系大量 HR 后难以持续跟踪进度”的痛点，基于本人真实 BOSS 求职数据构建本地决策 Agent；只读增量同步招聘聊天与岗位卡，结合 PDF 简历和求职偏好，自动生成机会阶段、下一步行动、待确认任务/草稿、岗位契合度、风险提示及可追溯 Agent Trace。

**技术栈：** Python、FastAPI、OpenAI Tool Calling、FastMCP、PostgreSQL、Redis、Celery、WebSocket、Chrome CDP、PaddleOCR、React

- 设计单 Opportunity Tool-Calling Agent，首轮仅输入 `goal + opportunity + delta`，由 LLM 按证据缺口动态调用 Memory、候选人画像、渐进式 Skill 及 BOSS/公司情报 MCP，形成 `Plan -> Tool -> Observation -> Replan -> Commit` 闭环。
- 构建增量 DecisionRouter，通过 `message_id`/内容指纹过滤无变化与平台自动消息，并对确定性单向冷联系执行 0 LLM 投影；在 41 个真实会话、57 个时间增量轮次 A/B 中，相比同模型全量 One-shot 减少 **60.00% LLM 调用、66.05% Token 和 63.37% 模型处理时间**。
- 实现 CommitGate 与 Evidence Ledger，对 Structured Output、证据存在性、阶段/行动状态机、重复建议和草稿安全进行提交前校验；所有阶段、任务、草稿和风险均可回溯原始消息或岗位快照，非法输出修复一次后仍失败则拒绝覆盖旧状态。
- 设计六维岗位契合度与机会优先级评估：解析 PDF 简历生成候选人画像，模型按简历/岗位/聊天证据评价技能、项目、Agent 相关性等维度，代码执行固定权重聚合与城市、周期、薪资、收费风险 Hard Cap。
- 基于 PostgreSQL、Redis、Celery 与 WebSocket 拆分事实存储、后台任务和实时通知，使导入、Agent 分析与评分异步执行并按机会局部刷新；建立完整 Trace 埋点，记录 LLM/tool 次数、Token、耗时、新增证据及最终采用证据。

## 可替换的工具优化亮点

若版面更强调 Agent 工具质量，可用下列条目替换最后一条：

- 针对 Memory Tool 空调用问题，删除自由查询词、限制单层一次读取并动态隐藏已满足能力；真实增量回放中工具执行由 21 次降至 11 次、空结果由 10 次降至 0，最终阶段+行动一致率保持 100%，Token 进一步减少 6.66%。

## 数据口径

- 真实 L1：41 个本人 BOSS 会话、267 条消息、40 个岗位卡；排除平台会话后形成 40 个有效机会和 57 个时间增量轮次。
- 质量参照是 LLM 双标注裁决参考集，不是人工 Gold Set；最终阶段+行动联合一致率为 Agent 100.00%、One-shot 62.50%。
- A/B 为控制变量关闭在线 MCP；MCP 另以真实登录态集成测试和 50 个受控工具压力场景验证。
- 压力场景中的聊天为受控合成数据，不冒充真实 HR 对话。
- BOSS 页面协议并非官方开放 API，只读补查可能因会话超窗、岗位下架或页面变化而 fail closed。

## 30 秒口述

我做的不是 BOSS 聊天展示页，而是一个帮助用户管理大量求职机会的决策 Agent。系统先增量同步聊天，把确定性无变化和单向冷联系以 0 LLM 处理；有真实 HR 互动时，模型从最小上下文出发，自主决定是否读取历史、候选人画像、Skill 或 MCP 新证据。最终阶段、下一步、任务、草稿和风险必须经过 CommitGate 才能写入 PostgreSQL，Trace 还能说明每次工具调用拿到了什么、是否真的被最终决策采用。
