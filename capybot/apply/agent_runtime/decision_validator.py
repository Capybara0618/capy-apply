"""Deterministic invariants for normalized opportunity decisions."""

from __future__ import annotations

from .schema import OpportunityDecision, is_canonical_evidence_ref


class DecisionValidator:
    """Reject unsupported or unsafe decisions without replacing their semantics."""

    AUTO_SEND_MARKERS = (
        "已自动发送",
        "我将自动发送",
        "已替你发送",
        "自动填入 BOSS",
    )
    TEMPLATE_PLACEHOLDER_MARKERS = (
        "[您的",
        "【您的",
        "<您的",
        "[项目名称]",
        "【项目名称】",
        "[公司名称]",
        "【公司名称】",
        "[职位名称]",
        "【职位名称】",
        "[请填写",
        "【请填写",
    )

    def validate(
        self,
        decision: OpportunityDecision,
        *,
        valid_evidence_refs: set[str],
        pending_hr_question_refs: set[str],
        critical_risk_refs: set[str],
        pending_material_request_refs: set[str],
    ) -> tuple[list[str], list[str]]:
        refs = self._collect_refs(decision)
        invalid = sorted(ref for ref in refs if not is_canonical_evidence_ref(ref))
        missing = sorted(ref for ref in refs if ref not in valid_evidence_refs)
        errors: list[str] = []
        warnings: list[str] = []
        if invalid:
            errors.append("证据引用命名空间非法：" + "、".join(invalid))
        if missing:
            errors.append("以下证据引用未在本次 Agent 观察中出现：" + "、".join(missing))

        drafts = [item for item in decision.suggestions if item.kind == "draft"]
        if len(drafts) > 1:
            errors.append("一次机会分析最多生成一条回复草稿")
        if any(
            marker in draft.content
            for marker in self.AUTO_SEND_MARKERS
            for draft in drafts
        ):
            errors.append("回复草稿包含自动发送语义")
        if any(
            marker in draft.content
            for marker in self.TEMPLATE_PLACEHOLDER_MARKERS
            for draft in drafts
        ):
            errors.append("回复草稿包含待填写模板占位符")

        fingerprints = {
            (item.kind, " ".join(item.content.lower().split()))
            for item in decision.suggestions
        }
        if len(fingerprints) != len(decision.suggestions):
            errors.append("建议列表包含重复内容")
        if decision.next and decision.next.action in {
            "reply",
            "send_material",
            "confirm_interview",
        }:
            kinds = {item.kind for item in decision.suggestions}
            if "task" not in kinds:
                errors.append("需要回复 HR 的决策必须生成待确认任务")
            if "draft" not in kinds:
                errors.append("需要回复 HR 的决策必须生成待确认草稿")
        if decision.status == "ready" and decision.confidence < 0.6:
            errors.append("低置信度结果不得标记为 ready")

        errors.extend(self._stage_action_errors(decision))
        errors.extend(self._evidence_scope_errors(decision))
        errors.extend(
            self._pending_hr_question_errors(decision, pending_hr_question_refs)
        )
        errors.extend(self._critical_risk_errors(decision, critical_risk_refs))
        errors.extend(
            self._pending_material_request_errors(
                decision,
                pending_material_request_refs,
            )
        )
        if decision.status != "ready" or decision.confidence < 0.75:
            warnings.append("结果需要用户确认")
        return errors, warnings

    @staticmethod
    def _pending_material_request_errors(
        decision: OpportunityDecision,
        refs: set[str],
    ) -> list[str]:
        if not refs:
            return []
        if decision.next is None or decision.next.action != "send_material":
            return ["HR 明确索要材料且尚未完成，下一步必须是 send_material"]
        if not (set(decision.next.evidence) & refs):
            return ["发送材料行动没有引用尚未完成的 HR 材料请求"]
        return []

    @staticmethod
    def _critical_risk_errors(
        decision: OpportunityDecision,
        refs: set[str],
    ) -> list[str]:
        if not refs:
            return []
        errors: list[str] = []
        if decision.next is None or decision.next.action != "verify":
            errors.append("明确收费或培训贷风险必须先执行 verify")
        supported_high_risk = any(
            suggestion.kind == "risk"
            and suggestion.severity == "high"
            and bool(set(suggestion.evidence) & refs)
            for suggestion in decision.suggestions
        )
        if not supported_high_risk:
            errors.append("明确收费或培训贷风险必须生成带触发证据的高风险提示")
        return errors

    @staticmethod
    def _stage_action_errors(decision: OpportunityDecision) -> list[str]:
        if decision.next is None:
            return (
                []
                if decision.status == "insufficient_evidence"
                else ["可执行决策缺少下一步行动"]
            )
        stage = decision.stage
        action = decision.next.action
        errors: list[str] = []
        required_stage = {
            "confirm_interview": "interviewing",
            "prepare_interview": "interviewing",
            "close": "closed",
        }.get(action)
        if required_stage and stage != required_stage:
            errors.append(f"行动 {action} 与阶段 {stage} 不一致，应为 {required_stage}")
        if stage == "closed" and action != "close":
            errors.append("closed 阶段的下一步必须是 close")
        has_high_risk = any(
            item.kind == "risk" and item.severity == "high"
            for item in decision.suggestions
        )
        if has_high_risk and action != "verify":
            errors.append("高风险信号必须先执行 verify")
        return errors

    @staticmethod
    def _evidence_scope_errors(decision: OpportunityDecision) -> list[str]:
        errors: list[str] = []
        if (
            decision.next is not None
            and any(ref.startswith("web_source:") for ref in decision.next.evidence)
            and decision.next.action != "verify"
        ):
            errors.append("公开网页证据只能直接支撑 verify 行动")
        for change in decision.changes:
            if (
                any(ref.startswith("web_source:") for ref in change.evidence)
                and change.type not in {"job_requirement_changed", "risk_signal"}
            ):
                errors.append(f"公开网页证据不能支撑 {change.type} 类型的聊天进展")
        for suggestion in decision.suggestions:
            if (
                any(ref.startswith("web_source:") for ref in suggestion.evidence)
                and suggestion.kind != "risk"
            ):
                errors.append("公开网页证据只能直接支撑风险建议")
        return errors

    @staticmethod
    def _collect_refs(decision: OpportunityDecision) -> set[str]:
        refs = set(decision.evidence)
        refs.update(decision.next.evidence if decision.next else [])
        for change in decision.changes:
            refs.update(change.evidence)
        for suggestion in decision.suggestions:
            refs.update(suggestion.evidence)
        return refs

    @staticmethod
    def _pending_hr_question_errors(
        decision: OpportunityDecision,
        refs: set[str],
    ) -> list[str]:
        if not refs:
            return []
        if decision.next is None:
            return ["最新 HR 问题尚未处理，必须给出候选人下一步行动"]
        if decision.next.action not in {
            "reply",
            "send_material",
            "confirm_interview",
            "verify",
        }:
            return ["最新 HR 问题尚未处理，不得判定为继续等待对方"]
        if not (set(decision.next.evidence) & refs):
            return ["下一步行动没有引用最新未处理 HR 问题的证据"]
        return []
