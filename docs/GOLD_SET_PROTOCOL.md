# Capybot Apply 参考集标注规范

## 目标

依据每个会话当前快照中已经出现的证据，标注机会阶段和此刻最合适的下一步行动。
不得推测 HR 内心、未来结果或聊天中未出现的岗位事实。

这套流程由两个模型独立标注并由第三个模型裁决分歧。未经人工复核时，产物应称为
“LLM 双标注裁决参考集”，不能称为人工 Gold Set。

## 输出格式

输出一个 JSON 对象：

```json
{
  "annotations": [
    {
      "case_id": "原样复制",
      "include": true,
      "stage": "waiting_feedback",
      "action": "wait",
      "confidence": "high",
      "evidence": ["boss_message:原样复制"],
      "reason": "一句简短、可审计的中文理由"
    }
  ]
}
```

每个输入 case 必须且只能输出一条 annotation，不得增加字段。

## 阶段

- `discovered`：存在岗位或候选人单向发起联系，但尚未形成有效双向沟通。
- `communicating`：双方已沟通，但当前没有明确待办、等待反馈或面试安排。
- `need_my_action`：HR 最后提出了尚未完成的问题、材料请求或确认请求。
- `waiting_feedback`：候选人已经回复、提交材料或完成当前动作，正在等待 HR。
- `interviewing`：已有明确面试邀请、时间协商、面试安排或面试后的反馈流程。
- `closed`：HR 明确拒绝、岗位关闭、已招满，或双方明确结束流程。

没有 HR 回复不等于拒绝。此类会话通常是 `discovered + wait`；如果候选人已经完成了
HR 先前要求的动作，则是 `waiting_feedback + wait`。

## 下一步行动

- `reply`：回答 HR 的普通问题。
- `send_material`：发送简历、作品集、项目材料或附件。
- `wait`：当前动作已完成，暂时等待；单向冷联系也使用 wait，不主动制造催促任务。
- `follow_up`：已有真实双向沟通且等待时间明显过长，适合礼貌跟进。
- `confirm_interview`：确认面试意向、时间或会议安排。
- `prepare_interview`：面试已确认，下一步是准备。
- `verify`：消息方向、岗位信息或风险证据不足，需要人工确认。
- `close`：明确结束机会。

## 排除规则

以下情况设置 `include=false`，且 `stage=null`、`action=null`：

- BOSS 求职助手、平台机器人或营销会话。
- 只有平台卡片、系统提示、自动追问，没有有效人类对话。
- 没有任何可解释招聘进度的证据。

排除样本仍需填写 `confidence`、`reason`；`evidence` 可以为空，也可以引用支持排除的消息。

## 证据与置信度

- `evidence` 只能原样复制输入中的 `boss_message:*` 或 `boss_job_snapshot:*`。
- 阶段和行动都必须由这些证据直接支持。
- `high`：存在明确措辞或清晰的最后行动方向。
- `medium`：结论合理，但附件语义、消息方向或时间策略仍有不确定性。
- `low`：只能给出保守判断，应优先选择 `verify`，不得编造确定结论。
- 平台自动追问、职位竞争力卡等不得当作 HR 真人回复。
