"""Project validated Agent decisions into Apply's persisted read model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from capybot.apply.store import ApplyStore

from .commit_gate import CommitResult


class DecisionCommitter:
    """Translate an approved semantic decision and persist it atomically."""

    def __init__(self, store: ApplyStore) -> None:
        self.store = store

    def commit(
        self,
        opportunity_id: str,
        result: CommitResult,
        *,
        conversation_id: str | None,
    ) -> dict[str, Any]:
        if not result.accepted or result.decision is None:
            raise ValueError("只有 CommitGate 通过的决策可以写入")
        projection = self.project(result)
        self.store.save_opportunity_analysis(
            opportunity_id,
            projection,
            conversation_id=conversation_id,
        )
        return projection

    @classmethod
    def project(cls, result: CommitResult) -> dict[str, Any]:
        decision = result.decision or {}
        next_step = decision.get("next") or {}
        suggestions = decision.get("suggestions") or []
        tasks = [
            {
                "title": item.get("content"),
                "due_at": cls._due_at(next_step.get("when")),
                "priority": cls._priority(next_step.get("when")),
                "reason": next_step.get("reason"),
                "evidence_message_ids": item.get("evidence") or [],
            }
            for item in suggestions
            if item.get("kind") == "task"
        ]
        drafts = [item for item in suggestions if item.get("kind") == "draft"]
        risks = [item for item in suggestions if item.get("kind") == "risk"]
        next_action = (
            "等待新消息或补充证据"
            if decision.get("status") == "insufficient_evidence"
            else (
                tasks[0]["title"]
                if tasks
                else cls._action_label(next_step.get("action"))
            )
        )
        return {
            "pipeline_stage": decision.get("stage"),
            "pursuit_recommendation": (
                "hold"
                if decision.get("status") == "insufficient_evidence"
                else cls._pursuit(next_step.get("action"))
            ),
            "stage_reason": next_step.get("reason"),
            "confidence": decision.get("confidence"),
            "events": [
                {
                    "event_type": item.get("type"),
                    "title": item.get("detail"),
                    "detail": item.get("detail"),
                    "evidence_message_ids": item.get("evidence") or [],
                }
                for item in decision.get("changes") or []
            ],
            "tasks": tasks,
            "reply_draft": {
                "content": drafts[0].get("content") if drafts else "",
                "reason": next_step.get("reason") if drafts else "",
                "evidence_message_ids": (
                    drafts[0].get("evidence") if drafts else []
                ),
            },
            "risk_flags": [
                {
                    "type": "other",
                    "severity": item.get("severity"),
                    "reason": item.get("content"),
                    "evidence_message_ids": item.get("evidence") or [],
                }
                for item in risks
            ],
            "next_action": next_action,
            "open_questions": (
                [decision.get("summary") or "当前证据不足，需要补充信息。"]
                if decision.get("status") == "insufficient_evidence"
                else []
            ),
            "summary_update": {
                "opportunity_summary": decision.get("summary"),
                "contact_summary": decision.get("summary"),
            },
            "evidence_message_ids": cls._all_evidence(decision),
            "_commit": {
                "stage": result.status != "needs_review",
                "summary": True,
                "next_action": True,
                "events": True,
                "risk_flags": True,
            },
            "run_status": result.status,
        }

    @staticmethod
    def _all_evidence(decision: dict[str, Any]) -> list[str]:
        refs = set((decision.get("next") or {}).get("evidence") or [])
        for item in (decision.get("changes") or []) + (
            decision.get("suggestions") or []
        ):
            refs.update(item.get("evidence") or [])
        return sorted(refs)

    @staticmethod
    def _due_at(when: str | None) -> str | None:
        hours = {"now": 0, "today": 8, "after_24h": 24, "after_48h": 48}.get(
            str(when or "")
        )
        if hours is None:
            return None
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    @staticmethod
    def _priority(when: str | None) -> str:
        return "high" if when in {"now", "today", "before_interview"} else "medium"

    @staticmethod
    def _pursuit(action: str | None) -> str:
        if action == "close":
            return "close"
        if action == "wait":
            return "hold"
        return "pursue"

    @staticmethod
    def _action_label(action: str | None) -> str:
        return {
            "reply": "回复 HR 最新消息",
            "send_material": "发送 HR 请求的材料",
            "wait": "等待 HR 反馈",
            "follow_up": "在合适时间跟进",
            "confirm_interview": "确认面试安排",
            "prepare_interview": "准备面试",
            "verify": "核实缺失信息",
            "close": "结束该机会",
        }.get(str(action or ""), "查看最新证据")
