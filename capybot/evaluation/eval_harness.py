"""Offline contract and optional live-model evaluation for Opportunity Agent v2."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from capybot.apply.agent_runtime.bootstrap import BootstrapContext
from capybot.apply.agent_runtime.commit_gate import CommitGate
from capybot.apply.agent_runtime.model import OpenAIPlannerModel
from capybot.apply.agent_runtime.sdk_runtime import (
    OpenAIAgentsLoop,
    OpenAIAgentsPolicy,
)
from capybot.apply.decision_router import DecisionRouter

CASES = [
    ("hr_resume", "方便发一份简历吗？", False, "need_my_action", "send_material"),
    ("candidate_sent", "好的，简历和作品集已经发您了。", True, "waiting_feedback", "wait"),
    ("interview", "明天下午三点方便参加线上面试吗？", False, "interviewing", "confirm_interview"),
    ("rejected", "岗位已经招满了，感谢关注。", False, "closed", "close"),
    ("salary", "这个岗位日薪 300，至少实习 4 个月，可以接受吗？", False, "need_my_action", "reply"),
    ("fee_risk", "入职前需要先缴纳 9800 元培训费。", False, "communicating", "verify"),
]


@dataclass
class EvalBootstrapBuilder:
    case: tuple[str, str, bool, str, str]

    def build(
        self,
        opportunity_id: str,
        *,
        trigger: dict[str, Any] | None = None,
    ) -> BootstrapContext:
        case_id, text, from_me, _, _ = self.case
        ref = f"boss_message:{case_id}"
        return BootstrapContext(
            prompt={
                "goal": "判断新增消息如何影响当前求职机会",
                "opportunity": {
                    "id": opportunity_id,
                    "title": "Agent 开发实习生",
                    "company": "示例科技",
                    "stage": "communicating",
                    "summary": "正在沟通",
                    "next_action": None,
                },
                "delta": {
                    "count": 1,
                    "messages": [
                        {
                            "ref": ref,
                            "speaker": "me" if from_me else "hr",
                            "type": "text",
                            "content": text,
                            "sent_at": None,
                        }
                    ],
                    "all_refs": [ref],
                    "truncated": False,
                },
            },
            evidence_refs={ref},
            metadata={"delta_count": 1, "trigger_type": "eval"},
        )


class NoTools:
    async def __aenter__(self) -> "NoTools":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    @property
    def tools(self) -> list[Any]:
        return []

    def kind_for(self, _name: str) -> None:
        return None

    def server_for(self, _name: str) -> None:
        return None


def _gold_decision(case: tuple[str, str, bool, str, str]) -> dict[str, Any]:
    case_id, text, _, stage, action = case
    ref = f"boss_message:{case_id}"
    suggestions: list[dict[str, Any]] = []
    if stage == "need_my_action" or action == "confirm_interview":
        suggestions.append(
            {
                "kind": "task",
                "content": (
                    "确认面试时间"
                    if action == "confirm_interview"
                    else "处理招聘者最新问题"
                ),
                "evidence": [ref],
            }
        )
        suggestions.append(
            {
                "kind": "draft",
                "content": (
                    "您好，明天下午三点可以，我会按时参加线上面试。"
                    if action == "confirm_interview"
                    else "您好，收到，我确认后尽快回复您。"
                ),
                "evidence": [ref],
            }
        )
    if case_id == "fee_risk":
        suggestions.append(
            {
                "kind": "risk",
                "content": "对方要求缴纳培训费，需要停止付款并核实岗位真实性。",
                "severity": "high",
                "evidence": [ref],
            }
        )
    return {
        "status": "ready",
        "stage": stage,
        "summary": text,
        "next": {
            "action": action,
            "owner": "hr" if action == "wait" else ("none" if action == "close" else "me"),
            "when": "none" if action in {"wait", "close"} else "now",
            "reason": text,
            "evidence": [ref],
        },
        "changes": [
            {
                "type": "rejected" if stage == "closed" else "stage_changed",
                "detail": text,
                "evidence": [ref],
            }
        ],
        "suggestions": suggestions,
        "confidence": 0.85,
    }


def run_offline_eval() -> dict[str, Any]:
    gate = CommitGate()
    accepted = sum(
        gate.validate(
            _gold_decision(case),
            valid_evidence_refs={f"boss_message:{case[0]}"},
        ).accepted
        for case in CASES
    )
    fake_ref = _gold_decision(CASES[0])
    fake_ref["next"]["evidence"] = ["boss_message:invented"]
    auto_send = _gold_decision(CASES[0])
    auto_send["suggestions"][1]["content"] = "已替你发送简历。"
    adversarial = [
        not gate.validate(
            fake_ref,
            valid_evidence_refs={"boss_message:hr_resume"},
        ).accepted,
        not gate.validate(
            auto_send,
            valid_evidence_refs={"boss_message:hr_resume"},
        ).accepted,
    ]
    routes = [
        DecisionRouter.route_delta(
            [{"message_type": "platform_card", "is_human_message": 0}],
            source_quality=None,
        ).mode
        == "skip",
        DecisionRouter.route_delta(
            [{"message_type": "text", "is_human_message": 1, "from_me": False}],
            source_quality=None,
        ).mode
        == "agent",
    ]
    passed = accepted + sum(adversarial) + sum(routes)
    total = len(CASES) + len(adversarial) + len(routes)
    return {
        "mode": "offline_contract",
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 4),
        "scenario_contracts": {"passed": accepted, "total": len(CASES)},
        "adversarial_safety": {"passed": sum(adversarial), "total": len(adversarial)},
        "decision_router": {"passed": sum(routes), "total": len(routes)},
    }


async def run_live_eval(model: OpenAIPlannerModel | None = None) -> dict[str, Any]:
    planner = model or OpenAIPlannerModel()
    if not planner.available:
        raise RuntimeError("未配置 OpenAI-compatible 模型")
    rows = []
    for case in CASES:
        agent = OpenAIAgentsLoop(
            model=planner,
            bootstrap_builder=EvalBootstrapBuilder(case),
            toolbox_factory=lambda _opportunity_id: NoTools(),
            policy=OpenAIAgentsPolicy(max_turns=2, max_tool_calls=0),
        )
        result, metrics = await agent.run(case[0], trigger={"type": "eval"})
        decision = result.decision or {}
        rows.append(
            {
                "id": case[0],
                "accepted": result.accepted,
                "stage_ok": decision.get("stage") == case[3],
                "action_ok": (decision.get("next") or {}).get("action") == case[4],
                "actual_stage": decision.get("stage"),
                "actual_action": (decision.get("next") or {}).get("action"),
                "duration_ms": metrics.get("duration_ms"),
                "errors": result.errors,
            }
        )
    count = len(rows)
    return {
        "mode": "live_model",
        "model": planner.model_label,
        "cases": count,
        "accepted_rate": round(sum(bool(row["accepted"]) for row in rows) / count, 4),
        "stage_accuracy": round(sum(bool(row["stage_ok"]) for row in rows) / count, 4),
        "action_accuracy": round(sum(bool(row["action_ok"]) for row in rows) / count, 4),
        "rows": rows,
    }


def run_eval(*, live: bool = False) -> dict[str, Any]:
    return asyncio.run(run_live_eval()) if live else run_offline_eval()


def format_eval(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)
