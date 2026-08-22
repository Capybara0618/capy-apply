"""Conservative normalization for model decisions before validation."""

from __future__ import annotations

from typing import Any

from .schema import (
    NextDecision,
    OpportunityDecision,
    Suggestion,
    is_canonical_evidence_ref,
)


class DecisionNormalizer:
    """Repair narrow protocol mistakes without inventing business conclusions."""

    def normalize_input(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        """Reuse one explicit top-level ref when next omitted only that ref."""

        next_step = payload.get("next")
        top_refs = payload.get("evidence")
        if (
            isinstance(next_step, dict)
            and not next_step.get("evidence")
            and isinstance(top_refs, list)
            and len(top_refs) == 1
            and is_canonical_evidence_ref(str(top_refs[0]))
        ):
            normalized = dict(payload)
            normalized["next"] = {**next_step, "evidence": [str(top_refs[0])]}
            return normalized, ["下一步复用了顶层唯一合法证据引用"]
        return payload, []

    def normalize(
        self,
        decision: OpportunityDecision,
        *,
        interview_signal_refs: set[str],
        critical_risk_refs: set[str],
        preserve_discovered_stage: bool,
        material_completed_after_request: bool,
    ) -> tuple[OpportunityDecision, list[str]]:
        warnings: list[str] = []
        decision, current = self._safety_next(decision)
        warnings.extend(current)
        decision, current = self._critical_risk(decision, critical_risk_refs)
        warnings.extend(current)
        decision, current = self._stage_protocol(
            decision,
            interview_signal_refs=interview_signal_refs,
            preserve_discovered_stage=preserve_discovered_stage,
            material_completed_after_request=material_completed_after_request,
        )
        warnings.extend(current)
        decision, current = self._evidence_scope(decision)
        warnings.extend(current)
        return decision, warnings

    @staticmethod
    def _critical_risk(
        decision: OpportunityDecision,
        refs: set[str],
    ) -> tuple[OpportunityDecision, list[str]]:
        """Apply a deterministic fail-safe for explicit payment requests."""

        if not refs:
            return decision, []
        evidence = sorted(refs)
        next_step = decision.next
        changed = False
        if next_step is None or next_step.action != "verify":
            next_step = NextDecision(
                action="verify",
                owner="me",
                when="now",
                reason="聊天中出现明确收费或培训贷信息，需先核实且不要付款。",
                evidence=evidence,
            )
            changed = True

        suggestions = []
        supported_risk = False
        for suggestion in decision.suggestions:
            if suggestion.kind == "risk" and bool(set(suggestion.evidence) & refs):
                changed = changed or suggestion.severity != "high"
                suggestion = suggestion.model_copy(update={"severity": "high"})
                supported_risk = True
            suggestions.append(suggestion)
        if not supported_risk:
            suggestions.append(
                Suggestion(
                    kind="risk",
                    severity="high",
                    content="存在明确收费或培训贷信号，在核实前不要付款或承诺参加。",
                    evidence=evidence,
                )
            )
            changed = True
        if not changed:
            return decision, []
        return (
            decision.model_copy(
                update={
                    "next": next_step,
                    "suggestions": suggestions,
                    "status": "needs_review",
                }
            ),
            ["明确收费风险已按安全策略规范化为 verify 和高风险提示"],
        )

    @staticmethod
    def _safety_next(
        decision: OpportunityDecision,
    ) -> tuple[OpportunityDecision, list[str]]:
        if decision.next is not None:
            return decision, []
        high_risks = [
            item
            for item in decision.suggestions
            if item.kind == "risk" and item.severity == "high"
        ]
        if not high_risks:
            return decision, []
        risk = high_risks[0]
        return (
            decision.model_copy(
                update={
                    "next": NextDecision(
                        action="verify",
                        owner="me",
                        when="now",
                        reason=risk.content,
                        evidence=risk.evidence,
                    ),
                    "status": "needs_review",
                }
            ),
            ["高风险结论缺少下一步，已按安全策略规范化为 verify"],
        )

    @staticmethod
    def _stage_protocol(
        decision: OpportunityDecision,
        *,
        interview_signal_refs: set[str],
        preserve_discovered_stage: bool,
        material_completed_after_request: bool,
    ) -> tuple[OpportunityDecision, list[str]]:
        """Keep process progress while deriving non-interview action states."""

        if decision.next is None:
            return decision, []
        if decision.stage == "closed" and decision.next.action != "close":
            return (
                decision.model_copy(
                    update={
                        "next": decision.next.model_copy(
                            update={
                                "action": "close",
                                "owner": "none",
                                "when": "none",
                            }
                        ),
                        "status": "needs_review",
                    }
                ),
                ["closed 阶段已按安全协议规范化为 close 行动"],
            )

        required_stage = {
            "confirm_interview": "interviewing",
            "prepare_interview": "interviewing",
            "verify": "need_my_action",
            "close": "closed",
        }.get(decision.next.action)
        if interview_signal_refs and decision.stage != "closed":
            required_stage = "interviewing"
        elif material_completed_after_request and decision.next.action == "wait":
            required_stage = "waiting_feedback"
            if decision.next.owner != "hr":
                decision = decision.model_copy(
                    update={
                        "next": decision.next.model_copy(
                            update={"owner": "hr", "when": "none"}
                        ),
                        "status": "needs_review",
                    }
                )
        elif (
            decision.next.action in {"reply", "send_material"}
            and decision.stage != "interviewing"
            and not (
                preserve_discovered_stage and decision.stage == "discovered"
            )
        ):
            required_stage = "need_my_action"
        elif (
            decision.next.action == "wait"
            and decision.next.owner == "hr"
            and decision.stage != "interviewing"
        ):
            required_stage = "waiting_feedback"

        if not required_stage or decision.stage == required_stage:
            return decision, []
        return (
            decision.model_copy(
                update={"stage": required_stage, "status": "needs_review"}
            ),
            [
                f"模型阶段 {decision.stage} 已按行动 {decision.next.action} "
                f"规范化为 {required_stage}"
            ],
        )

    @staticmethod
    def _evidence_scope(
        decision: OpportunityDecision,
    ) -> tuple[OpportunityDecision, list[str]]:
        """Remove decorative web citations when local evidence supports a claim."""

        warnings: list[str] = []
        fallback_local_refs = [
            ref
            for ref in ((decision.next.evidence if decision.next else []) or decision.evidence)
            if not ref.startswith("web_source:")
        ]

        def local_or_original(refs: list[str], label: str) -> list[str]:
            local = [ref for ref in refs if not ref.startswith("web_source:")]
            if local and len(local) != len(refs):
                warnings.append(f"{label} 已移除不适用的公开网页引用")
                return local
            if refs and not local and fallback_local_refs:
                warnings.append(f"{label} 已改用触发当前行动的本地证据")
                return fallback_local_refs
            return refs

        next_step = decision.next
        if next_step is not None and next_step.action != "verify":
            next_step = next_step.model_copy(
                update={
                    "evidence": local_or_original(next_step.evidence, "下一步行动")
                }
            )

        changes = []
        for change in decision.changes:
            evidence = change.evidence
            if change.type not in {"job_requirement_changed", "risk_signal"}:
                evidence = local_or_original(evidence, f"变化 {change.type}")
            changes.append(change.model_copy(update={"evidence": evidence}))

        suggestions = []
        for suggestion in decision.suggestions:
            evidence = suggestion.evidence
            if suggestion.kind != "risk":
                evidence = local_or_original(evidence, f"建议 {suggestion.kind}")
            suggestions.append(suggestion.model_copy(update={"evidence": evidence}))

        return (
            decision.model_copy(
                update={
                    "next": next_step,
                    "changes": changes,
                    "suggestions": suggestions,
                }
            ),
            warnings,
        )
