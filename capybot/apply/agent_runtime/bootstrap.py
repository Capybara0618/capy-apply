"""Build the minimal first-turn context for one opportunity."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from capybot.apply.conversation_signals import ConversationSignals
from capybot.apply.store import ApplyStore

from .skills import ApplySkillLibrary


@dataclass(frozen=True)
class BootstrapContext:
    prompt: dict[str, Any]
    evidence_refs: set[str]
    metadata: dict[str, Any]


class OpportunityBootstrapBuilder:
    """Expose only the current projection and the triggering message delta."""

    IGNORED_TYPES = {"platform_card", "system", "auto_followup"}

    def __init__(self, store: ApplyStore | None = None, *, delta_limit: int = 12) -> None:
        self.store = store or ApplyStore()
        self.delta_limit = delta_limit

    def build(
        self,
        opportunity_id: str,
        *,
        trigger: dict[str, Any] | None = None,
    ) -> BootstrapContext:
        raw = self.store.opportunity_context(opportunity_id)
        opportunity = raw.get("opportunity")
        if not opportunity:
            raise ValueError(f"机会不存在: {opportunity_id}")

        human_messages = [
            message
            for message in raw.get("messages") or []
            if bool(message.get("is_human_message", 1))
            and str(message.get("message_type") or "text") not in self.IGNORED_TYPES
        ]
        trigger = trigger or {"type": "manual"}
        requested_ids = {str(value) for value in trigger.get("new_message_ids") or [] if value}
        delta = (
            [
                message
                for message in human_messages
                if str(message.get("message_id") or "") in requested_ids
            ]
            if requested_ids
            else human_messages
        )
        visible = delta[-self.delta_limit :]
        messages = [self._message(message) for message in visible]
        refs = {message["ref"] for message in messages}
        human_refs = {
            f"boss_message:{message['message_id']}"
            for message in human_messages
            if message.get("message_id")
        }
        hidden_history_count = len(human_refs - refs)
        event_count = len(raw.get("events") or [])
        has_job_evidence = bool(raw.get("jobs") or raw.get("job_snapshots"))
        has_job_lookup = self._has_job_lookup(raw)
        has_job_detail = self._has_job_detail(raw)
        trigger_type = str(trigger.get("type") or "manual")
        latest_message = visible[-1] if visible else None
        real_hr_messages = [
            message for message in human_messages if not bool(message.get("from_me"))
        ]
        candidate_attachment_sent = any(
            bool(message.get("from_me"))
            and str(message.get("message_type") or "") in {"image", "file"}
            for message in human_messages
        )
        material_request_index = next(
            (
                index
                for index in range(len(human_messages) - 1, -1, -1)
                if not bool(human_messages[index].get("from_me"))
                and ConversationSignals.is_material_request(
                    str(human_messages[index].get("text") or ""),
                )
            ),
            -1,
        )
        completed_material_messages = (
            [
                message
                for message in human_messages[material_request_index + 1 :]
                if bool(message.get("from_me"))
                and (
                    str(message.get("message_type") or "") in {"image", "file"}
                    or re.search(
                        r"已经发|已发|发您了|附后",
                        str(message.get("text") or ""),
                    )
                )
            ]
            if material_request_index >= 0
            else []
        )
        material_completed_after_request = bool(completed_material_messages)
        pending_material_request_refs = (
            [
                f"boss_message:{message['message_id']}"
                for message in visible
                if message.get("message_id")
                and not bool(message.get("from_me"))
                and ConversationSignals.is_material_request(str(message.get("text") or ""))
            ]
            if not material_completed_after_request
            else []
        )
        has_rejection_signal = any(
            re.search(
                r"不合适|不匹配|不完全吻合|不完全一致|暂不考虑|岗位已关闭|"
                r"已经招满|流程结束",
                str(message.get("text") or ""),
            )
            for message in real_hr_messages
        )
        interview_signal_refs = [
            f"boss_message:{message['message_id']}"
            for message in real_hr_messages
            if message.get("message_id")
            and ConversationSignals.is_interview_invitation(str(message.get("text") or ""))
        ]
        pending_hr_question_refs = (
            [f"boss_message:{latest_message['message_id']}"]
            if latest_message
            and not bool(latest_message.get("from_me"))
            and ConversationSignals.requires_reply(str(latest_message.get("text") or ""))
            else []
        )
        critical_risk_refs = [
            f"boss_message:{message['message_id']}"
            for message in visible
            if message.get("message_id")
            and not bool(message.get("from_me"))
            and ConversationSignals.is_critical_safety_risk(str(message.get("text") or ""))
        ]
        hr_delta_text = " ".join(
            str(message.get("text") or "")
            for message in visible
            if not bool(message.get("from_me"))
        )
        information_gaps: list[str] = []
        suggested_external_tools: list[str] = []
        requires_job_context = any(
            ConversationSignals.is_interview_invitation(str(message.get("text") or ""))
            or ConversationSignals.asks_for_job_context(str(message.get("text") or ""))
            for message in visible
            if not bool(message.get("from_me"))
        )
        profile_requested = any(
            ConversationSignals.requests_candidate_profile(str(message.get("text") or ""))
            for message in visible
            if not bool(message.get("from_me"))
        )
        profile_read_enabled = bool(
            (profile_requested or interview_signal_refs) and raw.get("candidate_profile")
        )
        job_read_enabled = bool(
            has_job_evidence and (requires_job_context or trigger_type == "research")
        )
        if profile_requested:
            information_gaps.append("HR 要求介绍候选人或项目，需要读取候选人画像生成有依据的回复")
        if has_job_lookup and not has_job_detail and requires_job_context:
            information_gaps.append("缺少生成针对性回复或面试准备所需的完整岗位要求")
            suggested_external_tools.append("boss_fetch_job_detail")
        if opportunity.get("company") and re.search(
            r"代招|合作项目|外包|派遣|入职主体",
            hr_delta_text,
        ):
            information_gaps.append("公司主体或招聘关系不清楚，需要核验公开信息")
            suggested_external_tools.append("research_company")
        required_any_tools: list[str] = []
        if trigger_type == "research":
            research_focus = str(trigger.get("focus") or "")
            if research_focus == "job":
                external_tools = ["boss_fetch_job_detail"] if has_job_lookup else []
            elif research_focus == "company":
                external_tools = ["research_company"] if opportunity.get("company") else []
            else:
                external_tools = (
                    ["boss_fetch_job_detail"]
                    if has_job_lookup and not has_job_detail
                    else ["research_company"]
                    if opportunity.get("company")
                    else []
                )
            required_any_tools = list(external_tools)
        elif trigger_type == "boss_refresh":
            external_tools = ["boss_refresh_opportunity"]
            required_any_tools = list(external_tools)
        else:
            external_tools = list(dict.fromkeys(suggested_external_tools))
            if profile_read_enabled:
                required_any_tools.append("profile_read")
        if trigger.get("allow_external") is False:
            external_tools = []
            required_any_tools = []
        skill_names = ApplySkillLibrary.names("opportunity")
        skill_tools = [ApplySkillLibrary.tool_name(name) for name in skill_names]
        memory_layers = [
            layer
            for layer, available in (
                ("l1", hidden_history_count > 0),
                ("l2", event_count > 0),
            )
            if available
        ]
        all_refs = [
            f"boss_message:{message['message_id']}"
            for message in delta
            if message.get("message_id")
        ]
        prompt = {
            "goal": self._goal(
                trigger,
                has_job_detail=has_job_detail,
                has_job_lookup=has_job_lookup,
            ),
            "decision_scope": {
                "required_outputs": ["stage", "next_action", "evidence"],
                "tailor_reply_or_preparation": bool(information_gaps),
                "information_gaps": information_gaps,
                "suggested_external_tools": suggested_external_tools,
                "planner_must_decide": True,
            },
            "opportunity": {
                "id": opportunity_id,
                "title": opportunity.get("title") or "待补全岗位",
                "company": opportunity.get("company"),
                "stage": opportunity.get("stage") or "discovered",
                "summary": opportunity.get("summary") or "暂无机会摘要",
                "previous_next_action": opportunity.get("next_action"),
                "evidence_state": {
                    "hidden_history_count": hidden_history_count,
                    "event_count": event_count,
                    "has_job_evidence": has_job_evidence,
                    "has_job_detail": has_job_detail,
                },
            },
            "delta": {
                "count": len(delta),
                "messages": messages,
                "all_refs": all_refs,
                "truncated": len(delta) > len(visible),
                "pending_hr_question_refs": pending_hr_question_refs,
                "conversation_state": {
                    "has_real_hr_reply": bool(real_hr_messages),
                    "latest_speaker": (
                        "me"
                        if latest_message and bool(latest_message.get("from_me"))
                        else "hr"
                        if latest_message
                        else "none"
                    ),
                    "candidate_attachment_sent": candidate_attachment_sent,
                    "material_completed_after_request": (material_completed_after_request),
                    "pending_material_request_refs": pending_material_request_refs,
                    "has_rejection_signal": has_rejection_signal,
                    "interview_signal_refs": interview_signal_refs,
                    "critical_risk_refs": critical_risk_refs,
                },
            },
        }
        return BootstrapContext(
            prompt=prompt,
            evidence_refs=refs,
            metadata={
                "opportunity_id": opportunity_id,
                "delta_count": len(delta),
                "visible_delta_count": len(visible),
                "truncated": len(delta) > len(visible),
                "trigger_type": trigger_type,
                "memory_layers": memory_layers,
                "has_job_evidence": has_job_evidence,
                "job_read_enabled": job_read_enabled,
                "profile_read_enabled": profile_read_enabled,
                "skill_tools": skill_tools,
                "has_job_detail": has_job_detail,
                "external_tools": external_tools,
                "external_tools_allowed": trigger.get("allow_external") is not False,
                "required_any_tools": required_any_tools,
                "pending_hr_question_refs": pending_hr_question_refs,
                "interview_signal_refs": interview_signal_refs,
                "critical_risk_refs": critical_risk_refs,
                "material_completed_after_request": material_completed_after_request,
                "pending_material_request_refs": pending_material_request_refs,
                "information_gaps": information_gaps,
                "suggested_external_tools": suggested_external_tools,
            },
        )

    @staticmethod
    def _message(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "ref": f"boss_message:{value['message_id']}",
            "speaker": "me" if bool(value.get("from_me")) else "hr",
            "type": str(value.get("message_type") or "text"),
            "content": OpportunityBootstrapBuilder.redact(str(value.get("text") or "")),
            "sent_at": value.get("sent_at"),
        }

    @staticmethod
    def _goal(
        trigger: dict[str, Any],
        *,
        has_job_detail: bool = False,
        has_job_lookup: bool = False,
    ) -> str:
        trigger_type = str(trigger.get("type") or "")
        if trigger_type == "research":
            if str(trigger.get("focus") or "") == "company":
                return "查询当前机会的公司公开信息，再更新机会决策"
            if str(trigger.get("focus") or "") == "job":
                return "从 BOSS 补全当前机会的岗位详情，再更新机会决策"
            if has_job_lookup and not has_job_detail:
                return "从 BOSS 补全当前机会的岗位详情，再更新机会决策"
            return "查询当前机会的公司公开信息，再更新机会决策"
        if trigger_type == "boss_refresh":
            return "核对当前机会的 BOSS 证据是否仍然最新，再更新机会决策"
        if trigger_type in {"cold_start", "rebuild"}:
            if trigger_type == "cold_start":
                return "把当前证据视为首次导入的完整历史，从零判断机会阶段和下一步行动"
            return "根据已有证据重建该求职机会的当前阶段和下一步行动"
        return (
            "判断本次新增消息如何影响当前求职机会；如果 HR 的问题、面试准备或"
            "招聘主体风险需要当前上下文之外的事实，先用最小必要工具取得证据，"
            "再生成有针对性的下一步行动与回复建议"
        )

    @staticmethod
    def _has_job_lookup(context: dict[str, Any]) -> bool:
        opportunity = context.get("opportunity") or {}
        has_identity = bool(
            str(opportunity.get("platform_job_id") or "").strip()
            or (
                str(opportunity.get("title") or "").strip()
                and str(opportunity.get("title") or "").strip() != "待补全岗位"
                and str(opportunity.get("company") or "").strip()
            )
            or any(
                str(row.get("platform_job_id") or "").strip()
                or (str(row.get("title") or "").strip() and str(row.get("company") or "").strip())
                for row in [
                    *(context.get("jobs") or []),
                    *(context.get("job_snapshots") or []),
                ]
            )
        )
        return has_identity

    @staticmethod
    def _has_job_detail(context: dict[str, Any]) -> bool:
        payloads: list[dict[str, Any]] = []
        for row in [
            *(context.get("job_snapshots") or []),
            *(context.get("jobs") or []),
        ]:
            for key in ("payload", "raw_payload"):
                value = row.get(key)
                if isinstance(value, dict):
                    payloads.append(value)
                elif isinstance(value, str):
                    try:
                        parsed = json.loads(value)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        payloads.append(parsed)
        for payload in payloads:
            job_info = payload.get("jobInfo") if isinstance(payload.get("jobInfo"), dict) else {}
            raw_payload = (
                payload.get("raw_payload") if isinstance(payload.get("raw_payload"), dict) else {}
            )
            if any(
                str(value or "").strip()
                for value in (
                    payload.get("description"),
                    payload.get("requirements"),
                    payload.get("jobDetail"),
                    payload.get("postDescription"),
                    job_info.get("postDescription"),
                    raw_payload.get("description"),
                    raw_payload.get("jobDetail"),
                    raw_payload.get("postDescription"),
                )
            ):
                return True
        return False

    @staticmethod
    def redact(text: str) -> str:
        replacements = (
            (r"1[3-9]\d{9}", "[手机号]"),
            (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱]"),
            (r"https?://\S+", "[链接]"),
            (r"\b\d{17}[\dXx]\b", "[身份证]"),
        )
        output = text
        for pattern, replacement in replacements:
            output = re.sub(pattern, replacement, output)
        return output
