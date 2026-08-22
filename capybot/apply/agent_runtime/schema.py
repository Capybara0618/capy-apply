"""Minimal semantic contracts for opportunity decisions."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PipelineStage = Literal[
    "discovered",
    "communicating",
    "need_my_action",
    "waiting_feedback",
    "interviewing",
    "closed",
]
DecisionStatus = Literal["ready", "needs_review", "insufficient_evidence"]
ActionType = Literal[
    "reply",
    "send_material",
    "wait",
    "follow_up",
    "confirm_interview",
    "prepare_interview",
    "verify",
    "close",
]
ActionOwner = Literal["me", "hr", "both", "none"]
ActionTiming = Literal[
    "now",
    "today",
    "after_24h",
    "after_48h",
    "before_interview",
    "none",
]
ChangeType = Literal[
    "hr_question",
    "material_requested",
    "material_sent",
    "interview_invited",
    "interview_confirmed",
    "feedback_received",
    "job_requirement_changed",
    "rejected",
    "risk_signal",
    "stage_changed",
]
SuggestionKind = Literal["task", "draft", "risk"]

_ALLOWED_EVIDENCE_PREFIXES = (
    "boss_message:",
    "boss_job_snapshot:",
    "web_source:",
    "candidate_profile:",
)
EvidenceRef = Annotated[
    str,
    Field(
        min_length=3,
        pattern=r"^(boss_message|boss_job_snapshot|web_source|candidate_profile):.+$",
        description="必须原样复制输入或工具 Observation 中的 canonical evidence ref",
    ),
]


def is_canonical_evidence_ref(value: str) -> bool:
    return bool(value) and value.startswith(_ALLOWED_EVIDENCE_PREFIXES)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NextDecision(StrictModel):
    action: ActionType
    owner: ActionOwner
    when: ActionTiming
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceRef] = Field(min_length=1)


class OpportunityChange(StrictModel):
    type: ChangeType
    detail: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceRef] = Field(min_length=1)


class Suggestion(StrictModel):
    kind: SuggestionKind
    content: str = Field(min_length=1, max_length=1000)
    evidence: list[EvidenceRef] = Field(min_length=1)
    severity: Literal["low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def risk_requires_severity(self) -> "Suggestion":
        if self.kind == "risk" and self.severity is None:
            raise ValueError("risk suggestion requires severity")
        if self.kind != "risk" and self.severity is not None:
            raise ValueError("only risk suggestions may define severity")
        return self


class OpportunityDecision(StrictModel):
    status: DecisionStatus
    stage: PipelineStage
    summary: str = Field(min_length=1, max_length=1000)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    next: NextDecision | None
    changes: list[OpportunityChange]
    suggestions: list[Suggestion]
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence")
    @classmethod
    def decision_evidence_is_canonical(
        cls,
        values: list[EvidenceRef],
    ) -> list[EvidenceRef]:
        _validate_refs(values)
        return values

    @field_validator("next", mode="before")
    @classmethod
    def empty_next_is_null(cls, value: object) -> object:
        return None if value == {} else value

    @field_validator("next")
    @classmethod
    def next_evidence_is_canonical(
        cls,
        value: NextDecision | None,
    ) -> NextDecision | None:
        if value is not None:
            _validate_refs(value.evidence)
        return value

    @field_validator("changes")
    @classmethod
    def change_evidence_is_canonical(
        cls,
        values: list[OpportunityChange],
    ) -> list[OpportunityChange]:
        for value in values:
            _validate_refs(value.evidence)
        return values

    @field_validator("suggestions")
    @classmethod
    def suggestion_evidence_is_canonical(
        cls,
        values: list[Suggestion],
    ) -> list[Suggestion]:
        for value in values:
            _validate_refs(value.evidence)
        return values


def _validate_refs(refs: list[str]) -> None:
    for ref in refs:
        if not is_canonical_evidence_ref(ref):
            raise ValueError(f"unsupported evidence reference: {ref}")


def decision_json_schema() -> dict:
    return OpportunityDecision.model_json_schema()
