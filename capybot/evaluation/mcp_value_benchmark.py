"""Controlled MCP value experiment on an isolated cold-start database."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy.engine import make_url

from capybot.apply.agent_runtime.bootstrap import OpportunityBootstrapBuilder
from capybot.apply.agent_runtime.fit_evaluator import JobFitAnalysisService
from capybot.apply.opportunity_service import OpportunityAnalysisService
from capybot.apply.store import ApplyStore
from capybot.evaluation.cold_start_benchmark import (
    SAFE_DATABASE,
    benchmark_database_url,
)

JOB_FIELDS = (
    "title",
    "company",
    "salary",
    "city",
    "experience",
    "education",
    "description",
    "requirements",
)


async def run_mcp_value_benchmark(
    *,
    database_url: str | None = None,
    job_limit: int = 5,
    company_limit: int = 3,
    request_delay_s: float = 1.5,
) -> dict[str, Any]:
    target_url = database_url or benchmark_database_url()
    database_name = str(make_url(target_url).database or "")
    if not SAFE_DATABASE.fullmatch(database_name):
        raise ValueError("MCP 基准只能在 capybot_apply_benchmark 隔离数据库运行")

    with _database_environment(target_url):
        store = ApplyStore(
            account_id=_benchmark_account_id(target_url),
            database_url=target_url,
        )
        opportunity_ids = _select_job_cases(store, max(1, job_limit))
        if not opportunity_ids:
            raise RuntimeError("隔离库中没有同时具备 HR 互动和 BOSS securityId 的机会")

        agent = OpportunityAnalysisService(store)
        fit = JobFitAnalysisService(store)
        job_cases: list[dict[str, Any]] = []
        successful_job_ids: list[str] = []
        for index, opportunity_id in enumerate(opportunity_ids, start=1):
            case = await _run_job_case(
                store,
                agent,
                fit,
                opportunity_id,
                label=f"job-{index:02d}",
            )
            job_cases.append(case)
            if case["tool_success"]:
                successful_job_ids.append(opportunity_id)
            await asyncio.sleep(max(0.0, request_delay_s))

        company_candidates = [
            *successful_job_ids,
            *[
                opportunity_id
                for opportunity_id in opportunity_ids
                if opportunity_id not in successful_job_ids
            ],
        ]
        company_cases: list[dict[str, Any]] = []
        for index, opportunity_id in enumerate(
            company_candidates[: max(0, company_limit)],
            start=1,
        ):
            company_cases.append(
                await _run_company_case(
                    store,
                    agent,
                    opportunity_id,
                    label=f"company-{index:02d}",
                )
            )
            await asyncio.sleep(max(0.0, request_delay_s))

    return {
        "benchmark": {
            "kind": "controlled_mcp_value",
            "database": database_name,
            "privacy": "aggregate_and_anonymous_case_metrics_only",
            "job_case_limit": job_limit,
            "company_case_limit": company_limit,
        },
        "boss_job_detail": _summarize(job_cases),
        "company_research": _summarize(company_cases),
        "cases": {
            "boss_job_detail": job_cases,
            "company_research": company_cases,
        },
    }


async def _run_job_case(
    store: ApplyStore,
    agent: OpportunityAnalysisService,
    fit: JobFitAnalysisService,
    opportunity_id: str,
    *,
    label: str,
) -> dict[str, Any]:
    before = _state(store, opportunity_id)
    before_fit = await _safe_fit(fit, opportunity_id)
    try:
        result = await agent.analyze(
            opportunity_id,
            trigger={
                "type": "research",
                "focus": "job",
                "allow_external": True,
            },
        )
        error = None
    except Exception as exc:
        result = {}
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    observations = _observations(agent, result.get("run_id"))
    after_fit = await _safe_fit(fit, opportunity_id)
    after = _state(store, opportunity_id)
    expected = [item for item in observations if item["tool"] == "boss_fetch_job_detail"]
    tool_success = bool(
        expected
        and expected[-1]["status"] == "ok"
        and not expected[-1]["empty_result"]
    )
    return {
        "case": label,
        "expected_tool": "boss_fetch_job_detail",
        "accepted": bool(result.get("accepted")),
        "tool_success": tool_success,
        "error": error,
        "agent_errors": [str(value) for value in result.get("errors") or []],
        "observations": expected,
        "job_signal_delta": after["job_signal_count"] - before["job_signal_count"],
        "snapshot_delta": after["snapshot_count"] - before["snapshot_count"],
        "stage_changed": before["stage"] != after["stage"],
        "action_changed": before["next_action"] != after["next_action"],
        "fit_before": before_fit,
        "fit_after": after_fit,
    }


async def _run_company_case(
    store: ApplyStore,
    agent: OpportunityAnalysisService,
    opportunity_id: str,
    *,
    label: str,
) -> dict[str, Any]:
    before = _state(store, opportunity_id)
    try:
        result = await agent.analyze(
            opportunity_id,
            trigger={
                "type": "research",
                "focus": "company",
                "allow_external": True,
            },
        )
        error = None
    except Exception as exc:
        result = {}
        error = f"{type(exc).__name__}: {str(exc)[:240]}"
    observations = _observations(agent, result.get("run_id"))
    after = _state(store, opportunity_id)
    expected = [item for item in observations if item["tool"] == "research_company"]
    tool_success = bool(
        expected
        and expected[-1]["status"] == "ok"
        and not expected[-1]["empty_result"]
    )
    return {
        "case": label,
        "expected_tool": "research_company",
        "accepted": bool(result.get("accepted")),
        "tool_success": tool_success,
        "error": error,
        "agent_errors": [str(value) for value in result.get("errors") or []],
        "observations": expected,
        "job_signal_delta": 0,
        "snapshot_delta": 0,
        "stage_changed": before["stage"] != after["stage"],
        "action_changed": before["next_action"] != after["next_action"],
        "fit_before": None,
        "fit_after": None,
    }


def _select_job_cases(store: ApplyStore, limit: int) -> list[str]:
    builder = OpportunityBootstrapBuilder(store)
    candidates: list[tuple[int, str]] = []
    for opportunity in store.opportunities():
        opportunity_id = str(opportunity["id"])
        context = store.opportunity_context(opportunity_id)
        has_hr = any(
            bool(message.get("is_human_message", 1))
            and not bool(message.get("from_me"))
            for message in context.get("messages") or []
        )
        bootstrap = builder.build(opportunity_id, trigger={"type": "research"})
        if bootstrap.metadata.get("external_tools") != ["boss_fetch_job_detail"]:
            continue
        message_count = len(context.get("messages") or [])
        candidates.append((int(has_hr) * 10_000 + message_count, opportunity_id))
    candidates.sort(reverse=True)
    return [opportunity_id for _, opportunity_id in candidates[:limit]]


def _benchmark_account_id(database_url: str) -> str:
    store = ApplyStore(database_url=database_url)
    with store.connect() as db:
        row = db.execute(
            """
            SELECT account_id, COUNT(*) AS opportunity_count
            FROM opportunities
            GROUP BY account_id
            ORDER BY opportunity_count DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        raise RuntimeError("冷启动隔离库尚未生成机会，请先运行 capybot apply benchmark")
    return str(row["account_id"])


def _state(store: ApplyStore, opportunity_id: str) -> dict[str, Any]:
    context = store.opportunity_context(opportunity_id)
    opportunity = context.get("opportunity") or {}
    snapshots = context.get("job_snapshots") or []
    source = snapshots[0] if snapshots else (context.get("jobs") or [{}])[0]
    payload = _json_object(source.get("payload") or source.get("raw_payload"))
    raw_payload = _json_object(payload.get("raw_payload"))
    values = {
        field: source.get(field)
        or payload.get(field)
        or raw_payload.get(field)
        for field in JOB_FIELDS
    }
    return {
        "stage": opportunity.get("stage"),
        "next_action": opportunity.get("next_action"),
        "job_signal_count": sum(bool(str(value or "").strip()) for value in values.values()),
        "snapshot_count": len(snapshots),
    }


async def _safe_fit(
    service: JobFitAnalysisService,
    opportunity_id: str,
) -> dict[str, Any]:
    try:
        payload = await service.analyze(opportunity_id)
        result = payload.get("result") or {}
        return {
            "status": result.get("status"),
            "score": result.get("job_fit_score"),
            "confidence": result.get("confidence"),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "score": None,
            "confidence": None,
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def _observations(
    service: OpportunityAnalysisService,
    run_id: str | None,
) -> list[dict[str, Any]]:
    if not run_id:
        return []
    payload = service.runs.payload(str(run_id))
    return [
        {
            "tool": row.get("tool_name"),
            "status": row.get("status"),
            "duration_ms": int(row.get("duration_ms") or 0),
            "fact_count": int(row.get("fact_count") or 0),
            "novel_evidence_count": int(row.get("novel_evidence_count") or 0),
            "used_evidence_count": int(row.get("used_evidence_count") or 0),
            "empty_result": bool(row.get("empty_result")),
            "utility": row.get("utility"),
        }
        for row in payload.get("tool_observations") or []
    ]


def _summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    observations = [
        observation
        for case in cases
        for observation in case.get("observations") or []
    ]
    return {
        "cases": len(cases),
        "accepted": sum(bool(case.get("accepted")) for case in cases),
        "tool_successes": sum(bool(case.get("tool_success")) for case in cases),
        "tool_calls": len(observations),
        "empty_calls": sum(bool(item.get("empty_result")) for item in observations),
        "novel_evidence": sum(
            int(item.get("novel_evidence_count") or 0) for item in observations
        ),
        "used_evidence": sum(
            int(item.get("used_evidence_count") or 0) for item in observations
        ),
        "evidence_used_calls": sum(
            item.get("utility") == "evidence_used" for item in observations
        ),
        "duration_ms": sum(int(item.get("duration_ms") or 0) for item in observations),
        "job_signal_gain": sum(int(case.get("job_signal_delta") or 0) for case in cases),
        "stage_changes": sum(bool(case.get("stage_changed")) for case in cases),
        "action_changes": sum(bool(case.get("action_changed")) for case in cases),
        "fit_status_improved": sum(
            (case.get("fit_before") or {}).get("status") == "needs_review"
            and (case.get("fit_after") or {}).get("status") == "ok"
            for case in cases
        ),
        "failures": [
            {
                "case": case["case"],
                "error": case.get("error"),
                "agent_errors": case.get("agent_errors") or [],
            }
            for case in cases
            if case.get("error") or not case.get("accepted")
        ],
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    job = report["boss_job_detail"]
    company = report["company_research"]
    content = f"""# Capybot Apply 受控 MCP 价值实验

> 实验只在隔离数据库 `{report["benchmark"]["database"]}` 中运行。报告不包含公司名、岗位名、
> 聊天正文、简历、Cookie、Prompt 或 API Key。

## BOSS 岗位详情 MCP

- 样本：{job["cases"]}
- 工具成功：{job["tool_successes"]}/{job["cases"]}
- Agent 输出通过 CommitGate：{job["accepted"]}/{job["cases"]}
- 新增规范证据：{job["novel_evidence"]}
- 最终决策采用证据：{job["used_evidence"]}
- 新增岗位信息字段：{job["job_signal_gain"]}
- `needs_review -> ok` 评分改善：{job["fit_status_improved"]}
- 工具累计耗时：{job["duration_ms"] / 1000:.3f}s

## 公司公开信息 MCP

- 样本：{company["cases"]}
- 工具成功：{company["tool_successes"]}/{company["cases"]}
- 空结果：{company["empty_calls"]}
- 新增规范证据：{company["novel_evidence"]}
- 最终决策采用证据：{company["used_evidence"]}
- 工具累计耗时：{company["duration_ms"] / 1000:.3f}s

## 解释边界

- 该实验衡量工具是否真正取得并影响证据，不是招聘阶段准确率测试。
- 样本很小，只用于暴露空调用、重复上下文和证据未采用问题。
- BOSS 内部只读接口与公开网页均不承诺稳定 SLA。
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@contextmanager
def _database_environment(database_url: str) -> Iterator[None]:
    key = "CAPYBOT_APPLY_DATABASE_URL"
    previous = os.environ.get(key)
    os.environ[key] = database_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
