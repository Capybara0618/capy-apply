import json
import time

import pytest

from capybot.apply.events import (
    APPLY_EVENT_CHANNEL,
    normalize_apply_event,
    publish_apply_event,
    redis_client,
    redis_ready,
)


def test_event_schema_strips_business_content_and_rejects_unknown_types() -> None:
    normalized = normalize_apply_event(
        {
            "event": "apply_opportunity_updated",
            "opportunity_id": "opp-1",
            "status": "ok",
            "changed_fields": ["stage", "next_action"],
            "raw_messages": [{"text": "must not leave the server"}],
            "full_prompt": "private prompt",
        }
    )

    assert normalized is not None
    assert normalized["opportunity_id"] == "opp-1"
    assert normalized["changed_fields"] == ["stage", "next_action"]
    assert "raw_messages" not in normalized
    assert "full_prompt" not in normalized
    assert normalize_apply_event({"event": "apply_private_dump"}) is None


def test_redis_pubsub_delivers_sanitized_apply_event() -> None:
    ready, reason = redis_ready()
    if not ready:
        pytest.skip(f"Redis unavailable: {reason}")
    pubsub = redis_client().pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(APPLY_EVENT_CHANNEL)
    try:
        assert publish_apply_event(
            "apply_job_updated",
            job_id="job-realtime-test",
            status="running",
            raw_messages=[{"text": "private"}],
        )
        deadline = time.monotonic() + 3
        message = None
        while time.monotonic() < deadline and message is None:
            message = pubsub.get_message(timeout=0.25)
        assert message is not None
        payload = json.loads(message["data"])
        assert payload["job_id"] == "job-realtime-test"
        assert payload["status"] == "running"
        assert "raw_messages" not in payload
    finally:
        pubsub.close()
