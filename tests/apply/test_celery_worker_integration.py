from __future__ import annotations

import uuid

from celery.contrib.testing.worker import start_worker

from capybot.apply.celery_app import celery_app
from capybot.apply.events import redis_client, redis_ready, worker_ready
from capybot.apply.jobs import ApplyJobStore
from capybot.apply.tasks import trigger_import_analysis


def test_real_redis_worker_executes_and_persists_job():
    ready, reason = redis_ready()
    assert ready, reason
    jobs = ApplyJobStore(account_id="test_account")
    job = jobs.create(
        "trigger_import_analysis",
        idempotency_key=f"celery-integration:{uuid.uuid4().hex}",
        payload={"import_run_id": "missing-import-run"},
    )
    queue = f"capybot_apply_test_{uuid.uuid4().hex}"

    with start_worker(
        celery_app,
        pool="solo",
        concurrency=1,
        perform_ping_check=False,
        queues=(queue,),
    ):
        assert worker_ready()[0] is True
        result = trigger_import_analysis.apply_async(
            (job["id"], "missing-import-run"),
            queue=queue,
        )
        assert result.get(timeout=20)["opportunity_agent"] == 0
        assert next(redis_client().scan_iter("apply:worker:*:heartbeat", count=10), None)
        assert jobs.get(job["id"])["status"] == "ok"

        duplicate = trigger_import_analysis.apply_async(
            (job["id"], "missing-import-run"),
            queue=queue,
        )
        assert duplicate.get(timeout=20)["status"] == "duplicate_ignored"
