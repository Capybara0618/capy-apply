"""Transparent deterministic opportunity-priority calculation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from capybot.apply.models import parse_utc_datetime


class OpportunityPriorityCalculator:
    """Combine fit and progress signals without asking the LLM for a magic score."""

    @classmethod
    def calculate(
        cls,
        context: dict[str, Any],
        *,
        fit_score: int | None,
    ) -> dict[str, Any]:
        opportunity = context.get("opportunity") or {}
        messages = [
            message
            for message in context.get("messages") or []
            if bool(message.get("is_human_message", 1))
        ]
        fit = round((fit_score or 0) * 0.4) if fit_score is not None else 0
        hr_interaction = 25 if any(not bool(message.get("from_me")) for message in messages) else 0
        urgency = {
            "need_my_action": 20,
            "interviewing": 18,
            "communicating": 10,
            "waiting_feedback": 6,
            "discovered": 4,
            "closed": 0,
        }.get(str(opportunity.get("stage") or ""), 0)
        recency = cls._recency(messages)
        risk_penalty = cls._risk_penalty(opportunity.get("risk_flags"))
        score = max(0, min(100, fit + hr_interaction + urgency + recency - risk_penalty))
        return {
            "score": score,
            "breakdown": {
                "fit": fit,
                "hr_interaction": hr_interaction,
                "urgency": urgency,
                "recency": recency,
                "risk_penalty": -risk_penalty,
            },
        }

    @staticmethod
    def _recency(messages: list[dict[str, Any]]) -> int:
        values = [
            parse_utc_datetime(message.get("sent_at") or message.get("created_at"))
            for message in messages
        ]
        values = [value for value in values if value is not None]
        if not values:
            return 0
        latest = max(values)
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - latest).total_seconds() / 86400
        if days <= 2:
            return 15
        if days <= 7:
            return 10
        if days <= 30:
            return 5
        return 0

    @staticmethod
    def _risk_penalty(value: Any) -> int:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = []
        penalties = {"low": 4, "medium": 10, "high": 20}
        return min(
            40,
            sum(
                penalties.get(str(item.get("severity") or "low"), 4)
                for item in value or []
                if isinstance(item, dict)
            ),
        )
