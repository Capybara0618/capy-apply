"""Apply job repository backed by PostgreSQL."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from sqlalchemy import desc, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from .events import cache_job_progress, publish_apply_event, redis_ready, worker_ready
from .models import utc_now_iso
from .postgres import apply_jobs, begin, database_ready, engine

TERMINAL_STATUSES = {"ok", "failed", "cancelled"}


class ApplyJobStore:
    def __init__(self, account_id: str | None = None) -> None:
        self.account_id = str(account_id) if account_id else None

    def create(
        self,
        job_type: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
        message: str = "任务已创建，等待 Worker 执行。",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        account_id = self._current_account_id()
        row = {
            "id": f"job_{uuid.uuid4().hex}",
            "account_id": account_id,
            "job_type": job_type,
            "status": "queued",
            "progress_current": 0,
            "progress_total": 0,
            "progress_percent": 0,
            "message": message,
            "target_type": target_type,
            "target_id": target_id,
            "idempotency_key": idempotency_key,
            "payload": json.dumps(payload or {}, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        }
        with begin() as db:
            db.execute(insert(apply_jobs).values(**row))
        self._emit(row)
        return self.get(row["id"]) or row

    def create_or_get(
        self,
        job_type: str,
        *,
        idempotency_key: str,
        target_type: str | None = None,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
        message: str = "任务已创建，等待 Worker 执行。",
    ) -> tuple[dict[str, Any], bool]:
        """Create one active job per key, including concurrent API requests."""

        existing = self.active_by_key(idempotency_key)
        if existing:
            return existing, False
        try:
            return self.create(
                job_type,
                target_type=target_type,
                target_id=target_id,
                payload=payload,
                message=message,
                idempotency_key=idempotency_key,
            ), True
        except IntegrityError:
            existing = self.active_by_key(idempotency_key)
            if existing:
                return existing, False
            raise

    def active_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with begin() as db:
            row = (
                db.execute(
                    select(apply_jobs)
                    .where(
                        apply_jobs.c.idempotency_key == idempotency_key,
                        apply_jobs.c.status.in_(("queued", "running")),
                    )
                    .order_by(desc(apply_jobs.c.created_at))
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return self._decode(dict(row)) if row else None

    def fail_stale(self, max_age_minutes: int = 30) -> int:
        """Release jobs abandoned by a terminated Worker."""

        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        now = utc_now_iso()
        with begin() as db:
            result = db.execute(
                update(apply_jobs)
                .where(
                    apply_jobs.c.status.in_(("queued", "running")),
                    apply_jobs.c.updated_at < cutoff,
                )
                .values(
                    status="failed",
                    error="任务因 Worker 中断而过期，可安全重试。",
                    message="后台 Worker 曾中断，该任务已标记为失败，请重试。",
                    finished_at=now,
                    updated_at=now,
                )
            )
        return int(result.rowcount or 0)

    @contextmanager
    def execution_guard(self, job_id: str) -> Iterator[bool]:
        """Allow one Worker to execute a durable job at a time.

        PostgreSQL session advisory locks are released automatically when a
        Worker process dies. Celery can therefore redeliver an unacknowledged
        task without risking concurrent duplicate writes.
        """

        connection = engine().connect()
        acquired = True
        try:
            if connection.dialect.name == "postgresql":
                acquired = bool(
                    connection.execute(
                        text("SELECT pg_try_advisory_lock(hashtextextended(:job_id, 0))"),
                        {"job_id": job_id},
                    ).scalar()
                )
            if not acquired:
                yield False
                return
            status = connection.execute(
                select(apply_jobs.c.status).where(apply_jobs.c.id == job_id)
            ).scalar()
            if connection.in_transaction():
                connection.commit()
            yield bool(status and status not in TERMINAL_STATUSES)
        finally:
            if acquired and connection.dialect.name == "postgresql":
                connection.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:job_id, 0))"),
                    {"job_id": job_id},
                )
                if connection.in_transaction():
                    connection.commit()
            connection.close()

    def _current_account_id(self) -> str | None:
        if self.account_id:
            return self.account_id
        from .postgres import apply_accounts

        with begin() as db:
            return db.execute(
                select(apply_accounts.c.id)
                .order_by(
                    desc(apply_accounts.c.last_import_at), desc(apply_accounts.c.last_seen_at)
                )
                .limit(1)
            ).scalar()

    def mark_task(self, job_id: str, celery_task_id: str | None) -> None:
        if not celery_task_id:
            return
        self.update(job_id, celery_task_id=celery_task_id)

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        now = utc_now_iso()
        if fields.get("status") == "running" and not fields.get("started_at"):
            fields["started_at"] = now
        if fields.get("status") in TERMINAL_STATUSES and not fields.get("finished_at"):
            fields["finished_at"] = now
        if fields.get("status") == "ok" and "error" not in fields:
            fields["error"] = None
        fields["updated_at"] = now
        with begin() as db:
            db.execute(update(apply_jobs).where(apply_jobs.c.id == job_id).values(**fields))
        row = self.get(job_id) or {"id": job_id, **fields}
        self._emit(row)
        return row

    def progress(
        self,
        job_id: str,
        *,
        current: int | None = None,
        total: int | None = None,
        percent: int | None = None,
        message: str | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if current is not None:
            fields["progress_current"] = int(current)
        if total is not None:
            fields["progress_total"] = int(total)
        if percent is not None:
            fields["progress_percent"] = max(0, min(100, int(percent)))
        elif current is not None and total:
            fields["progress_percent"] = round(int(current) / int(total) * 100)
        if message is not None:
            fields["message"] = message
        if status is not None:
            fields["status"] = status
        if error is not None or status == "ok":
            fields["error"] = error
        return self.update(job_id, **fields)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with begin() as db:
            row = db.execute(select(apply_jobs).where(apply_jobs.c.id == job_id)).mappings().first()
        return self._decode(dict(row)) if row else None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        account_id = self._current_account_id()
        with begin() as db:
            query = select(apply_jobs)
            query = query.where(
                apply_jobs.c.account_id == account_id
                if account_id
                else apply_jobs.c.account_id.is_(None)
            )
            rows = (
                db.execute(query.order_by(desc(apply_jobs.c.created_at)).limit(limit))
                .mappings()
                .all()
            )
        return [self._decode(dict(row)) for row in rows]

    def latest_by_type(self, job_type: str) -> dict[str, Any] | None:
        account_id = self._current_account_id()
        with begin() as db:
            account_filter = (
                apply_jobs.c.account_id == account_id
                if account_id
                else apply_jobs.c.account_id.is_(None)
            )
            row = (
                db.execute(
                    select(apply_jobs)
                    .where(apply_jobs.c.job_type == job_type, account_filter)
                    .order_by(desc(apply_jobs.c.created_at))
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return self._decode(dict(row)) if row else None

    def _emit(self, row: dict[str, Any]) -> None:
        cache_job_progress(row["id"], row)
        publish_apply_event(
            "apply_job_updated",
            job_id=row["id"],
            account_id=row.get("account_id"),
            status=row.get("status"),
            target_type=row.get("target_type"),
            target_id=row.get("target_id"),
            changed_fields=["status", "progress", "message"],
        )

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        raw = row.get("payload")
        if isinstance(raw, str):
            try:
                row["payload"] = json.loads(raw)
            except Exception:
                row["payload"] = {}
        return row


def apply_health() -> dict[str, Any]:
    pg_ok, pg_error = database_ready()
    redis_ok, redis_error = redis_ready()
    worker_ok, worker_error = (
        worker_ready() if redis_ok else (False, "Redis 不可用，无法检测 Worker。")
    )
    can_view = pg_ok
    can_enqueue = pg_ok and redis_ok and worker_ok
    return {
        "postgres": {"ok": pg_ok, "error": pg_error},
        "redis": {"ok": redis_ok, "error": redis_error},
        "worker": {"ok": worker_ok, "error": worker_error},
        "can_view": can_view,
        "can_enqueue": can_enqueue,
        "message": _health_message(pg_ok, redis_ok, worker_ok, pg_error, redis_error, worker_error),
    }


def _health_message(
    pg_ok: bool,
    redis_ok: bool,
    worker_ok: bool,
    pg_error: str | None,
    redis_error: str | None,
    worker_error: str | None,
) -> str:
    if not pg_ok:
        return f"PostgreSQL 不可用：{pg_error or '未知错误'}。请运行 docker compose up -d postgres redis 后执行 capybot db upgrade。"
    if not redis_ok:
        return f"Redis 不可用：{redis_error or '未知错误'}。请运行 docker compose up -d redis。"
    if not worker_ok:
        return worker_error or "Celery Worker 未启动，请运行 capybot apply worker。"
    return "Capybot Apply 依赖正常。"
