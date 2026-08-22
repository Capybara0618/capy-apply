"""Celery tasks for BOSS sync and evidence-first opportunity decisions."""

from __future__ import annotations

import asyncio
import functools
import json
from typing import Any

from capybot.connectors.boss import BossConnector

from .agent_runtime.fit_evaluator import JobFitAnalysisService
from .celery_app import celery_app, enqueue_apply_task
from .events import publish_apply_event, worker_heartbeat
from .importer import SnapshotImporter
from .jobs import ApplyJobStore
from .opportunity_service import OpportunityAnalysisService
from .store import ApplyStore


def _guarded_job(job_index: int = 0):
    """Serialize Celery redeliveries through the durable PostgreSQL job."""

    def decorate(function):
        @functools.wraps(function)
        def guarded(*args, **kwargs):
            job_id = str(args[job_index] if len(args) > job_index else kwargs["job_id"])
            jobs, _store = _job_scope(job_id)
            with jobs.execution_guard(job_id) as acquired:
                if not acquired:
                    return {"job_id": job_id, "status": "duplicate_ignored"}
                return function(*args, **kwargs)

        return guarded

    return decorate


@celery_app.task(name="apply.heartbeat")
def heartbeat() -> str:
    worker_heartbeat("default")
    return "ok"


@celery_app.task(bind=True, name="apply.sync_account", max_retries=2)
@_guarded_job(job_index=1)
def import_boss_snapshot(
    self,
    job_id: str,
    days: int = 30,
    account_id: str | None = None,
    analyze: bool = True,
) -> dict[str, Any]:
    """Synchronize BOSS into the raw evidence repository, without running an Agent inline."""

    jobs = ApplyJobStore()
    worker_heartbeat()
    jobs.progress(
        job_id,
        status="running",
        percent=1,
        message=f"开始同步近 {days} 天 BOSS 聊天。",
    )

    def update(progress: dict[str, Any]) -> None:
        current = int(progress.get("current") or 0)
        total = int(progress.get("total") or 0)
        percent = int(progress.get("percent") or (round(current / total * 100) if total else 0))
        jobs.progress(
            job_id,
            current=current,
            total=total,
            percent=percent,
            message=str(progress.get("message") or "正在同步 BOSS 证据。"),
        )
        publish_apply_event(
            "apply_import_updated",
            job_id=job_id,
            status="running",
        )

    try:
        result = asyncio.run(
            SnapshotImporter(
                boss=BossConnector(),
                progress_callback=update,
            ).import_boss_async(days=days)
        )
        source_status = (
            result.get("source_status") if isinstance(result.get("source_status"), dict) else {}
        )
        explicit_empty = bool(source_status.get("explicitly_empty"))
        if int(result.get("successful_conversations") or 0) == 0 and not explicit_empty:
            error = str(
                (result.get("failures") or [{}])[0].get("error") or "未获取到任何 BOSS 会话"
            )
            if (
                _is_transient_boss_error(RuntimeError(error))
                and self.request.retries < self.max_retries
            ):
                delay = 5 * (2**self.request.retries)
                jobs.update(
                    job_id,
                    status="queued",
                    error=error,
                    message=f"BOSS 连接暂时失败，{delay} 秒后自动重试。",
                )
                raise self.retry(exc=RuntimeError(error), countdown=delay)
            jobs.update(
                job_id,
                status="failed",
                error=error,
                message=f"同步失败：{error[:180]}",
            )
            publish_apply_event(
                "apply_import_updated",
                job_id=job_id,
                status="failed",
            )
            return result

        imported_account_id = str(result.get("account_id") or account_id or "") or None
        if imported_account_id:
            jobs.update(job_id, account_id=imported_account_id)
        trigger_job = (
            enqueue_import_analysis(str(result.get("import_run_id") or ""))
            if analyze
            else None
        )
        result["trigger_job_id"] = trigger_job.get("id") if trigger_job else None
        jobs.progress(
            job_id,
            status="ok",
            percent=100,
            message=(
                str(source_status.get("message"))
                if explicit_empty
                else "BOSS 证据同步完成，变化机会已交给 DecisionRouter。"
            ),
        )
        publish_apply_event(
            "apply_overview_invalidated",
            job_id=job_id,
            status="ok",
        )
        return result
    except Exception as exc:
        if _is_transient_boss_error(exc) and self.request.retries < self.max_retries:
            delay = 5 * (2**self.request.retries)
            jobs.update(
                job_id,
                status="queued",
                error=str(exc),
                message=f"BOSS 连接暂时失败，{delay} 秒后自动重试。",
            )
            raise self.retry(exc=exc, countdown=delay)
        jobs.update(
            job_id,
            status="failed",
            error=str(exc),
            message=f"同步失败：{exc}",
        )
        raise


def _is_transient_boss_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "no close frame",
            "connection closed",
            "websocket",
            "page.navigate",
            "target closed",
            "connection reset",
        )
    )


def enqueue_import_analysis(import_run_id: str) -> dict[str, Any] | None:
    if not import_run_id:
        return None
    with ApplyStore().connect() as db:
        row = db.execute(
            "SELECT account_id FROM import_runs WHERE id=?",
            (import_run_id,),
        ).fetchone()
    jobs = ApplyJobStore(account_id=str(row["account_id"]) if row and row["account_id"] else None)
    job, created = jobs.create_or_get(
        "trigger_import_analysis",
        idempotency_key=f"trigger_import_analysis:{import_run_id}",
        target_type="import_run",
        target_id=import_run_id,
        payload={"import_run_id": import_run_id},
        message="增量触发任务已创建。",
    )
    if created:
        queued = enqueue_apply_task(
            trigger_import_analysis,
            "trigger_import_analysis",
            job["id"],
            import_run_id,
        )
        jobs.mark_task(job["id"], queued.id)
    return job


@celery_app.task(name="apply.trigger_import_analysis")
@_guarded_job()
def trigger_import_analysis(job_id: str, import_run_id: str) -> dict[str, Any]:
    """Route changed evidence into one Opportunity Agent run per opportunity."""

    jobs, store = _job_scope(job_id)
    worker_heartbeat()
    items = store.import_run_items(import_run_id, limit=1000)
    total = len(items)
    counts = {"skipped": 0, "opportunity_agent": 0, "failed": 0}
    jobs.progress(
        job_id,
        status="running",
        current=0,
        total=total,
        percent=1,
        message="DecisionRouter 正在识别有价值的增量证据。",
    )
    for index, item in enumerate(items, start=1):
        jobs.progress(
            job_id,
            status="running",
            current=index,
            total=total,
            message=f"DecisionRouter 正在处理 {index}/{total}",
        )
        try:
            mode = _analysis_mode(item)
            opportunity_id = str(item.get("opportunity_id") or "")
            if mode == "skipped" or not opportunity_id:
                _update_import_item(
                    str(item["id"]),
                    analysis_mode="skipped",
                    skipped_reason=(item.get("skipped_reason") or "本次同步没有可分析的新证据。"),
                )
                counts["skipped"] += 1
                continue
            child, created = jobs.create_or_get(
                "analyze_opportunity",
                idempotency_key=f"analyze_opportunity:import-item:{item['id']}",
                target_type="opportunity",
                target_id=opportunity_id,
                payload={
                    "import_run_id": import_run_id,
                    "import_run_item_id": item["id"],
                    "analysis_mode": "opportunity_agent",
                },
                message="机会决策 Agent 已入队。",
            )
            trigger = {
                "type": "import_delta",
                "import_run_id": import_run_id,
                "import_run_item_id": item["id"],
                "new_message_ids": item.get("new_message_ids") or [],
                "before_stage": item.get("before_stage"),
            }
            if created:
                queued = enqueue_apply_task(
                    analyze_opportunity,
                    "analyze_opportunity",
                    child["id"],
                    opportunity_id,
                    trigger,
                )
                jobs.mark_task(child["id"], queued.id)
            _update_import_item(
                str(item["id"]),
                analysis_mode="opportunity_agent",
            )
            counts["opportunity_agent"] += 1
        except Exception as exc:
            counts["failed"] += 1
            if item.get("id"):
                _update_import_item(
                    str(item["id"]),
                    analysis_mode="failed",
                    skipped_reason=str(exc),
                )
    _merge_import_report(
        import_run_id,
        {"queued_opportunities": counts["opportunity_agent"]},
    )
    jobs.progress(
        job_id,
        status="ok",
        current=total,
        total=total,
        percent=100,
        message="DecisionRouter 已完成增量分流。",
    )
    publish_apply_event(
        "apply_overview_invalidated",
        job_id=job_id,
        import_run_id=import_run_id,
        status="ok",
    )
    return {"import_run_id": import_run_id, **counts}


def _analysis_mode(item: dict[str, Any]) -> str:
    if int(item.get("new_message_count") or 0) <= 0:
        return "skipped"
    if str(item.get("analysis_mode") or "").lower() in {"skipped", "failed"}:
        return str(item.get("analysis_mode")).lower()
    return "opportunity_agent"


def _opportunity_mcp_env(store: ApplyStore) -> dict[str, str]:
    """Bind demo MCP reads to the packaged fixture without affecting real accounts."""

    account = store.current_account() or {}
    if str(account.get("source") or "") != "demo_fixture":
        return {}
    from .demo import DEMO_FIXTURE

    return {"CAPYBOT_BOSS_FIXTURE": str(DEMO_FIXTURE)}


@celery_app.task(name="apply.analyze_opportunity")
@_guarded_job()
def analyze_opportunity(
    job_id: str,
    opportunity_id: str,
    trigger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jobs, store = _job_scope(job_id)
    worker_heartbeat()
    jobs.progress(
        job_id,
        status="running",
        percent=10,
        message="Opportunity Agent 正在构造最小上下文。",
    )
    try:
        result = asyncio.run(
            OpportunityAnalysisService(
                store,
                mcp_env=_opportunity_mcp_env(store),
            ).analyze(
                opportunity_id,
                trigger=trigger,
            )
        )
        if isinstance(trigger, dict):
            item_id = str(trigger.get("import_run_item_id") or "")
            stage = (
                (result.get("decision") or {}).get("stage")
                if isinstance(result.get("decision"), dict)
                else None
            )
            if item_id:
                routing_mode = str(
                    ((result.get("metrics") or {}).get("routing_mode"))
                    or "opportunity_agent"
                )
                _update_import_item(
                    item_id,
                    analysis_mode=routing_mode,
                    after_stage=stage,
                )
            _recompute_import_analysis_report(str(trigger.get("import_run_id") or ""))
        jobs.progress(
            job_id,
            status="ok" if result.get("accepted") else "failed",
            percent=100,
            message=(
                "机会决策已通过 CommitGate。"
                if result.get("accepted")
                else "机会决策未通过 CommitGate，请查看 Agent Trace。"
            ),
            error=None if result.get("accepted") else "；".join(result.get("errors") or []),
        )
        for event_type in (
            "apply_opportunity_updated",
            "apply_agent_run_updated",
            "apply_overview_invalidated",
        ):
            publish_apply_event(
                event_type,
                job_id=job_id,
                opportunity_id=opportunity_id,
                agent_run_id=result.get("run_id"),
                status="ok" if result.get("accepted") else "failed",
            )
        if result.get("accepted"):
            _enqueue_fit_if_stale(store, jobs, opportunity_id)
        return result
    except Exception as exc:
        jobs.update(
            job_id,
            status="failed",
            error=str(exc),
            message=f"Opportunity Agent 执行失败：{exc}",
        )
        raise


@celery_app.task(name="apply.analyze_job_fit")
@_guarded_job()
def analyze_job_fit(job_id: str, opportunity_id: str) -> dict[str, Any]:
    jobs, store = _job_scope(job_id)
    worker_heartbeat()
    jobs.progress(
        job_id,
        status="running",
        percent=10,
        message="正在读取简历、偏好和岗位证据。",
    )
    try:
        result = asyncio.run(JobFitAnalysisService(store).analyze(opportunity_id))
        jobs.progress(
            job_id,
            status="ok",
            percent=100,
            message="岗位契合度与机会优先级已更新。",
        )
        for event_type in (
            "apply_opportunity_updated",
            "apply_agent_run_updated",
            "apply_overview_invalidated",
        ):
            publish_apply_event(
                event_type,
                job_id=job_id,
                opportunity_id=opportunity_id,
                agent_run_id=result.get("run_id"),
                status="ok",
            )
        return result
    except Exception as exc:
        jobs.update(
            job_id,
            status="failed",
            error=str(exc),
            message=f"岗位契合度评估失败：{exc}",
        )
        raise


@celery_app.task(name="apply.rebuild_derived_from_l1")
@_guarded_job()
def rebuild_derived_from_l1(job_id: str, limit: int = 500) -> dict[str, Any]:
    """Rebuild projections by enqueuing one independent Agent run per opportunity."""

    jobs, store = _job_scope(job_id)
    worker_heartbeat()
    jobs.progress(
        job_id,
        status="running",
        percent=1,
        message="正在清理旧投影并重建机会索引。",
    )
    try:
        store.clear_current_account_derived(preserve_agent_runs=True)
        account_id = store.current_account_id()
        with store.connect() as db:
            rows = db.execute(
                """
                SELECT id FROM boss_conversations
                WHERE account_id=?
                ORDER BY COALESCE(last_message_at, updated_at) DESC
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        opportunity_ids: list[str] = []
        for row in rows:
            for opportunity_id in store.ensure_opportunities_for_conversation(row["id"]):
                if opportunity_id not in opportunity_ids:
                    opportunity_ids.append(opportunity_id)

        queued_count = 0
        total = len(opportunity_ids)
        for index, opportunity_id in enumerate(opportunity_ids, start=1):
            jobs.progress(
                job_id,
                current=index,
                total=total,
                message=f"正在入队机会 {index}/{total}",
            )
            child, created = jobs.create_or_get(
                "analyze_opportunity",
                idempotency_key=f"rebuild:{job_id}:{opportunity_id}",
                target_type="opportunity",
                target_id=opportunity_id,
                payload={
                    "opportunity_id": opportunity_id,
                    "source_job_id": job_id,
                    "trigger": "rebuild",
                },
                message="重建机会决策已入队。",
            )
            if created:
                queued = enqueue_apply_task(
                    analyze_opportunity,
                    "analyze_opportunity",
                    child["id"],
                    opportunity_id,
                    {"type": "rebuild", "source_job_id": job_id},
                )
                jobs.mark_task(child["id"], queued.id)
                queued_count += 1
        jobs.progress(
            job_id,
            status="ok",
            percent=100,
            message=f"已为 {queued_count} 个机会创建独立 Agent 任务。",
        )
        publish_apply_event(
            "apply_overview_invalidated",
            job_id=job_id,
            status="ok",
        )
        return {
            "opportunities": total,
            "queued": queued_count,
            "engine": OpportunityAnalysisService.ENGINE,
        }
    except Exception as exc:
        jobs.update(
            job_id,
            status="failed",
            error=str(exc),
            message=f"重建失败：{exc}",
        )
        raise


def _get_import_item(item_id: str) -> dict[str, Any] | None:
    with ApplyStore().connect() as db:
        row = db.execute(
            "SELECT * FROM import_run_items WHERE id=?",
            (item_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    try:
        item["new_message_ids"] = json.loads(item.get("new_message_ids") or "[]")
    except Exception:
        item["new_message_ids"] = []
    return item


def _enqueue_fit_if_stale(
    store: ApplyStore,
    jobs: ApplyJobStore,
    opportunity_id: str,
) -> dict[str, Any] | None:
    context = store.opportunity_context(opportunity_id)
    profile = context.get("candidate_profile") or {}
    fit = context.get("fit_analysis") or {}
    if not str(profile.get("resume_markdown") or "").strip():
        return None
    if str(fit.get("status") or "") in {"ok", "needs_review", "no_profile"}:
        return None
    profile_version = str(profile.get("updated_at") or "profile")
    snapshots = context.get("job_snapshots") or []
    jobs_data = context.get("jobs") or []
    job_version = str(
        (snapshots[0].get("captured_at") if snapshots else None)
        or (jobs_data[0].get("updated_at") if jobs_data else None)
        or "job"
    )
    child, created = jobs.create_or_get(
        "analyze_job_fit",
        idempotency_key=(
            f"{store.current_account_id()}:analyze_job_fit:"
            f"{opportunity_id}:{profile_version}:{job_version}"
        ),
        target_type="opportunity",
        target_id=opportunity_id,
        payload={"opportunity_id": opportunity_id},
        message="岗位契合度评估已入队。",
    )
    if created:
        queued = enqueue_apply_task(
            analyze_job_fit,
            "analyze_job_fit",
            child["id"],
            opportunity_id,
        )
        jobs.mark_task(child["id"], queued.id)
    return child


def _update_import_item(item_id: str, **fields: Any) -> None:
    if not item_id or not fields:
        return
    allowed = {
        "analysis_mode",
        "skipped_reason",
        "before_stage",
        "after_stage",
    }
    pairs = [(key, value) for key, value in fields.items() if key in allowed]
    if not pairs:
        return
    assignments = ", ".join(f"{key}=?" for key, _ in pairs)
    values = [value for _, value in pairs]
    with ApplyStore().connect() as db:
        db.execute(
            f"UPDATE import_run_items SET {assignments} WHERE id=?",
            (*values, item_id),
        )


def _merge_import_report(import_run_id: str, patch: dict[str, Any]) -> None:
    if not import_run_id:
        return
    with ApplyStore().connect() as db:
        row = db.execute(
            "SELECT report FROM import_runs WHERE id=?",
            (import_run_id,),
        ).fetchone()
        if not row:
            return
        try:
            report = json.loads(row["report"] or "{}")
        except Exception:
            report = {}
        report.update(patch)
        db.execute(
            "UPDATE import_runs SET report=? WHERE id=?",
            (json.dumps(report, ensure_ascii=False), import_run_id),
        )


def _recompute_import_analysis_report(import_run_id: str) -> None:
    if not import_run_id:
        return
    with ApplyStore().connect() as db:
        row = db.execute(
            "SELECT report FROM import_runs WHERE id=?",
            (import_run_id,),
        ).fetchone()
        if not row:
            return
        try:
            report = json.loads(row["report"] or "{}")
        except Exception:
            report = {}
        items = db.execute(
            """
            SELECT opportunity_id, analysis_mode, after_stage
            FROM import_run_items
            WHERE import_run_id=?
            """,
            (import_run_id,),
        ).fetchall()
        completed = {
            str(item["opportunity_id"])
            for item in items
            if item["opportunity_id"]
            and item["after_stage"]
            and str(item["analysis_mode"] or "")
            in {"opportunity_agent", "cold_projection"}
        }
        queued = {
            str(item["opportunity_id"])
            for item in items
            if item["opportunity_id"]
            and not item["after_stage"]
            and str(item["analysis_mode"] or "") == "opportunity_agent"
        }
        report["analyzed_opportunities"] = len(completed)
        report["queued_opportunities"] = len(queued)
        db.execute(
            "UPDATE import_runs SET report=? WHERE id=?",
            (json.dumps(report, ensure_ascii=False), import_run_id),
        )


def _job_scope(job_id: str) -> tuple[ApplyJobStore, ApplyStore]:
    """Bind every Worker operation to the account captured by its durable job."""

    unscoped = ApplyJobStore()
    job = unscoped.get(job_id) or {}
    account_id = str(job.get("account_id") or "") or None
    return (
        ApplyJobStore(account_id=account_id),
        ApplyStore(account_id=account_id),
    )
