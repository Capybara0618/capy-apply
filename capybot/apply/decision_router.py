"""One explicit boundary between deterministic filtering and semantic Agent work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

RouteMode = Literal["skip", "cold_projection", "agent"]


@dataclass(frozen=True)
class DecisionRoute:
    mode: RouteMode
    reason: str
    trigger_type: str
    reactivated: bool = False
    decision: dict[str, Any] | None = None

    def import_payload(self) -> dict[str, Any]:
        return {
            "analysis_mode": "skipped" if self.mode == "skip" else "opportunity_agent",
            "routing_reason": self.reason,
            "trigger_type": self.trigger_type,
            "reactivated": self.reactivated,
        }


class DecisionRouter:
    """Skip non-evidence and project only unambiguous one-sided outreach."""

    IGNORED_TYPES = {"system", "platform_card", "job_card", "auto_followup"}
    ROUTED_TRIGGER_TYPES = {"cold_start", "import_delta", "rebuild"}

    @classmethod
    def route_delta(
        cls,
        messages: list[dict[str, Any]],
        *,
        source_quality: str | None,
    ) -> DecisionRoute:
        relevant = cls._human_messages(messages)
        if not relevant:
            return DecisionRoute(
                mode="skip",
                reason="新增内容仅包含平台卡片、系统消息或自动追问。",
                trigger_type="non_human_delta",
            )
        hr_messages = [message for message in relevant if not bool(message.get("from_me"))]
        if hr_messages:
            return DecisionRoute(
                mode="agent",
                reason=f"发现 {len(hr_messages)} 条真实 HR 新消息，需要重新决策。",
                trigger_type="hr_message",
                reactivated=source_quality
                in {"cold_outreach_no_reply", "cold_outreach_vip_no_reply"},
            )
        return DecisionRoute(
            mode="agent",
            reason=f"发现 {len(relevant)} 条我方新消息，需要更新机会投影。",
            trigger_type="candidate_message",
        )

    @classmethod
    def route_context(
        cls,
        context: dict[str, Any],
        trigger: dict[str, Any] | None,
    ) -> DecisionRoute:
        trigger_type = str((trigger or {}).get("type") or "manual")
        if trigger_type not in cls.ROUTED_TRIGGER_TYPES:
            return DecisionRoute(
                mode="agent",
                reason="用户目标需要语义决策或证据研究。",
                trigger_type=trigger_type,
            )

        messages = cls._human_messages(list(context.get("messages") or []))
        if not messages or any(not bool(message.get("from_me")) for message in messages):
            return DecisionRoute(
                mode="agent",
                reason="存在真实 HR 互动或缺少可安全投影的候选人消息。",
                trigger_type=trigger_type,
            )

        decision = cls._cold_projection(context, messages)
        return DecisionRoute(
            mode="cold_projection",
            reason=(
                "平台明确要求修正附件后重发。"
                if (decision.get("next") or {}).get("action") == "send_material"
                else "当前只有候选人单向联系，尚未收到真实 HR 回复。"
            ),
            trigger_type=trigger_type,
            decision=decision,
        )

    @classmethod
    def _human_messages(
        cls,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            message
            for message in messages
            if bool(message.get("is_human_message", 1))
            and str(message.get("message_type") or "text") not in cls.IGNORED_TYPES
        ]

    @staticmethod
    def _cold_projection(
        context: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        latest = messages[-1]
        evidence_ref = f"boss_message:{latest['message_id']}"
        platform_rejections = [
            message
            for message in context.get("messages") or []
            if str(message.get("message_type") or "") == "system"
            and any(
                marker in str(message.get("text") or "")
                for marker in ("打码后再发", "图片中可能存在联系方式", "附件发送失败")
            )
        ]
        sent_attachment = any(
            str(message.get("message_type") or "") in {"image", "file"}
            for message in messages
        )
        if sent_attachment and platform_rejections:
            rejection = platform_rejections[-1]
            rejection_ref = f"boss_message:{rejection['message_id']}"
            evidence = [evidence_ref, rejection_ref]
            return {
                "status": "ready",
                "stage": "discovered",
                "summary": "候选人发送的材料被平台拦截，需要修正后重新发送。",
                "evidence": evidence,
                "next": {
                    "action": "send_material",
                    "owner": "me",
                    "when": "now",
                    "reason": "平台提示附件可能含联系方式，应打码后重新发送。",
                    "evidence": evidence,
                },
                "changes": [],
                "suggestions": [
                    {
                        "kind": "task",
                        "content": "检查简历中的联系方式，打码后重新发送。",
                        "evidence": evidence,
                    },
                    {
                        "kind": "draft",
                        "content": "您好，刚才的简历图片被平台拦截，我已处理联系方式后重新发送。",
                        "evidence": evidence,
                    },
                ],
                "confidence": 0.95,
            }

        summary = (
            "候选人已主动联系并发送材料，尚未收到真实 HR 回复。"
            if sent_attachment
            else "候选人已主动联系，尚未收到真实 HR 回复。"
        )
        return {
            "status": "ready",
            "stage": "discovered",
            "summary": summary,
            "evidence": [evidence_ref],
            "next": {
                "action": "wait",
                "owner": "none",
                "when": "none",
                "reason": "当前只有候选人单向消息，不应制造待办或重复催促。",
                "evidence": [evidence_ref],
            },
            "changes": [],
            "suggestions": [],
            "confidence": 0.99,
        }
