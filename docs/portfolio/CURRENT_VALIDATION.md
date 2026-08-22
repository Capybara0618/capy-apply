# Capybot Apply 当前验证报告

更新日期：2026-07-28

> 报告只公开匿名聚合指标，不包含聊天正文、联系人、简历、Cookie、完整 Prompt 或 API Key。真实数据、受控合成数据和演示 fixture 分开标注。

## 1. 自动化质量门

| 验证项 | 结果 |
| --- | --- |
| Python 全量测试（独立 PostgreSQL 测试库） | 207 passed，0 failed |
| Ruff | All checks passed |
| Vulture（90% 置信度） | 未发现未引用 Python 代码 |
| WebUI Vitest | 7 passed，0 failed |
| WebUI production build | 成功，JS 225.34 kB / gzip 71.71 kB |
| WebUI production dependency audit | 0 vulnerabilities |
| Alembic | `20260725_0017 (head)` |
| Offline Agent contract eval | 10/10 passed |
| Python sdist + wheel | 构建成功；Twine 与 wheel 内容检查通过，wheel 93 files |
| Wheel 隔离审计 | 无 nanobot 路径、无 API Key；全新虚拟环境安装及 CLI 冒烟测试通过 |

复现命令：

```powershell
$env:CAPYBOT_APPLY_TEST_DATABASE_URL="postgresql+psycopg://capybot:capybot@127.0.0.1:15432/capybot_apply_test"

uv run pytest -q
uv run ruff check capybot tests scripts
uvx vulture capybot scripts --min-confidence 90
uv run alembic current
uv run capybot apply eval

cd webui
npm test -- --run
npm run build
npm audit --omit=dev
```

## 2. 真实 L1 动态 Agent 与 One-shot A/B

### 数据与口径

- 源快照：41 个本人真实 BOSS 会话、267 条消息、40 个岗位卡；
- 排除 1 个平台系统会话，40 个有效会话按时间切成 57 个增量轮次；
- Agent 每轮只输入新增 turn、上一轮机会投影和按需工具；
- One-shot 基线每轮输入截至当时的全部历史、全部岗位资料和全部 Skill；
- 两组使用同一 `gpt-4o-mini`、同一决策 Schema 与同一 CommitGate；
- 为控制变量，该 A/B 关闭在线 MCP；MCP 在独立集成与压力测试中验证；
- 质量只在每个会话最终时间点与 LLM 双标注裁决参考集比较。

| 指标 | 增量 Agent | One-shot | 变化 |
| --- | ---: | ---: | ---: |
| 增量轮次 | 57 | 57 | - |
| 通过 CommitGate | 57 | 46 | +11 |
| LLM 调用 | 42 | 105 | **-60.00%** |
| Token | 104,917 | 309,030 | **-66.05%** |
| 模型累计处理时间 | 363.549s | 992.412s | **-63.37%** |
| 最终阶段一致率 | 100.00% | 70.00% | +30.00pp |
| 最终行动一致率 | 100.00% | 70.00% | +30.00pp |
| 最终阶段+行动联合一致率 | 100.00% | 62.50% | +37.50pp |

40 个零 LLM 轮次来自无变化、平台自动消息或可以确定证明的单向冷联系。真实 HR 的语义判断不会由 Router 冒充。

完整原始聚合报告：

- [DYNAMIC_AGENT_VS_ONESHOT_20260728.md](DYNAMIC_AGENT_VS_ONESHOT_20260728.md)
- [DYNAMIC_AGENT_VS_ONESHOT_20260728.json](DYNAMIC_AGENT_VS_ONESHOT_20260728.json)

## 3. 参考标签边界

41 个真实会话由两个 `gpt-5.6-sol` 仅依据脱敏 L1 独立标注，第三个同模型实例裁决主标签分歧：

| 指标 | 结果 |
| --- | ---: |
| 纳入评测 | 40 |
| 主标签完全一致率 | 95.12% |
| 阶段一致率 | 97.56% |
| 行动一致率 | 95.12% |
| 阶段 Cohen's Kappa | 0.9487 |
| 行动 Cohen's Kappa | 0.8863 |
| 裁决样本 | 2 |

它应称为 **LLM 双标注裁决参考集**，不是人工 Gold Set。三位标注者属于同一模型家族，仍可能共享偏差。详见 [标注规范](../GOLD_SET_PROTOCOL.md) 与 [匿名参考集报告](REFERENCE_SET_REPORT.md)。

## 4. Tool 与 Memory 优化

动态回放初版虽然达到最终参考一致，但有 10 次 `memory_read` 空结果。移除自由查询词、限制同层一次读取，并将非法层规范化到唯一可用层后：

| 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 实际工具执行 | 21 | 11 |
| 空结果 | 10 | 0 |
| Agent Token | 112,402 | 104,917 |
| 最终阶段+行动一致率 | 100.00% | 100.00% |

在质量不回退的前提下，工具执行减少 47.62%，Token 进一步减少 6.66%。详见 [MEMORY_TOOL_OPTIMIZATION.md](MEMORY_TOOL_OPTIMIZATION.md)。

## 5. MCP 闭环

### 真实登录态只读验证

- `boss_fetch_job_detail`：从当时可用的 BOSS 岗位页读取 1,622 字职责与要求，生成版本化岗位快照；
- `research_company`：获取 4 条当前公司招聘来源并支持缓存；
- `boss_refresh_opportunity`：对超出 30 天窗口的历史会话返回 `boss_conversation_outside_window`，没有覆盖本地 L1 或旧状态。

### 可复现演示闭环

隔离中文演示账号使用与生产相同的 FastMCP 子进程：

```text
Planner
-> boss_fetch_job_detail
-> MCP Observation
-> boss_job_snapshot evidence
-> Replan
-> CommitGate
```

代表性运行中，Agent 主动调用 BOSS 岗位 MCP，下一轮引用新增岗位快照；首次决策缺少必要任务/草稿，被 CommitGate 拒绝，修复后才提交。

### Tool 压力集

使用 50 个当时仍在招聘的 BOSS 实习岗位事实，构造 50 个受控交互场景：

- 50/50 通过 CommitGate；
- 31 次本地工具、32 次外部 MCP；
- 岗位详情 24/24 返回并采用新证据；
- 公司研究 8/8 返回 28 条来源证据并全部采用；
- `skill_discover -> skill_load` 完整发生 6 次。

岗位事实来自真实在招页面，聊天是受控合成数据，不冒充真实 HR 对话，也不将 100% 回归命中率表述为生产准确率。详见 [MCP 集成报告](MCP_INTEGRATION_REPORT.md) 与 [Tool 压力集](TOOL_STRESS_BENCHMARK_20260726.md)。

## 6. 安全与故障回归

测试覆盖：

- Apply Runtime 不暴露 Shell、文件、自由网页搜索或 BOSS 写工具；
- 工具被绑定到当前账号与机会，模型不能越权替换实体；
- `message_id` 证据必须真实存在；
- 重复任务合并，拒绝后的同证据建议不会刷屏；
- 草稿包含模板占位符、自动发送语义或无证据经历时拒绝写入；
- HR 要求自我介绍时，Agent 按需调用 `profile_read`，只读取脱敏画像；
- 图片、附件和平台卡不再误标为 `system`；
- 无新增消息不调用 LLM，真实 HR 回复才进入 Agent；
- Worker 重投通过持久 Job 与 PostgreSQL advisory lock 防止并发重复执行；
- Trace 不保存完整 Prompt、完整简历或聊天副本。

## 7. 仍然存在的边界

- BOSS 页面协议不是官方开放 API，页面结构变化可能导致同步或 MCP 补查失效；
- 真实招聘数据规模是个人求职量级，不是生产流量压测；
- 动态 A/B 参考集不是人工 Gold Set；
- 模型与中转站网络延迟仍是交互式分析的主要耗时；
- Tool 压力集验证闭环和安全性，不代表线上招聘成功率；
- 系统只生成建议和草稿，最终决策与消息发送始终由用户完成。
