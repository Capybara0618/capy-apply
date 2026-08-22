"""HTTP payload helpers for the Capybot Apply WebUI."""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import parse_qs

from capybot.apply.agent_runs import AgentRunRepository
from capybot.apply.celery_app import enqueue_apply_task
from capybot.apply.jobs import ApplyJobStore, apply_health
from capybot.apply.resume_pdf import ResumePdfError, parse_resume_pdf_data_url
from capybot.apply.store import ApplyStore
from capybot.apply.tasks import (
    analyze_job_fit,
    analyze_opportunity,
    enqueue_import_analysis,
    import_boss_snapshot,
    rebuild_derived_from_l1,
)
from capybot.connectors.boss import BossConnector

_boss = BossConnector()
_boss_status_cache: dict[str, Any] = {"value": None, "checked_at": 0.0, "checked_monotonic": 0.0}
_boss_status_ttl_s = 30.0
_health_cache: dict[str, Any] = {"value": None, "checked_monotonic": 0.0}
_health_ttl_s = 5.0
_maintenance_lock = threading.Lock()
_maintenance_started = False


class ApplyAPIError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _store() -> ApplyStore:
    return ApplyStore()


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def apply_health_payload(*, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _health_cache.get("value")
    checked = float(_health_cache.get("checked_monotonic") or 0.0)
    if not force and isinstance(cached, dict) and now - checked < _health_ttl_s:
        return dict(cached)
    value = apply_health()
    _health_cache["value"] = dict(value)
    _health_cache["checked_monotonic"] = now
    return value


def _require_postgres() -> None:
    health = apply_health_payload()
    if not health.get("postgres", {}).get("ok"):
        raise ApplyAPIError(
            str(
                health.get("message")
                or "PostgreSQL 不可用，请先启动 docker compose up -d postgres redis"
            ),
            status=503,
        )


def apply_jobs_payload(job_id: str | None = None) -> dict[str, Any]:
    if not apply_health_payload().get("postgres", {}).get("ok"):
        return {"job": None} if job_id else {"jobs": []}
    jobs = ApplyJobStore()
    if job_id:
        job = jobs.get(job_id)
        if job is None:
            raise ApplyAPIError("任务不存在", status=404)
        return {"job": job}
    return {"jobs": jobs.list(limit=100)}


def apply_job_retry(job_id: str) -> dict[str, Any]:
    _require_postgres()
    job = ApplyJobStore().get(job_id)
    if job is None:
        raise ApplyAPIError("任务不存在", status=404)
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    job_type = str(job.get("job_type") or "")
    if job_type == "import_boss_snapshot":
        return _enqueue_import(int(payload.get("days") or 30), retry_of=job_id)
    if job_type == "rebuild_derived_from_l1":
        return _enqueue_rebuild(int(payload.get("limit") or 500), retry_of=job_id)
    if job_type == "analyze_opportunity":
        target_id = str(job.get("target_id") or payload.get("opportunity_id") or "")
        return _enqueue_analyze_opportunity(
            target_id,
            payload.get("delta") if isinstance(payload.get("delta"), dict) else None,
            retry_of=job_id,
        )
    if job_type == "analyze_job_fit":
        target_id = str(job.get("target_id") or payload.get("opportunity_id") or "")
        return _enqueue_job_fit(target_id, retry_of=job_id)
    raise ApplyAPIError(f"暂不支持重试任务类型：{job_type}")


def apply_overview() -> dict[str, Any]:
    _schedule_apply_maintenance()
    try:
        payload = _store().overview()
    except Exception as exc:
        health = apply_health_payload(force=True)
        if health.get("postgres", {}).get("ok"):
            raise ApplyAPIError("账号数据暂时读取失败，请稍后重试。", status=503) from exc
        return {
            "metrics": {},
            "action_items": [],
            "stage_changes": [],
            "risk_opportunities": [],
            "top_job_fits": [],
            "high_priority_opportunities": [],
            "profile_ready": False,
            "latest_import": None,
            "recent_delta_panel": None,
            "agent_runs": [],
            "current_account": None,
            "health": health,
        }
    cached_health = _health_cache.get("value")
    if isinstance(cached_health, dict):
        payload["health"] = dict(cached_health)
    return payload


def _schedule_apply_maintenance() -> None:
    """Run startup checks once without delaying the first Apply page read."""

    global _maintenance_started
    with _maintenance_lock:
        if _maintenance_started:
            return
        _maintenance_started = True

    def run() -> None:
        try:
            apply_health_payload(force=True)
            _enqueue_derived_rebuild_if_needed()
            _enqueue_session_sync_if_ready()
        except Exception:
            return

    threading.Thread(target=run, name="capybot-apply-maintenance", daemon=True).start()


def _enqueue_session_sync_if_ready() -> None:
    """Treat one successful app startup as one user-initiated incremental sync."""

    health = apply_health()
    if not health.get("can_enqueue"):
        return
    status = apply_login_status(force=True)
    if not status.get("logged_in"):
        return
    _enqueue_import(30)


def apply_opportunities() -> dict[str, Any]:
    if not apply_health_payload().get("postgres", {}).get("ok"):
        return {"opportunities": []}
    return {"opportunities": _store().opportunities()}


def apply_opportunity_detail(opportunity_id: str) -> dict[str, Any]:
    if not apply_health_payload().get("postgres", {}).get("ok"):
        raise ApplyAPIError(
            "PostgreSQL 不可用，请先启动 docker compose up -d postgres redis", status=503
        )
    detail = _store().opportunity_detail(opportunity_id)
    if detail is None:
        raise ApplyAPIError("机会不存在", status=404)
    return detail


def apply_opportunity_evidence(query_string: str) -> dict[str, Any]:
    if not apply_health_payload().get("postgres", {}).get("ok"):
        return {"messages": []}
    query = parse_qs(query_string, keep_blank_values=True)
    raw = _first(query, "evidence_refs") or _first(query, "message_ids")
    refs = [item.strip() for item in raw.split(",") if item.strip()]
    return _store().resolve_evidence_refs(refs)


def apply_tasks() -> dict[str, Any]:
    if not apply_health_payload().get("postgres", {}).get("ok"):
        return {"tasks": [], "suggestions": []}
    return _store().tasks_payload()


def apply_agent_runs(run_id: str | None = None) -> dict[str, Any]:
    if not apply_health_payload().get("postgres", {}).get("ok"):
        return {"run": None, "steps": []} if run_id else {"runs": []}
    return AgentRunRepository(_store()).payload(run_id)


def apply_profile() -> dict[str, Any]:
    if not apply_health_payload().get("postgres", {}).get("ok"):
        return {"profile": None, "preferences": None}
    return _store().profile_payload()


def apply_profile_update(query_string: str) -> dict[str, Any]:
    _require_postgres()
    query = parse_qs(query_string, keep_blank_values=True)
    raw = _first(query, "payload")
    if not raw:
        raise ApplyAPIError("缺少 profile payload")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApplyAPIError("profile payload 必须是 JSON") from exc
    if not isinstance(payload, dict):
        raise ApplyAPIError("profile payload 必须是对象")
    result = _store().update_profile(payload)
    _start_profile_fit_analysis(limit=200)
    result["profile_reanalysis"] = {
        "status": "running",
        "message": "已保存简历画像，正在后台重新计算全部岗位契合度。",
    }
    return result


def apply_profile_upload_pdf(payload: dict[str, Any]) -> dict[str, Any]:
    _require_postgres()
    if not isinstance(payload, dict):
        raise ApplyAPIError("PDF 上传 payload 必须是对象")
    data_url = payload.get("data_url")
    filename = payload.get("filename")
    if not isinstance(data_url, str) or not data_url:
        raise ApplyAPIError("缺少 PDF 文件数据")
    try:
        parsed = parse_resume_pdf_data_url(data_url, filename=str(filename or "resume.pdf"))
    except ResumePdfError as exc:
        raise ApplyAPIError(str(exc)) from exc
    preferences = payload.get("preferences") if isinstance(payload.get("preferences"), dict) else {}
    result = _store().update_profile(
        {
            "resume_markdown": parsed.markdown,
            "preferences": preferences,
        }
    )
    _start_profile_fit_analysis(limit=200)
    result["pdf_parse"] = parsed.to_payload()
    result["profile_reanalysis"] = {
        "status": "running",
        "message": "已从 PDF 简历生成画像，正在后台重新计算全部岗位契合度。",
    }
    return result


def apply_import_report() -> dict[str, Any]:
    if not apply_health_payload().get("postgres", {}).get("ok"):
        return {"latest_import": None}
    latest = _store().overview().get("latest_import")
    return {"latest_import": latest}


def apply_rebuild_status() -> dict[str, Any]:
    try:
        latest = ApplyJobStore().latest_by_type("rebuild_derived_from_l1")
    except Exception:
        latest = None
    return _job_progress_payload(latest, idle_message="尚未开始派生分析重建。")


def apply_rebuild_start(limit: int = 500) -> dict[str, Any]:
    return _enqueue_rebuild(limit)


def apply_clear_derived() -> dict[str, Any]:
    _require_postgres()
    _store().clear_derived()
    return {"ok": True}


def apply_login_status(*, force: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _boss_status_cache.get("value")
    checked_at = float(_boss_status_cache.get("checked_at") or 0.0)
    checked_monotonic = float(_boss_status_cache.get("checked_monotonic") or 0.0)
    if not force and isinstance(cached, dict) and now - checked_monotonic < _boss_status_ttl_s:
        payload = dict(cached)
        payload["last_checked_at"] = checked_at
        payload["stale"] = False
        payload["cached"] = True
        return payload
    return _remember_boss_status(_boss.login_status())


def _remember_boss_status(status: dict[str, Any]) -> dict[str, Any]:
    status = dict(status)
    try:
        persisted = _store().current_account()
        browser_account = status.get("account") or {}
        if (
            persisted
            and str(persisted.get("profile_dir") or "")
            == str(browser_account.get("profile_dir") or status.get("profile_dir") or "")
        ):
            status["account"] = {
                "id": persisted.get("id"),
                "account_uid": persisted.get("account_uid"),
                "display_name": persisted.get("display_name"),
                "profile_dir": persisted.get("profile_dir"),
                "source": persisted.get("source"),
            }
    except Exception:
        pass
    checked_at = time.time()
    checked_monotonic = time.monotonic()
    _boss_status_cache["value"] = dict(status)
    _boss_status_cache["checked_at"] = checked_at
    _boss_status_cache["checked_monotonic"] = checked_monotonic
    status["last_checked_at"] = checked_at
    status["stale"] = False
    status["cached"] = False
    return status


def apply_begin_login() -> dict[str, Any]:
    return _remember_boss_status(_boss.begin_login())


async def apply_begin_login_async() -> dict[str, Any]:
    return _remember_boss_status(await _boss.begin_login_async())


def apply_import_progress() -> dict[str, Any]:
    try:
        jobs = ApplyJobStore()
        latest = jobs.latest_by_type("import_boss_snapshot")
        account = _store().current_account()
        if (
            not latest
            and account
            and str(account.get("source") or "") == "demo_fixture"
        ):
            latest = jobs.latest_by_type("trigger_import_analysis")
        if latest:
            return _job_progress_payload(latest, idle_message="尚未开始导入。")
    except Exception:
        pass
    return _job_progress_payload(None, idle_message="尚未开始导入。")


def apply_import_start(days: int = 30) -> dict[str, Any]:
    status = apply_login_status(force=True)
    if not status.get("logged_in"):
        if status.get("profile_ready") and not status.get("cdp_alive"):
            raise ApplyAPIError(
                "Capybot 专用 BOSS 浏览器尚未启动，请先点击“打开 BOSS 专用浏览器”。", 409
            )
        raise ApplyAPIError(
            "BOSS 聊天页当前不可读，请在 Capybot 专用浏览器中完成登录并保持聊天页打开。", 409
        )
    return _enqueue_import(days)


def apply_demo_start() -> dict[str, Any]:
    """Load an isolated demo account, then enqueue the production Agent path."""

    _ensure_enqueue_ready()
    from capybot.apply.demo import ApplyDemoService

    report = ApplyDemoService().load(reset=True)
    job = enqueue_import_analysis(str(report.get("import_run_id") or ""))
    return {
        "ok": True,
        "report": report,
        "job_id": job.get("id") if job else None,
    }


def _job_progress_payload(job: dict[str, Any] | None, *, idle_message: str) -> dict[str, Any]:
    if not job:
        return {"status": "idle", "message": idle_message, "current": 0, "total": 0, "percent": 0}
    return {
        "status": job.get("status"),
        "phase": job.get("job_type"),
        "message": job.get("message"),
        "current": job.get("progress_current") or 0,
        "total": job.get("progress_total") or 0,
        "percent": job.get("progress_percent") or 0,
        "job_id": job.get("id"),
        "updated_at": job.get("updated_at"),
        "error": job.get("error"),
    }


def _enqueue_derived_rebuild_if_needed(limit: int = 500) -> None:
    """Enqueue derived-state reconstruction once; never run an Agent inside a GET request."""

    health = apply_health()
    if not health.get("can_enqueue"):
        return
    latest = ApplyJobStore().latest_by_type("rebuild_derived_from_l1")
    if latest and latest.get("status") in {"queued", "running"}:
        return
    store = _store()
    account_id = store.current_account_id()
    if not account_id:
        return
    with store.connect() as db:
        raw_count = db.execute(
            "SELECT COUNT(*) FROM boss_conversations WHERE account_id=?",
            (account_id,),
        ).fetchone()[0]
        opportunity_count = db.execute(
            "SELECT COUNT(*) FROM opportunities WHERE account_id=?",
            (account_id,),
        ).fetchone()[0]
    if raw_count > 0 and opportunity_count == 0:
        _enqueue_rebuild(limit)


def apply_clear(include_login: bool = False) -> dict[str, Any]:
    _require_postgres()
    _store().clear()
    if include_login:
        _boss.clear_login_state()
    return {"ok": True}


async def apply_clear_async(include_login: bool = False) -> dict[str, Any]:
    _require_postgres()
    _store().clear()
    if include_login:
        await _boss.clear_login_state_async()
    return {"ok": True}


def apply_reanalyze(target_id: str) -> dict[str, Any]:
    _require_postgres()
    if not target_id:
        raise ApplyAPIError("缺少机会 ID")
    store = _store()
    detail = store.opportunity_detail(target_id)
    if detail:
        return _enqueue_analyze_opportunity(target_id)
    opp_ids = store.ensure_opportunities_for_conversation(target_id)
    if not opp_ids:
        raise ApplyAPIError("未找到可分析机会", status=404)
    return _enqueue_analyze_opportunity(opp_ids[0])


def apply_reanalyze_all(limit: int = 50) -> dict[str, Any]:
    _require_postgres()
    ids = _store().opportunity_ids_for_analysis(limit=limit)
    return {"jobs": [_enqueue_analyze_opportunity(oid) for oid in ids]}


def apply_fit_reanalyze(opportunity_id: str) -> dict[str, Any]:
    _require_postgres()
    if not _store().opportunity_detail(opportunity_id):
        raise ApplyAPIError("机会不存在", status=404)
    return _enqueue_job_fit(opportunity_id)


def apply_fit_reanalyze_all(limit: int = 200) -> dict[str, Any]:
    _require_postgres()
    ids = _store().opportunity_ids_for_analysis(limit=limit)
    return {"jobs": [_enqueue_job_fit(oid) for oid in ids]}


def apply_research_opportunity(opportunity_id: str) -> dict[str, Any]:
    _require_postgres()
    if not _store().opportunity_detail(opportunity_id):
        raise ApplyAPIError("机会不存在", status=404)
    return _enqueue_analyze_opportunity(
        opportunity_id,
        {"type": "research"},
    )


def apply_refresh_boss_opportunity(opportunity_id: str) -> dict[str, Any]:
    _require_postgres()
    if not _store().opportunity_detail(opportunity_id):
        raise ApplyAPIError("机会不存在", status=404)
    return _enqueue_analyze_opportunity(
        opportunity_id,
        {"type": "boss_refresh"},
    )


def _start_profile_fit_analysis(limit: int = 200) -> None:
    for oid in _store().opportunity_ids_for_analysis(limit=limit):
        try:
            _enqueue_job_fit(oid)
        except Exception:
            continue


def _enqueue_import(days: int, *, retry_of: str | None = None) -> dict[str, Any]:
    _ensure_enqueue_ready()
    jobs = ApplyJobStore()
    job, created = jobs.create_or_get(
        "import_boss_snapshot",
        idempotency_key=_job_key("import_boss_snapshot", scope=str(days), retry_of=retry_of),
        payload={"days": days, "retry_of": retry_of},
        message=f"已加入导入队列：近 {days} 天 BOSS 聊天。",
    )
    if created:
        async_result = enqueue_apply_task(
            import_boss_snapshot, "import_boss_snapshot", job["id"], days, None
        )
        jobs.mark_task(job["id"], async_result.id)
    return {"job_id": job["id"], "job": jobs.get(job["id"]), "reused": not created}


def _enqueue_rebuild(limit: int, *, retry_of: str | None = None) -> dict[str, Any]:
    _ensure_enqueue_ready()
    jobs = ApplyJobStore()
    job, created = jobs.create_or_get(
        "rebuild_derived_from_l1",
        idempotency_key=_job_key("rebuild_derived_from_l1", retry_of=retry_of),
        payload={"limit": limit, "retry_of": retry_of},
        message="已加入 Opportunity Agent 派生分析重建队列。",
    )
    if created:
        async_result = enqueue_apply_task(
            rebuild_derived_from_l1, "rebuild_derived_from_l1", job["id"], limit
        )
        jobs.mark_task(job["id"], async_result.id)
    return {"job_id": job["id"], "job": jobs.get(job["id"]), "reused": not created}


def _enqueue_analyze_opportunity(
    opportunity_id: str, delta: dict[str, Any] | None = None, *, retry_of: str | None = None
) -> dict[str, Any]:
    _ensure_enqueue_ready()
    jobs = ApplyJobStore()
    job, created = jobs.create_or_get(
        "analyze_opportunity",
        idempotency_key=_job_key("analyze_opportunity", target=opportunity_id, retry_of=retry_of),
        target_type="opportunity",
        target_id=opportunity_id,
        payload={"opportunity_id": opportunity_id, "delta": delta or {}, "retry_of": retry_of},
        message="已加入 Agent 分析队列。",
    )
    if created:
        async_result = enqueue_apply_task(
            analyze_opportunity, "analyze_opportunity", job["id"], opportunity_id, delta or {}
        )
        jobs.mark_task(job["id"], async_result.id)
    return {"job_id": job["id"], "job": jobs.get(job["id"]), "reused": not created}


def _enqueue_job_fit(
    opportunity_id: str,
    *,
    retry_of: str | None = None,
) -> dict[str, Any]:
    _ensure_enqueue_ready()
    store = _store()
    context = store.opportunity_context(opportunity_id)
    profile = context.get("candidate_profile") or {}
    snapshots = context.get("job_snapshots") or []
    jobs_data = context.get("jobs") or []
    profile_version = str(profile.get("updated_at") or "profile")
    job_version = str(
        (snapshots[0].get("captured_at") if snapshots else None)
        or (jobs_data[0].get("updated_at") if jobs_data else None)
        or "job"
    )
    jobs = ApplyJobStore()
    job, created = jobs.create_or_get(
        "analyze_job_fit",
        idempotency_key=(
            f"{store.current_account_id()}:analyze_job_fit:"
            f"{opportunity_id}:{profile_version}:{job_version}:"
            f"{'retry:' + retry_of if retry_of else 'active'}"
        ),
        target_type="opportunity",
        target_id=opportunity_id,
        payload={"opportunity_id": opportunity_id, "retry_of": retry_of},
        message="岗位契合度评估已入队。",
    )
    if created:
        async_result = enqueue_apply_task(
            analyze_job_fit,
            "analyze_job_fit",
            job["id"],
            opportunity_id,
        )
        jobs.mark_task(job["id"], async_result.id)
    return {"job_id": job["id"], "job": jobs.get(job["id"]), "reused": not created}


def _job_key(
    job_type: str,
    *,
    target: str | None = None,
    scope: str | None = None,
    retry_of: str | None = None,
) -> str:
    account_id = _store().current_account_id() or "no-account"
    retry_scope = f"retry:{retry_of}" if retry_of else "active"
    return ":".join((account_id, job_type, target or "-", scope or "-", retry_scope))


def _ensure_enqueue_ready(*, require_worker: bool = True) -> None:
    health = apply_health()
    if not health.get("postgres", {}).get("ok"):
        raise ApplyAPIError(str(health.get("message") or "PostgreSQL 不可用"), status=503)
    if not health.get("redis", {}).get("ok"):
        raise ApplyAPIError(str(health.get("message") or "Redis 不可用"), status=503)
    if require_worker and not health.get("worker", {}).get("ok"):
        raise ApplyAPIError(str(health.get("message") or "Celery Worker 未启动"), status=503)


def apply_suggestion_update(
    suggestion_id: str, status: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    _require_postgres()
    if status not in {"accepted", "rejected", "edited"}:
        raise ApplyAPIError("无效的确认状态")
    ok = _store().set_suggestion_status(suggestion_id, status, payload)
    if not ok:
        raise ApplyAPIError("Agent 建议不存在", status=404)
    return {"ok": True}
