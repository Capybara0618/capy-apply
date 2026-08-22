"""Evidence-level job fit evaluation, isolated from progress decisions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from json_repair import repair_json
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from capybot.apply.agent_runs import AgentRunRepository
from capybot.apply.conversation_signals import ConversationSignals
from capybot.apply.store import ApplyStore

from .bootstrap import OpportunityBootstrapBuilder
from .model import ModelTurn, OpenAIPlannerModel
from .priority import OpportunityPriorityCalculator
from .schema import EvidenceRef, is_canonical_evidence_ref


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


FitDimensionName = Literal[
    "目标方向",
    "核心技能",
    "项目经历",
    "Agent/LLM 相关性",
    "地点与实习时间",
    "薪资与风险",
]
FIT_WEIGHTS: dict[FitDimensionName, float] = {
    "目标方向": 0.15,
    "核心技能": 0.25,
    "项目经历": 0.25,
    "Agent/LLM 相关性": 0.15,
    "地点与实习时间": 0.10,
    "薪资与风险": 0.10,
}
FitCapType = Literal[
    "excluded_industry",
    "non_internship",
    "city_mismatch",
    "internship_duration_mismatch",
    "salary_below_expectation",
    "training_fee",
]
HARD_CAP_LIMITS: dict[FitCapType, int] = {
    "excluded_industry": 30,
    "non_internship": 55,
    "city_mismatch": 60,
    "internship_duration_mismatch": 65,
    "salary_below_expectation": 75,
    "training_fee": 20,
}


class FitDimension(_StrictModel):
    name: FitDimensionName
    score: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def compares_candidate_and_job(self) -> "FitDimension":
        has_profile = any(ref.startswith("candidate_profile:") for ref in self.evidence)
        has_job = any(
            ref.startswith(("boss_job_snapshot:", "boss_message:", "web_source:"))
            for ref in self.evidence
        )
        if not has_profile or not has_job:
            raise ValueError("每个评分维度必须同时引用候选人证据和岗位证据")
        return self


class FitGap(_StrictModel):
    content: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceRef] = Field(min_length=1)


class FitCap(_StrictModel):
    type: FitCapType
    reason: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceRef] = Field(min_length=1)


class JobFitDecision(_StrictModel):
    status: Literal["ok", "needs_review"]
    summary: str = Field(min_length=1, max_length=800)
    dimensions: list[FitDimension] = Field(min_length=6, max_length=6)
    gaps: list[FitGap] = Field(default_factory=list)
    hard_caps: list[FitCap] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)

    @field_validator("dimensions", "gaps", "hard_caps")
    @classmethod
    def refs_are_canonical(cls, values: list[_StrictModel]) -> list[_StrictModel]:
        for value in values:
            for ref in value.evidence:
                if not is_canonical_evidence_ref(ref):
                    raise ValueError(f"unsupported evidence reference: {ref}")
        return values

    @model_validator(mode="after")
    def dimensions_are_complete(self) -> "JobFitDecision":
        names = [item.name for item in self.dimensions]
        if set(names) != set(FIT_WEIGHTS):
            raise ValueError("岗位契合度必须包含六个且不重复的固定维度")
        return self


class JobFitEvaluator:
    SYSTEM_POLICY = """
你是岗位契合度评估器，不负责判断招聘进度。
只使用输入中的简历画像、求职偏好和岗位事实评分，不得编造经历或岗位要求。
status 表示“本次评估是否有足够证据并可稳定复现”，不表示岗位是否匹配。
明确的不匹配、低分或硬性封顶仍然是有效评估，应返回 ok；只有证据冲突、
关键岗位字段严重缺失或 confidence 低于 0.65 时才返回 needs_review。
每个维度、缺口和硬性封顶都必须引用输入给出的证据。
每个评分维度都必须同时引用 candidate_profile 与
boss_job_snapshot/boss_message/web_source；
岗位要求不完整时应降低分数和置信度，不得只凭简历宣称匹配。
所有 evidence 必须逐字复制输入中的 ref，不得填写原文、理由或自行生成的 ID。
维度固定为：目标方向、核心技能、项目经历、Agent/LLM 相关性、地点与实习时间、薪资与风险。
排除行业、非实习、地点冲突、周期冲突、薪资冲突和收费风险需要给出可解释封顶。
模型只判断 hard cap 类型，不决定封顶分数；封顶值由代码策略计算。
候选人每周可用天数大于岗位最低要求不算周期冲突；薪资区间有交集不算薪资硬冲突。
最终输出符合 JSON Schema。
""".strip()

    def __init__(
        self,
        store: ApplyStore | None = None,
        *,
        model: OpenAIPlannerModel | None = None,
    ) -> None:
        self.store = store or ApplyStore()
        self.model = model or OpenAIPlannerModel()

    async def evaluate(self, opportunity_id: str) -> dict[str, Any]:
        context = self.store.opportunity_context(opportunity_id)
        profile = context.get("candidate_profile") or {}
        preferences = context.get("job_preferences") or {}
        resume = str(profile.get("resume_markdown") or "").strip()
        if not resume:
            priority = OpportunityPriorityCalculator.calculate(context, fit_score=None)
            return {
                "status": "no_profile",
                "job_fit_score": None,
                "opportunity_priority_score": priority["score"],
                "confidence": 1.0,
                "dimensions": [],
                "matched_evidence": [],
                "missing_requirements": [],
                "hard_filter_caps": [],
                "raw_model_output": {
                    "summary": "缺少简历画像，暂不计算岗位契合度。",
                    "priority_breakdown": priority["breakdown"],
                },
                "resume_version_hash": None,
                "job_context_hash": self._hash(self._jobs(context)),
            }

        resume_hash = self._hash({"resume": resume, "preferences": preferences})[:16]
        profile_ref = f"candidate_profile:{resume_hash}"
        jobs, job_refs = self._job_facts(context)
        if not jobs or not job_refs:
            priority = OpportunityPriorityCalculator.calculate(context, fit_score=None)
            return {
                "status": "needs_review",
                "job_fit_score": None,
                "opportunity_priority_score": priority["score"],
                "confidence": 0.0,
                "dimensions": [],
                "matched_evidence": [],
                "missing_requirements": [],
                "hard_filter_caps": [],
                "raw_model_output": {
                    "summary": "缺少可引用的岗位快照，无法严谨评分。",
                    "priority_breakdown": priority["breakdown"],
                },
                "resume_version_hash": resume_hash,
                "job_context_hash": self._hash(jobs),
            }

        prompt = {
            "profile": {
                "ref": profile_ref,
                "summary": profile.get("profile_summary"),
                "skills": profile.get("skill_tags") or [],
                "projects": profile.get("project_tags") or [],
                "resume_excerpt": OpportunityBootstrapBuilder.redact(resume[:6000]),
            },
            "preferences": {
                key: value
                for key, value in preferences.items()
                if key not in {"id", "account_id", "created_at", "updated_at"}
            },
            "jobs": jobs,
        }
        schema = JobFitDecision.model_json_schema()
        valid_refs = {profile_ref, *job_refs}
        messages = [
            {
                "role": "system",
                "content": (
                    self.SYSTEM_POLICY
                    + "\n\n最终 JSON Schema：\n"
                    + json.dumps(schema, ensure_ascii=False)
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, default=str)},
        ]
        turn = await self.model.complete_json(
            messages,
            schema=schema,
            schema_name="capybot_job_fit_decision",
        )
        repaired = False
        try:
            payload = json.loads(repair_json(turn.content or ""))
            payload = self._normalize_evidence_refs(payload, valid_refs)
            decision = JobFitDecision.model_validate(payload)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            repair = await self.model.complete_json(
                [
                    *messages,
                    {"role": "assistant", "content": turn.content or ""},
                    {
                        "role": "user",
                        "content": (
                            "上一个 JSON 不符合 Schema。只修正结构，不改变证据事实；"
                            "evidence 只能逐字复制输入中的 ref。错误：" + str(exc)
                        ),
                    },
                ],
                schema=schema,
                schema_name="capybot_job_fit_decision_repair",
            )
            try:
                payload = json.loads(repair_json(repair.content or ""))
                payload = self._normalize_evidence_refs(payload, valid_refs)
                decision = JobFitDecision.model_validate(payload)
            except (ValidationError, ValueError, json.JSONDecodeError) as repair_exc:
                raise ValueError(f"岗位契合度输出非法: {repair_exc}") from repair_exc
            repaired = True
            turn = ModelTurn(
                content=repair.content,
                tool_calls=repair.tool_calls,
                prompt_tokens=turn.prompt_tokens + repair.prompt_tokens,
                completion_tokens=turn.completion_tokens + repair.completion_tokens,
                response_mode=f"{turn.response_mode}+repair:{repair.response_mode}",
            )
        used_refs = {
            ref
            for item in [*decision.dimensions, *decision.gaps, *decision.hard_caps]
            for ref in item.evidence
        }
        missing = sorted(used_refs - valid_refs)
        if missing:
            raise ValueError("岗位契合度引用不存在：" + "、".join(missing))
        decision, policy_adjustments = self._normalize_policy_dimensions(
            decision,
            preferences=preferences,
            jobs=jobs,
        )
        weighted_score = round(
            sum(item.score * FIT_WEIGHTS[item.name] for item in decision.dimensions)
        )
        validated_caps, rejected_caps = self._validate_hard_caps(
            decision.hard_caps,
            preferences=preferences,
            jobs=jobs,
            evidence_refs=[profile_ref, *job_refs],
        )
        cap_limits = [HARD_CAP_LIMITS[cap.type] for cap in validated_caps]
        score = min([weighted_score, *cap_limits])
        priority = OpportunityPriorityCalculator.calculate(context, fit_score=score)
        signal_count = max(
            (
                sum(
                    bool(job.get(key))
                    for key in (
                        "title",
                        "company",
                        "salary",
                        "city",
                        "experience",
                        "requirements",
                    )
                )
                for job in jobs
            ),
            default=0,
        )
        has_requirements = any(bool(str(job.get("requirements") or "").strip()) for job in jobs)
        assessment_status = (
            "ok"
            if decision.confidence >= 0.65 and signal_count >= 4 and has_requirements
            else "needs_review"
        )
        return {
            "status": assessment_status,
            "job_fit_score": score,
            "opportunity_priority_score": priority["score"],
            "confidence": decision.confidence,
            "dimensions": [item.model_dump(mode="json") for item in decision.dimensions],
            "matched_evidence": [
                {
                    "statement": item.reason,
                    "evidence_refs": item.evidence,
                }
                for item in decision.dimensions
                if item.score >= 60
            ],
            "missing_requirements": [
                {
                    "statement": item.content,
                    "evidence_refs": item.evidence,
                }
                for item in decision.gaps
            ],
            "hard_filter_caps": [
                {
                    **item.model_dump(mode="json"),
                    "max_score": HARD_CAP_LIMITS[item.type],
                }
                for item in validated_caps
            ],
            "raw_model_output": {
                **decision.model_dump(mode="json"),
                "weighted_score": weighted_score,
                "policy_adjustments": policy_adjustments,
                "rejected_hard_caps": rejected_caps,
                "assessment_status_reason": (
                    "岗位事实字段与模型置信度满足可复现评分阈值。"
                    if assessment_status == "ok"
                    else (
                        "岗位缺少职责或任职要求，当前分数仅供待确认参考。"
                        if not has_requirements
                        else "岗位事实字段不足或模型置信度低于 0.65。"
                    )
                ),
                "priority_breakdown": priority["breakdown"],
            },
            "resume_version_hash": resume_hash,
            "job_context_hash": self._hash(jobs),
            "_metrics": {
                "prompt_tokens": turn.prompt_tokens,
                "completion_tokens": turn.completion_tokens,
                "response_mode": turn.response_mode,
                "llm_call_count": 2 if repaired else 1,
                "policy_hash": hashlib.sha256(self.SYSTEM_POLICY.encode("utf-8")).hexdigest(),
            },
        }

    @staticmethod
    def _job_facts(context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        facts: list[dict[str, Any]] = []
        refs: list[str] = []
        sources = context.get("job_snapshots") or context.get("jobs") or []
        for snapshot in sources[:3]:
            payload = snapshot.get("payload") or snapshot.get("raw_payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            ref = f"boss_job_snapshot:{snapshot['id']}"
            refs.append(ref)
            facts.append(
                {
                    "ref": ref,
                    "title": snapshot.get("title") or payload.get("title"),
                    "company": snapshot.get("company") or payload.get("company"),
                    "salary": snapshot.get("salary") or payload.get("salary"),
                    "city": snapshot.get("city") or payload.get("city"),
                    "experience": snapshot.get("experience") or payload.get("experience"),
                    "education": snapshot.get("education") or payload.get("education"),
                    "requirements": (
                        payload.get("requirements")
                        or payload.get("description")
                        or payload.get("content")
                        or payload.get("positionCategory")
                    ),
                }
            )
        requirement_markers = (
            "岗位",
            "工作",
            "负责",
            "职责",
            "要求",
            "需要",
            "看重",
            "主要",
            "技术",
            "方向",
            "驻场",
            "实习",
        )
        for message in reversed(context.get("messages") or []):
            text = str(message.get("text") or "").strip()
            if (
                not text
                or bool(message.get("from_me"))
                or not bool(message.get("is_human_message"))
                or (
                    bool(message.get("message_type"))
                    and str(message.get("message_type")) != "text"
                )
                or ConversationSignals.is_platform_assistant_conversation(
                    contact_name=str(message.get("sender_name") or ""),
                    preview=text,
                )
                or not any(marker in text for marker in requirement_markers)
            ):
                continue
            message_id = str(message.get("message_id") or "")
            if not message_id:
                continue
            ref = f"boss_message:{message_id}"
            refs.append(ref)
            facts.append(
                {
                    "ref": ref,
                    "requirements": text[:1200],
                    "source": "HR 聊天补充",
                }
            )
            if sum(ref.startswith("boss_message:") for ref in refs) >= 6:
                break
        return facts, refs

    @classmethod
    def _validate_hard_caps(
        cls,
        caps: list[FitCap],
        *,
        preferences: dict[str, Any],
        jobs: list[dict[str, Any]],
        evidence_refs: list[str] | None = None,
    ) -> tuple[list[FitCap], list[dict[str, str]]]:
        valid: list[FitCap] = []
        rejected: list[dict[str, str]] = []
        job_text = " ".join(
            str(job.get(key) or "")
            for job in jobs
            for key in ("city", "experience", "salary", "requirements")
        )
        for cap in caps:
            reason = ""
            if cap.type == "internship_duration_mismatch" and cls._availability_satisfies(
                str(preferences.get("internship_time") or ""),
                job_text,
            ):
                reason = "候选人可用天数或周期覆盖岗位最低要求。"
            elif cap.type == "salary_below_expectation" and cls._salary_overlaps(
                str(preferences.get("salary") or ""),
                job_text,
            ):
                reason = "候选人期望与岗位薪资区间存在交集。"
            elif cap.type == "city_mismatch" and cls._city_matches(
                str(preferences.get("cities") or ""),
                job_text,
            ):
                reason = "岗位地点命中候选人城市或远程偏好。"
            if reason:
                rejected.append({"type": cap.type, "reason": reason})
            else:
                valid.append(cap)
        if evidence_refs:
            valid.extend(
                cls._derive_hard_caps(
                    existing={cap.type for cap in valid},
                    preferences=preferences,
                    jobs=jobs,
                    evidence_refs=evidence_refs,
                )
            )
        return valid, rejected

    @classmethod
    def _derive_hard_caps(
        cls,
        *,
        existing: set[FitCapType],
        preferences: dict[str, Any],
        jobs: list[dict[str, Any]],
        evidence_refs: list[str],
    ) -> list[FitCap]:
        """Derive unambiguous hard filters even when the model omits them."""

        derived: list[FitCap] = []
        job_city = " ".join(str(job.get("city") or "") for job in jobs)
        job_schedule = " ".join(str(job.get("experience") or "") for job in jobs)
        job_salary = " ".join(str(job.get("salary") or "") for job in jobs)
        job_text = " ".join(
            str(job.get(key) or "")
            for job in jobs
            for key in ("title", "city", "experience", "salary", "requirements")
        )

        def add(cap_type: FitCapType, reason: str) -> None:
            if cap_type not in existing:
                existing.add(cap_type)
                derived.append(FitCap(type=cap_type, reason=reason, evidence=evidence_refs))

        candidate_cities = str(preferences.get("cities") or "")
        if candidate_cities and job_city and not cls._city_matches(candidate_cities, job_city):
            add("city_mismatch", "岗位城市与候选人的城市/远程偏好明确不符。")

        candidate_time = str(preferences.get("internship_time") or "")
        if cls._availability_is_comparable(
            candidate_time, job_schedule
        ) and not cls._availability_satisfies(candidate_time, job_schedule):
            add("internship_duration_mismatch", "候选人的可用频次或周期低于岗位最低要求。")

        candidate_salary = str(preferences.get("salary") or "")
        if cls._salary_is_comparable(candidate_salary, job_salary) and not cls._salary_overlaps(
            candidate_salary, job_salary
        ):
            add("salary_below_expectation", "岗位薪资区间与候选人期望不存在交集。")

        excluded_terms = [
            term.strip()
            for term in re.split(r"[,，、/]+", str(preferences.get("excluded") or ""))
            if len(term.strip()) >= 2
        ]
        if any(term in job_text for term in excluded_terms):
            add("excluded_industry", "岗位信息命中候选人明确排除项。")
        if any(
            marker in job_text for marker in ("培训贷", "收费培训", "付费培训", "入职费", "押金")
        ):
            add("training_fee", "岗位信息出现收费或培训贷高风险关键词。")
        if "全职" in job_text and "实习" not in job_text:
            add("non_internship", "岗位明确为全职且未说明接受实习。")
        return derived

    @classmethod
    def _normalize_policy_dimensions(
        cls,
        decision: JobFitDecision,
        *,
        preferences: dict[str, Any],
        jobs: list[dict[str, Any]],
    ) -> tuple[JobFitDecision, list[str]]:
        """Replace model assertions for mechanically decidable place/time conditions."""

        candidate_cities = str(preferences.get("cities") or "")
        candidate_time = str(preferences.get("internship_time") or "")
        job_city = " ".join(str(job.get("city") or "") for job in jobs)
        job_schedule = " ".join(str(job.get("experience") or "") for job in jobs)
        place_time_checks: list[tuple[str, bool]] = []
        if candidate_cities and job_city:
            place_time_checks.append(("地点", cls._city_matches(candidate_cities, job_city)))
        if cls._availability_is_comparable(candidate_time, job_schedule):
            place_time_checks.append(
                ("实习频次与周期", cls._availability_satisfies(candidate_time, job_schedule))
            )

        candidate_salary = str(preferences.get("salary") or "")
        job_salary = " ".join(str(job.get("salary") or "") for job in jobs)
        job_text = " ".join(
            str(job.get(key) or "") for job in jobs for key in ("title", "salary", "requirements")
        )
        salary_checks: list[tuple[str, bool]] = []
        if cls._salary_is_comparable(candidate_salary, job_salary):
            salary_checks.append(("薪资区间", cls._salary_overlaps(candidate_salary, job_salary)))
        has_fee_risk = any(
            marker in job_text for marker in ("培训贷", "收费培训", "付费培训", "入职费", "押金")
        )
        if has_fee_risk or salary_checks:
            salary_checks.append(("收费风险安全性", not has_fee_risk))

        if not place_time_checks and not salary_checks:
            return decision, []

        updates: dict[FitDimensionName, tuple[int, str]] = {}
        adjustments: list[str] = []
        for name, checks in (
            ("地点与实习时间", place_time_checks),
            ("薪资与风险", salary_checks),
        ):
            if not checks:
                continue
            score = round(sum(100 if matched else 0 for _, matched in checks) / len(checks))
            reason = (
                "；".join(f"{label}{'匹配' if matched else '不匹配'}" for label, matched in checks)
                + "。该维度由代码根据结构化偏好与岗位字段复核。"
            )
            updates[name] = (score, reason)
            adjustments.append(reason)
        dimensions = []
        for item in decision.dimensions:
            update = updates.get(item.name)
            dimensions.append(
                item.model_copy(update={"score": update[0], "reason": update[1]})
                if update
                else item
            )
        return (
            decision.model_copy(update={"dimensions": dimensions}),
            adjustments,
        )

    @staticmethod
    def _availability_satisfies(candidate: str, job: str) -> bool:
        checks: list[bool] = []
        candidate_days = JobFitEvaluator._range_before_unit(candidate, "天")
        job_days = JobFitEvaluator._range_before_unit(job, "天")
        if candidate_days and job_days:
            checks.append(candidate_days[1] >= job_days[0])
        candidate_months = JobFitEvaluator._range_before_unit(candidate, "个月")
        job_months = JobFitEvaluator._range_before_unit(job, "个月")
        if candidate_months and job_months:
            checks.append(candidate_months[1] >= job_months[0])
        return bool(checks) and all(checks)

    @staticmethod
    def _availability_is_comparable(candidate: str, job: str) -> bool:
        return any(
            JobFitEvaluator._range_before_unit(candidate, unit)
            and JobFitEvaluator._range_before_unit(job, unit)
            for unit in ("天", "个月")
        )

    @staticmethod
    def _salary_overlaps(candidate: str, job: str) -> bool:
        if not candidate or not job:
            return False
        day_units = ("元/天", "元／天")
        if not any(unit in candidate for unit in day_units) or not any(
            unit in job for unit in day_units
        ):
            return False
        candidate_range = JobFitEvaluator._first_numeric_range(candidate)
        job_range = JobFitEvaluator._first_numeric_range(job)
        if not candidate_range or not job_range:
            return False
        return max(candidate_range[0], job_range[0]) <= min(candidate_range[1], job_range[1])

    @staticmethod
    def _salary_is_comparable(candidate: str, job: str) -> bool:
        day_units = ("元/天", "元／天")
        return (
            any(unit in candidate for unit in day_units)
            and any(unit in job for unit in day_units)
            and JobFitEvaluator._first_numeric_range(candidate) is not None
            and JobFitEvaluator._first_numeric_range(job) is not None
        )

    @staticmethod
    def _city_matches(candidate: str, job: str) -> bool:
        if "远程" in candidate and "远程" in job:
            return True
        cities = [
            value.strip()
            for value in re.split(r"[,，、/\s]+", candidate)
            if len(value.strip()) >= 2
        ]
        return any(city in job for city in cities)

    @staticmethod
    def _range_before_unit(text: str, unit: str) -> tuple[int, int] | None:
        escaped = re.escape(unit)
        ranged = re.search(rf"(\d+)\s*[-~至到]\s*(\d+)\s*{escaped}", text)
        if ranged:
            low, high = int(ranged.group(1)), int(ranged.group(2))
            return min(low, high), max(low, high)
        single = re.search(rf"(\d+)\s*{escaped}", text)
        if single:
            value = int(single.group(1))
            return value, value
        return None

    @staticmethod
    def _first_numeric_range(text: str) -> tuple[int, int] | None:
        ranged = re.search(r"(\d+(?:\.\d+)?)\s*[-~至到]\s*(\d+(?:\.\d+)?)", text)
        if not ranged:
            return None
        low, high = float(ranged.group(1)), float(ranged.group(2))
        return round(min(low, high)), round(max(low, high))

    @staticmethod
    def _jobs(context: dict[str, Any]) -> list[dict[str, Any]]:
        jobs, _ = JobFitEvaluator._job_facts(context)
        return jobs

    @staticmethod
    def _normalize_evidence_refs(
        payload: Any,
        valid_refs: set[str],
    ) -> Any:
        """Recover a canonical ref only when one ledger ref is embedded unambiguously."""

        if isinstance(payload, list):
            return [JobFitEvaluator._normalize_evidence_refs(item, valid_refs) for item in payload]
        if not isinstance(payload, dict):
            return payload
        normalized: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "evidence" and isinstance(value, list):
                refs = []
                for raw in value:
                    text = str(raw)
                    matches = [ref for ref in valid_refs if ref in text]
                    refs.append(matches[0] if len(matches) == 1 else raw)
                normalized[key] = refs
            else:
                normalized[key] = JobFitEvaluator._normalize_evidence_refs(
                    value,
                    valid_refs,
                )
        return normalized

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()


class JobFitAnalysisService:
    ENGINE = "job_fit_evaluator_v2"

    def __init__(
        self,
        store: ApplyStore | None = None,
        *,
        evaluator: JobFitEvaluator | None = None,
    ) -> None:
        self.store = store or ApplyStore()
        self.evaluator = evaluator or JobFitEvaluator(self.store)
        self.runs = AgentRunRepository(self.store)

    async def analyze(self, opportunity_id: str) -> dict[str, Any]:
        run_id = self.runs.create(
            "opportunity",
            opportunity_id,
            opportunity_id=opportunity_id,
            model_provider=self.evaluator.model.provider_label,
            model_name=self.evaluator.model.model_label,
            input_summary="岗位、简历或求职偏好版本发生变化。",
            engine=self.ENGINE,
            planner_mode="structured_evaluator",
        )
        self.runs.add_trace(
            run_id,
            1,
            "memory_read",
            "读取岗位、简历和求职偏好",
            summary="只读取岗位契合度所需证据。",
        )
        try:
            result = await self.evaluator.evaluate(opportunity_id)
            metrics = result.pop("_metrics", {})
            if metrics.get("policy_hash"):
                self.runs.add_trace(
                    run_id,
                    2,
                    "evaluator_policy",
                    "应用岗位契合度评分策略",
                    summary="固定维度、证据约束和硬性封顶策略已参与模型输入。",
                    metadata={"policy_hash": metrics["policy_hash"]},
                )
            self.runs.add_trace(
                run_id,
                3,
                "self_check",
                "校验契合度证据与硬性封顶",
                summary=f"评估状态：{result.get('status')}。",
            )
            self.store.save_fit_analysis(opportunity_id, result)
            self.runs.add_trace(
                run_id,
                4,
                "commit_write",
                "写入岗位契合度和机会优先级",
                summary=f"岗位契合度：{result.get('job_fit_score')}。",
            )
            self.runs.finish(
                run_id,
                status="ok" if result.get("status") in {"ok", "no_profile"} else "needs_review",
                output_summary=str(
                    (result.get("raw_model_output") or {}).get("summary") or "岗位契合度已更新。"
                ),
                confidence=float(result.get("confidence") or 0),
                llm_call_count=int(metrics.get("llm_call_count") or 0),
                prompt_tokens=int(metrics.get("prompt_tokens") or 0),
                completion_tokens=int(metrics.get("completion_tokens") or 0),
            )
            return {"run_id": run_id, "result": result}
        except Exception as exc:
            self.runs.finish(
                run_id,
                status="failed",
                error=str(exc),
                output_summary="岗位契合度评估失败。",
            )
            raise
