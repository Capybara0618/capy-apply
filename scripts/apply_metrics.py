"""Export privacy-safe performance, reliability, and cost metrics."""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from capybot.apply.agent_runs import AgentRunRepository
from capybot.apply.postgres import begin
from capybot.apply.store import ApplyStore


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def seconds(start: str | None, end: str | None) -> float | None:
    left, right = parse_dt(start), parse_dt(end)
    return max(0.0, (right - left).total_seconds()) if left and right else None


def run_seconds(row: dict[str, Any]) -> float | None:
    if row.get("duration_ms") is not None:
        return max(0.0, float(row["duration_ms"]) / 1000)
    if row.get("degraded_reason") == "stale_worker_run":
        return None
    return seconds(row.get("started_at"), row.get("finished_at"))


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * p
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] * (1 - (index - lower)) + ordered[upper] * (index - lower)


def _round(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def _load_json(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


def _rows(query: str) -> list[dict[str, Any]]:
    with begin() as db:
        return [dict(row) for row in db.execute(text(query)).mappings().all()]


def _scalar(query: str) -> int:
    with begin() as db:
        return int(db.execute(text(query)).scalar_one() or 0)


def _duration_summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "samples": len(values),
        "average_seconds": _round(statistics.mean(values) if values else None),
        "p50_seconds": _round(percentile(values, 0.5)),
        "p90_seconds": _round(percentile(values, 0.9)),
        "p95_seconds": _round(percentile(values, 0.95)),
    }


def collect_metrics(input_price: float, output_price: float) -> dict[str, Any]:
    import_runs = _rows("SELECT * FROM import_runs ORDER BY started_at")
    jobs = _rows("SELECT * FROM apply_jobs ORDER BY created_at")
    runs = _rows("SELECT * FROM agent_runs ORDER BY started_at")
    import_items = _rows("SELECT * FROM import_run_items")
    completed_runs = [run for run in runs if run.get("finished_at")]

    mode_counts: dict[str, int] = {}
    for item in import_items:
        mode = str(item.get("analysis_mode") or "unknown")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
    skipped_rate = mode_counts.get("skipped", 0) / len(import_items) if import_items else None

    jobs_by_type: dict[str, dict[str, Any]] = {}
    for job in jobs:
        job_type = str(job.get("job_type") or "unknown")
        bucket = jobs_by_type.setdefault(job_type, {"statuses": {}, "durations": []})
        status = str(job.get("status") or "unknown")
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
        duration = seconds(
            job.get("started_at") or job.get("created_at"),
            job.get("finished_at") or job.get("updated_at"),
        )
        if duration is not None:
            bucket["durations"].append(duration)

    engines: dict[str, dict[str, Any]] = {}
    for run in completed_runs:
        engine = str(run.get("engine") or "unknown").strip()
        bucket = engines.setdefault(
            engine,
            {
                "statuses": {},
                "durations": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "llm_calls": 0,
                "tool_calls": 0,
                "boss_tool_calls": 0,
            },
        )
        status = str(run.get("status") or "unknown")
        bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
        duration = run_seconds(run)
        if duration is not None:
            bucket["durations"].append(duration)
        bucket["prompt_tokens"] += int(run.get("prompt_tokens") or 0)
        bucket["completion_tokens"] += int(run.get("completion_tokens") or 0)
        bucket["llm_calls"] += int(run.get("llm_call_count") or 0)
        bucket["tool_calls"] += int(run.get("tool_call_count") or 0)
        bucket["boss_tool_calls"] += int(run.get("boss_tool_call_count") or 0)

    engine_metrics: dict[str, Any] = {}
    for engine, bucket in sorted(engines.items()):
        statuses = bucket["statuses"]
        total = sum(statuses.values())
        effective = statuses.get("ok", 0) + statuses.get("needs_review", 0)
        completed = effective + statuses.get("degraded", 0)
        prompt = bucket["prompt_tokens"]
        completion = bucket["completion_tokens"]
        engine_metrics[engine] = {
            "runs": total,
            "statuses": statuses,
            "effective_rate": _round(effective / total if total else None, 4),
            "completion_rate": _round(completed / total if total else None, 4),
            "duration": _duration_summary(bucket["durations"]),
            "llm_calls": bucket["llm_calls"],
            "average_llm_calls": _round(bucket["llm_calls"] / total if total else None),
            "tool_calls": bucket["tool_calls"],
            "average_tool_calls": _round(bucket["tool_calls"] / total if total else None),
            "boss_tool_calls": bucket["boss_tool_calls"],
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "estimated_cost_usd": _round(
                prompt / 1_000_000 * input_price + completion / 1_000_000 * output_price,
                6,
            ),
        }

    terminal_jobs = [job for job in jobs if job.get("status") in {"ok", "failed", "cancelled"}]
    successful_jobs = sum(job.get("status") == "ok" for job in terminal_jobs)
    latest_report = _load_json(import_runs[-1].get("report"), {}) if import_runs else {}
    current_coverage = _rows(
        """
        WITH latest_agent AS (
          SELECT DISTINCT ON (opportunity_id) opportunity_id, status
          FROM agent_runs
          WHERE opportunity_id IS NOT NULL AND engine='opportunity_agent_v2'
          ORDER BY opportunity_id, started_at DESC
        ), latest_fit AS (
          SELECT DISTINCT ON (opportunity_id) opportunity_id, status
          FROM opportunity_fit_results
          ORDER BY opportunity_id, updated_at DESC
        )
        SELECT
          COUNT(*) AS opportunities,
          COUNT(la.opportunity_id) AS agent_covered,
          COUNT(*) FILTER (WHERE la.status IN ('ok', 'needs_review')) AS agent_effective,
          COUNT(*) FILTER (WHERE la.status IN ('failed', 'degraded')) AS agent_failed,
          COUNT(lf.opportunity_id) AS fit_covered,
          COUNT(*) FILTER (WHERE lf.status IN ('ok', 'needs_review', 'no_profile')) AS fit_effective,
          COUNT(*) FILTER (WHERE lf.status = 'failed') AS fit_failed
        FROM opportunities o
        LEFT JOIN latest_agent la ON la.opportunity_id=o.id
        LEFT JOIN latest_fit lf ON lf.opportunity_id=o.id
        """
    )[0]
    stage_counts = {
        str(row["stage"]): int(row["count"])
        for row in _rows("SELECT stage, COUNT(*) AS count FROM opportunities GROUP BY stage")
    }
    suggestion_counts = {
        f"{row['kind']}:{row['status']}": int(row["count"])
        for row in _rows(
            "SELECT kind, status, COUNT(*) AS count FROM suggestions GROUP BY kind, status"
        )
    }
    fit_status_counts = {
        str(row["fit_status"] or "unknown"): int(row["count"])
        for row in _rows(
            "SELECT fit_status, COUNT(*) AS count FROM opportunities GROUP BY fit_status"
        )
    }
    tool_utility = AgentRunRepository(ApplyStore()).metrics().get("tool_utility") or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": "仅包含聚合指标，不含聊天、简历、Cookie、Prompt 或 API Key。",
        "cost_assumption": {
            "input_usd_per_million_tokens": input_price,
            "output_usd_per_million_tokens": output_price,
            "note": "仅为可配置估算；中转站实际计费以服务商账单为准。",
        },
        "data_scale": {
            "conversations": _scalar("SELECT COUNT(*) FROM boss_conversations"),
            "messages": _scalar("SELECT COUNT(*) FROM boss_messages"),
            "opportunities": _scalar("SELECT COUNT(*) FROM opportunities"),
            "agent_runs": len(runs),
            "trace_steps": _scalar("SELECT COUNT(*) FROM agent_trace_steps"),
            "l2_events": _scalar("SELECT COUNT(*) FROM apply_events"),
            "tasks": _scalar("SELECT COUNT(*) FROM suggestions WHERE kind='task'"),
            "drafts": _scalar("SELECT COUNT(*) FROM suggestions WHERE kind='draft'"),
            "fit_evaluations": _scalar("SELECT COUNT(*) FROM opportunity_fit_results"),
            "current_coverage": current_coverage,
            "stage_counts": stage_counts,
            "suggestion_counts": suggestion_counts,
            "fit_status_counts": fit_status_counts,
        },
        "imports": {
            "runs": len(import_runs),
            "duration": _duration_summary(
                [
                    duration
                    for row in import_runs
                    if (duration := seconds(row.get("started_at"), row.get("finished_at")))
                    is not None
                ]
            ),
            "latest_report": latest_report,
            "items": len(import_items),
            "mode_counts": mode_counts,
            "skipped_rate": _round(skipped_rate, 4),
        },
        "jobs": {
            "total": len(jobs),
            "terminal": len(terminal_jobs),
            "success_rate": _round(
                successful_jobs / len(terminal_jobs) if terminal_jobs else None, 4
            ),
            "historical_failed": sum(job.get("status") == "failed" for job in terminal_jobs),
            "by_type": {
                name: {
                    "statuses": value["statuses"],
                    "duration": _duration_summary(value["durations"]),
                }
                for name, value in sorted(jobs_by_type.items())
            },
        },
        "agents": {
            "completed_runs": len(completed_runs),
            "duration": _duration_summary(
                [duration for row in completed_runs if (duration := run_seconds(row)) is not None]
            ),
            "by_engine": engine_metrics,
            "total_estimated_cost_usd": _round(
                sum(float(value["estimated_cost_usd"] or 0) for value in engine_metrics.values()), 6
            ),
            "tool_utility": tool_utility,
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    scale = payload["data_scale"]
    imports = payload["imports"]
    jobs = payload["jobs"]
    agents = payload["agents"]
    latest = imports["latest_report"]
    lines = [
        "# Capybot Apply 性能与成本报告",
        "",
        f"生成时间：`{payload['generated_at']}`",
        "",
        f"> {payload['privacy']}",
        "",
        "## 数据规模",
        "",
        f"- {scale['conversations']} 个会话，{scale['messages']} 条原始消息，{scale['opportunities']} 个机会。",
        f"- {scale['agent_runs']} 次 Agent run，{scale['trace_steps']} 条 trace step，{scale['fit_evaluations']} 次岗位评分。",
        f"- {scale['l2_events']} 条 L2 事件，{scale['tasks']} 个任务，{scale['drafts']} 条草稿。",
        (
            f"- 当前 Agent 覆盖 {scale['current_coverage']['agent_covered']}/"
            f"{scale['current_coverage']['opportunities']}，有效 {scale['current_coverage']['agent_effective']}，"
            f"最新失败 {scale['current_coverage']['agent_failed']}；岗位评分覆盖 "
            f"{scale['current_coverage']['fit_covered']}/{scale['current_coverage']['opportunities']}，"
            f"最新失败 {scale['current_coverage']['fit_failed']}。"
        ),
        f"- 当前阶段分布 `{scale['stage_counts']}`；建议分布 `{scale['suggestion_counts']}`。",
        f"- 当前岗位评分状态 `{scale['fit_status_counts']}`。",
        "",
        "## 增量导入",
        "",
        f"- {imports['runs']} 个导入批次，{imports['items']} 个 import item，模式分布 `{imports['mode_counts']}`。",
        f"- 无变化跳过率：{float(imports['skipped_rate'] or 0) * 100:.2f}%。",
        f"- 最近一次：扫描 {latest.get('scanned_conversations', 'N/A')}，成功 {latest.get('successful_conversations', 'N/A')}，失败 {latest.get('failed_conversations', 'N/A')}，新增消息 {latest.get('new_messages', 'N/A')}。",
        f"- 导入耗时：P50 {imports['duration']['p50_seconds']} 秒，P90 {imports['duration']['p90_seconds']} 秒。",
        "",
        "## 后台可靠性",
        "",
        f"- 累计 {jobs['total']} 个任务；终态任务成功率 {float(jobs['success_rate'] or 0) * 100:.2f}%。",
        f"- 历史失败任务 {jobs['historical_failed']} 个，保留用于故障审计，不与当前 Agent 有效结果率混算。",
        "",
        "## Agent 分引擎指标",
        "",
        "| 引擎 | Runs | 有效率 | 完成率 | P50(s) | P90(s) | P95(s) | LLM | Tools | Tokens(in/out) | 估算成本(USD) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, metric in agents["by_engine"].items():
        duration = metric["duration"]
        lines.append(
            f"| {name} | {metric['runs']} | {float(metric['effective_rate'] or 0) * 100:.2f}% | "
            f"{float(metric['completion_rate'] or 0) * 100:.2f}% | {duration['p50_seconds']} | "
            f"{duration['p90_seconds']} | {duration['p95_seconds']} | {metric['llm_calls']} | "
            f"{metric['tool_calls']} | {metric['prompt_tokens']}/{metric['completion_tokens']} | "
            f"{metric['estimated_cost_usd']} |"
        )
    cost = payload["cost_assumption"]
    utility = agents.get("tool_utility") or {}
    lines.extend(
        [
            "",
            f"估算采用输入 `${cost['input_usd_per_million_tokens']}`/百万 token、输出 `${cost['output_usd_per_million_tokens']}`/百万 token；{cost['note']}",
            "",
            "## Tool Calling 价值审计",
            "",
            (
                f"- 新版效用埋点覆盖 {utility.get('measured_calls', 0)} 次工具 Observation，"
                f"其中 {utility.get('useful_calls', 0)} 次被判定为有效，"
                f"有效率 {float(utility.get('useful_rate') or 0) * 100:.2f}%。"
            ),
            (
                f"- 工具共返回 {utility.get('novel_evidence', 0)} 条新增证据，"
                f"最终决策采用 {utility.get('used_evidence', 0)} 条；空结果和未采用证据同样保留。"
            ),
            "",
            "## 解释边界",
            "",
            "- 历史数据包含优化前运行，因此总体延迟不等同于当前版本冷启动延迟。",
            "- `needs_review` 表示 Agent 完成分析但要求人工确认；`degraded` 表示工具或模型失败后保留了安全降级结果。",
            "- 成本只按已记录 token 估算，不包含中转站倍率、缓存折扣、OCR 与本地基础设施成本。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--input-price-per-million", type=float, default=0.15)
    parser.add_argument("--output-price-per-million", type=float, default=0.60)
    args = parser.parse_args()
    payload = collect_metrics(args.input_price_per_million, args.output_price_per_million)
    markdown = render_markdown(payload)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
