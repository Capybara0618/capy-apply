"""Normalize and validate an Agent decision before local side effects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .decision_normalizer import DecisionNormalizer
from .decision_validator import DecisionValidator
from .schema import OpportunityDecision


@dataclass(frozen=True)
class CommitResult:
    accepted: bool
    status: str
    decision: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class CommitGate:
    """Keep model semantics separate from deterministic commit invariants."""

    AUTO_SEND_MARKERS = DecisionValidator.AUTO_SEND_MARKERS

    def __init__(
        self,
        *,
        normalizer: DecisionNormalizer | None = None,
        validator: DecisionValidator | None = None,
    ) -> None:
        self.normalizer = normalizer or DecisionNormalizer()
        self.validator = validator or DecisionValidator()

    def validate(
        self,
        payload: dict[str, Any],
        *,
        valid_evidence_refs: set[str],
        pending_hr_question_refs: set[str] | None = None,
        interview_signal_refs: set[str] | None = None,
        critical_risk_refs: set[str] | None = None,
        pending_material_request_refs: set[str] | None = None,
        preserve_discovered_stage: bool = False,
        material_completed_after_request: bool = False,
    ) -> CommitResult:
        payload, input_warnings = self.normalizer.normalize_input(payload)
        try:
            decision = OpportunityDecision.model_validate(payload)
        except ValidationError as exc:
            return CommitResult(False, "rejected", None, [str(exc)])

        decision, normalization_warnings = self.normalizer.normalize(
            decision,
            interview_signal_refs=interview_signal_refs or set(),
            critical_risk_refs=critical_risk_refs or set(),
            preserve_discovered_stage=preserve_discovered_stage,
            material_completed_after_request=material_completed_after_request,
        )
        errors, validation_warnings = self.validator.validate(
            decision,
            valid_evidence_refs=valid_evidence_refs,
            pending_hr_question_refs=pending_hr_question_refs or set(),
            critical_risk_refs=critical_risk_refs or set(),
            pending_material_request_refs=pending_material_request_refs or set(),
        )
        warnings = [
            *input_warnings,
            *normalization_warnings,
            *validation_warnings,
        ]
        accepted = not errors
        status = (
            "needs_review"
            if accepted and warnings
            else ("accepted" if accepted else "rejected")
        )
        return CommitResult(
            accepted=accepted,
            status=status,
            decision=decision.model_dump(mode="json") if accepted else None,
            errors=errors,
            warnings=warnings,
        )
