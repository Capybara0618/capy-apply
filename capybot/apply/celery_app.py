"""Celery application for Capybot Apply."""

from __future__ import annotations

from celery import Celery
from celery.signals import heartbeat_sent, worker_ready

from .postgres import apply_redis_url

celery_app = Celery(
    "capybot_apply",
    broker=apply_redis_url(),
    backend=apply_redis_url(),
    include=["capybot.apply.tasks"],
)

celery_app.conf.update(
    task_default_queue="capybot_apply",
    task_default_priority=5,
    task_queue_max_priority=9,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_soft_time_limit=840,
    task_time_limit=900,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    result_expires=86400,
    broker_transport_options={
        "visibility_timeout": 1800,
        "queue_order_strategy": "priority",
        "priority_steps": list(range(10)),
    },
    timezone="Asia/Shanghai",
)

APPLY_TASK_PRIORITIES = {
    "import_boss_snapshot": 0,
    "trigger_import_analysis": 2,
    "analyze_opportunity": 4,
    "rebuild_derived_from_l1": 7,
}


def enqueue_apply_task(task, job_type: str, *args):
    """Enqueue interactive work ahead of batch scoring on the Redis broker."""

    return task.apply_async(
        args=args,
        priority=APPLY_TASK_PRIORITIES.get(job_type, 5),
    )


def _refresh_worker_heartbeat(**_kwargs) -> None:
    from .events import worker_heartbeat

    worker_heartbeat("default")


worker_ready.connect(_refresh_worker_heartbeat, weak=False)
heartbeat_sent.connect(_refresh_worker_heartbeat, weak=False)
