"""Shared constants and lightweight helpers for Capybot Apply."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

PIPELINE_STAGES = {
    "discovered",
    "communicating",
    "need_my_action",
    "waiting_feedback",
    "interviewing",
    "closed",
}

ReviewKind = Literal["task", "stage", "draft"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_followup_iso(hours: int = 48) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def parse_utc_datetime(value: Any) -> datetime | None:
    """Parse BOSS epoch/ISO timestamps into timezone-aware UTC datetimes."""

    if value is None or value == "":
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.replace(".", "", 1).isdigit():
            value = float(stripped)
        else:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    return None


def normalize_utc_iso(value: Any) -> str | None:
    parsed = parse_utc_datetime(value)
    return parsed.isoformat() if parsed else None


@dataclass(slots=True)
class ImportReport:
    scanned_conversations: int = 0
    successful_conversations: int = 0
    failed_conversations: int = 0
    new_messages: int = 0
    new_jobs: int = 0
    pending_analysis: int = 0
    changed_conversations: int = 0
    skipped_conversations: int = 0
    analyzed_opportunities: int = 0
    queued_opportunities: int = 0
    failures: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_conversations": self.scanned_conversations,
            "successful_conversations": self.successful_conversations,
            "failed_conversations": self.failed_conversations,
            "new_messages": self.new_messages,
            "new_jobs": self.new_jobs,
            "pending_analysis": self.pending_analysis,
            "changed_conversations": self.changed_conversations,
            "skipped_conversations": self.skipped_conversations,
            "analyzed_opportunities": self.analyzed_opportunities,
            "queued_opportunities": self.queued_opportunities,
            "failures": self.failures or [],
        }
