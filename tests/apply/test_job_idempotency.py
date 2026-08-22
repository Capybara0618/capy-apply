import threading

from capybot.apply.jobs import ApplyJobStore
from capybot.apply.store import ApplyStore


def test_active_jobs_are_idempotent_and_terminal_jobs_can_be_recreated():
    jobs = ApplyJobStore()

    first, first_created = jobs.create_or_get(
        "analyze_opportunity",
        idempotency_key="test_account:analyze_opportunity:opp-1",
        target_type="opportunity",
        target_id="opp-1",
    )
    duplicate, duplicate_created = jobs.create_or_get(
        "analyze_opportunity",
        idempotency_key="test_account:analyze_opportunity:opp-1",
        target_type="opportunity",
        target_id="opp-1",
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate["id"] == first["id"]

    jobs.update(first["id"], status="ok")
    retried, retry_created = jobs.create_or_get(
        "analyze_opportunity",
        idempotency_key="test_account:analyze_opportunity:opp-1",
        target_type="opportunity",
        target_id="opp-1",
    )

    assert retry_created is True
    assert retried["id"] != first["id"]


def test_progress_can_record_terminal_error():
    jobs = ApplyJobStore()
    job = jobs.create("analyze_opportunity", idempotency_key="progress:error")

    updated = jobs.progress(
        job["id"],
        status="failed",
        percent=100,
        message="模型请求失败",
        error="upstream 429",
    )

    assert updated["status"] == "failed"
    assert updated["progress_percent"] == 100
    assert updated["error"] == "upstream 429"


def test_stale_jobs_are_released_for_retry():
    jobs = ApplyJobStore()
    job, _ = jobs.create_or_get(
        "import_boss_snapshot",
        idempotency_key="test_account:import:stale",
    )
    with ApplyStore().connect() as db:
        db.execute(
            "UPDATE apply_jobs SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00+00:00", job["id"]),
        )

    assert jobs.fail_stale(max_age_minutes=30) == 1
    assert jobs.get(job["id"])["status"] == "failed"
    _, created = jobs.create_or_get(
        "import_boss_snapshot",
        idempotency_key="test_account:import:stale",
    )
    assert created is True


def test_postgres_execution_guard_rejects_concurrent_duplicate():
    jobs = ApplyJobStore()
    job = jobs.create("analyze_opportunity", idempotency_key="guard:opp-1")
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[bool] = []

    def hold_guard() -> None:
        with jobs.execution_guard(job["id"]) as acquired:
            outcomes.append(acquired)
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_guard)
    thread.start()
    assert entered.wait(timeout=5)
    with jobs.execution_guard(job["id"]) as acquired:
        outcomes.append(acquired)
    release.set()
    thread.join(timeout=5)

    assert outcomes == [True, False]


def test_execution_guard_ignores_terminal_redelivery():
    jobs = ApplyJobStore()
    job = jobs.create("analyze_opportunity", idempotency_key="guard:done")
    jobs.update(job["id"], status="ok")

    with jobs.execution_guard(job["id"]) as acquired:
        assert acquired is False


def test_execution_guard_does_not_hold_an_idle_transaction():
    jobs = ApplyJobStore()
    job = jobs.create("analyze_opportunity", idempotency_key="guard:no-idle-transaction")

    with jobs.execution_guard(job["id"]) as acquired:
        assert acquired is True
        with ApplyStore().connect() as db:
            rows = db.execute(
                """
                SELECT a.state
                FROM pg_locks l
                JOIN pg_stat_activity a ON a.pid=l.pid
                WHERE l.locktype='advisory' AND l.granted
                  AND a.datname=current_database()
                """
            ).fetchall()

    assert rows
    assert all(row["state"] != "idle in transaction" for row in rows)
