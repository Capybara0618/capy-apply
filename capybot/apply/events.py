"""Redis Pub/Sub events for Apply realtime UI."""

from __future__ import annotations

import json
import logging
from typing import Any

import redis

from .models import utc_now_iso
from .postgres import apply_redis_url

APPLY_EVENT_CHANNEL = "apply.events"
APPLY_EVENT_TYPES = {
    "apply_job_updated",
    "apply_import_updated",
    "apply_opportunity_updated",
    "apply_agent_run_updated",
    "apply_overview_invalidated",
    "apply_health_changed",
}
APPLY_EVENT_FIELDS = {
    "account_id",
    "agent_run_id",
    "changed_fields",
    "import_run_id",
    "job_id",
    "opportunity_id",
    "status",
    "target_id",
    "target_type",
}
logger = logging.getLogger(__name__)


def redis_client(url: str | None = None) -> redis.Redis:
    return redis.Redis.from_url(
        url or apply_redis_url(),
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )


def redis_ready(url: str | None = None) -> tuple[bool, str | None]:
    try:
        redis_client(url).ping()
        return True, None
    except Exception as exc:
        return False, str(exc)


def publish_apply_event(event: str, **payload: Any) -> bool:
    body = normalize_apply_event({"event": event, **payload})
    if body is None:
        logger.warning("Rejected unknown Apply realtime event: %s", event)
        return False
    try:
        redis_client().publish(APPLY_EVENT_CHANNEL, json.dumps(body, ensure_ascii=False))
        return True
    except Exception as exc:
        logger.warning("Apply realtime event publish failed: event=%s error=%s", event, exc)
        return False


def normalize_apply_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return a lightweight, allowlisted invalidation event or reject it."""

    event = str(payload.get("event") or "")
    if event not in APPLY_EVENT_TYPES:
        return None
    body = {
        "event": event,
        "updated_at": str(payload.get("updated_at") or utc_now_iso()),
    }
    for key in APPLY_EVENT_FIELDS:
        value = payload.get(key)
        if value is None:
            continue
        if key == "changed_fields":
            body[key] = [str(item)[:80] for item in value[:20]] if isinstance(value, list) else []
        else:
            body[key] = str(value)[:200]
    return body


def cache_job_progress(job_id: str, payload: dict[str, Any]) -> bool:
    try:
        client = redis_client()
        key = f"apply:job:{job_id}"
        client.setex(key, 60 * 60 * 24, json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as exc:
        logger.warning("Apply job progress cache failed: job_id=%s error=%s", job_id, exc)
        return False


def worker_heartbeat(worker_id: str = "default") -> None:
    redis_client().setex(f"apply:worker:{worker_id}:heartbeat", 15, utc_now_iso())


def worker_ready() -> tuple[bool, str | None]:
    try:
        if next(redis_client().scan_iter("apply:worker:*:heartbeat", count=10), None):
            return True, None
        try:
            from .celery_app import celery_app

            response = celery_app.control.inspect(timeout=0.5).ping()
            if response:
                return True, None
        except Exception:
            pass
        return False, "Celery Worker 未上报心跳，请运行 capybot apply worker。"
    except Exception as exc:
        return False, str(exc)
