# Capybot Apply MCP 集成验证

更新日期：2026-07-28

## 1. 工具边界

Opportunity Agent 只连接两个独立 FastMCP stdio Server：

| Server | 工具 | 作用 |
| --- | --- | --- |
| BOSS Read-only | `boss_refresh_opportunity` | 补查当前机会的最新会话证据 |
| BOSS Read-only | `boss_fetch_job_detail` | 补全当前机会绑定岗位的完整详情 |
| Company Intelligence | `research_company` | 核验当前机会公司的公开背景与用工关系 |

模型不能传入任意账号、岗位 ID、公司实体或自由搜索词。Runtime 绑定当前
`opportunity_id`，Server 再从 PostgreSQL 读取平台身份并校验归属。

Memory、Job 与 Skill 是进程内 typed tools，不冒充 MCP。项目没有发送消息、自动
打招呼、自动投递、输入框写入或通用 Shell/Web 工具。

## 2. 真实 BOSS 登录态验证

在本机真实账号和专用 Chrome Profile 上完成过以下只读集成：

- `boss_fetch_job_detail`：从当前 BOSS 页面读取 1,622 字岗位职责与要求，写入一条
  版本化 `boss_job_snapshot`，端到端 12.81 秒。
- `research_company`：获得 4 条当前公司招聘来源，首次查询 8.54 秒；缓存命中后不再
  重复访问外部页面。
- `boss_refresh_opportunity`：目标五月会话已超出 BOSS 当前 30 天窗口，3.39 秒返回
  `boss_conversation_outside_window`；本地 L1 与原机会状态均未被覆盖。

该结果只证明当时账号、页面与岗位可用，不承诺 BOSS 非官方页面协议的长期 SLA。
岗位下架、会话超窗或实体不一致时必须 fail closed。

## 3. 可复现进程外闭环

隔离中文演示账号通过与生产相同的 FastMCP 子进程验证传输、证据与提交：

```text
Planner
-> boss_fetch_job_detail
-> MCP Observation
-> boss_job_snapshot evidence
-> Replan
-> CommitGate
```

| 指标 | 结果 |
| --- | ---: |
| LLM 调用 | 3 |
| 工具执行 | 1 |
| BOSS MCP 调用 | 1 |
| Agent 总耗时 | 28.384s |
| 新增岗位证据 | 1 |
| 最终采用岗位证据 | 1 |

该 run 的最终行动同时引用 HR 消息和 MCP 新增岗位快照。首次输出缺少任务与草稿，
CommitGate 拒绝写入；模型根据错误 Observation 修复后才提交为 `needs_review`。

演示账号的岗位响应来自仓库内 fixture，用于稳定复现 MCP 进程边界，不能表述为一次
真实 BOSS 网络请求。

## 4. Tool-Calling 压力集

另使用 50 个当时仍在招聘的 BOSS 实习岗位快照，构造 49 个交互密集受控机会：

- 50/50 run 通过 CommitGate。
- 31 次本地工具、32 次外部 MCP。
- `boss_fetch_job_detail` 24/24 返回新岗位证据，24 条全部被最终决策采用。
- `research_company` 8/8 返回 28 条公开来源证据，28 条全部被采用。
- `skill_discover -> skill_load` 完整发生 6 次，用于处理模糊活动/面试边界。

聊天是受控合成数据，不冒充真实 HR 对话；该压力集用于验证 Planner、工具闭环、
Evidence Ledger 与安全降级，不是生产准确率。

## 5. 工具价值判定

每次 Observation 记录：

```text
fact_count
novel_evidence_count
used_evidence_count
utility
duration_ms
```

`utility` 区分证据被采用、规则上下文、重复上下文、空结果和未采用新证据。外部 MCP
成功获得新证据但最终输出未引用时，Self-check 会要求模型修复；预算耗尽仍未引用则
拒绝提交。这样衡量的是“工具是否改变了可验证决策”，而不是单纯追求调用率。
