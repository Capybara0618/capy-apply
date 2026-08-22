"""Export a privacy-safe portfolio evidence snapshot for Capybot Apply."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from capybot.apply.agent_runs import AgentRunRepository
from capybot.apply.postgres import begin
from capybot.apply.store import ApplyStore


def _scalar(query: str) -> int:
    with begin() as db:
        return int(db.execute(text(query)).scalar_one() or 0)


def _mode_counts() -> dict[str, int]:
    with begin() as db:
        rows = db.execute(
            text(
                "SELECT analysis_mode, COUNT(*) AS count FROM import_run_items GROUP BY analysis_mode"
            )
        ).mappings()
        return {str(row["analysis_mode"] or "unknown"): int(row["count"]) for row in rows}


def _fit_status_counts() -> dict[str, int]:
    with begin() as db:
        rows = db.execute(
            text("SELECT fit_status, COUNT(*) AS count FROM opportunities GROUP BY fit_status")
        ).mappings()
        return {str(row["fit_status"] or "unknown"): int(row["count"]) for row in rows}


def _latest_live_eval() -> dict[str, Any] | None:
    candidates = sorted(
        Path(".artifacts/evaluation").glob("opportunity_agent_live_eval*.json"),
        reverse=True,
    )
    candidates.extend(
        sorted(
            (Path.home() / ".capybot" / "apply" / "evals").glob("live_eval_*.json"), reverse=True
        )
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        return {
            "report_file": path.name,
            "cases": payload.get("cases"),
            "accepted_rate": payload.get("accepted_rate"),
            "stage_accuracy": payload.get("stage_accuracy"),
            "action_accuracy": payload.get("action_accuracy"),
        }
    return None


def _public_eval_summary(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "report_file": path.name,
        "passed": payload.get("passed"),
        "total": payload.get("total"),
        "metrics": payload.get("metrics") or {},
    }


def collect_evidence() -> dict[str, Any]:
    store = ApplyStore()
    agent_payload = AgentRunRepository(store).payload()
    metrics = agent_payload.get("metrics") or {}
    runs = agent_payload.get("runs") or []
    current_runs = [run for run in runs if run.get("engine") == "opportunity_agent_v2"]
    fit_runs = [run for run in runs if run.get("engine") == "job_fit_evaluator_v2"]
    latest_current = current_runs[0] if current_runs else None
    latest_fit = fit_runs[0] if fit_runs else None
    modes = _mode_counts()
    import_items = sum(modes.values())
    skipped = modes.get("skipped", 0)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "privacy": "仅含聚合统计，不含聊天正文、简历、Cookie、Prompt 或 API Key。",
        "data_scale": {
            "conversations": _scalar("SELECT COUNT(*) FROM boss_conversations"),
            "messages": _scalar("SELECT COUNT(*) FROM boss_messages"),
            "opportunities": _scalar("SELECT COUNT(*) FROM opportunities"),
            "l2_events": _scalar("SELECT COUNT(*) FROM apply_events"),
            "tasks": _scalar("SELECT COUNT(*) FROM suggestions WHERE kind='task'"),
            "drafts": _scalar("SELECT COUNT(*) FROM suggestions WHERE kind='draft'"),
            "fit_results": _scalar(
                "SELECT COUNT(DISTINCT opportunity_id) FROM opportunity_fit_results"
            ),
            "fit_evaluations": _scalar("SELECT COUNT(*) FROM opportunity_fit_results"),
            "fit_status_counts": _fit_status_counts(),
            "agent_runs": _scalar("SELECT COUNT(*) FROM agent_runs"),
            "trace_steps": _scalar("SELECT COUNT(*) FROM agent_trace_steps"),
        },
        "incremental_routing": {
            "items": import_items,
            "modes": modes,
            "skipped_rate": round(skipped / import_items, 4) if import_items else None,
        },
        "agent_metrics": metrics,
        "latest_current_run": {
            key: latest_current.get(key)
            for key in (
                "id",
                "engine",
                "status",
                "planner_mode",
                "llm_call_count",
                "tool_call_count",
                "boss_tool_call_count",
                "prompt_tokens",
                "completion_tokens",
                "duration_ms",
            )
        }
        if latest_current
        else None,
        "latest_fit_run": {
            key: latest_fit.get(key)
            for key in (
                "id",
                "engine",
                "status",
                "llm_call_count",
                "prompt_tokens",
                "completion_tokens",
                "duration_ms",
            )
        }
        if latest_fit
        else None,
        "offline_eval": _public_eval_summary(
            Path(".artifacts/evaluation/opportunity_agent_offline_eval.json")
        ),
        "live_eval": _latest_live_eval(),
    }


def to_markdown(payload: dict[str, Any]) -> str:
    scale = payload["data_scale"]
    routing = payload["incremental_routing"]
    live = payload.get("live_eval") or {}
    offline = payload.get("offline_eval") or {}
    latest = payload.get("latest_current_run") or {}
    latest_fit = payload.get("latest_fit_run") or {}
    skipped = routing.get("skipped_rate")
    lines = [
        "# Capybot Apply 脱敏验证快照",
        "",
        f"生成时间：`{payload['generated_at']}`",
        "",
        f"> {payload['privacy']}",
        "",
        "## 真实数据规模",
        "",
        f"- {scale['conversations']} 个招聘会话，{scale['messages']} 条原始消息，{scale['opportunities']} 个岗位机会。",
        f"- {scale['agent_runs']} 次 Agent run，{scale['trace_steps']} 条 trace step。",
        f"- {scale['l2_events']} 条 L2 事件，{scale['tasks']} 个任务，{scale['drafts']} 条回复草稿，{scale['fit_results']} 个机会已有评分。",
        f"- 累计执行 {scale['fit_evaluations']} 次岗位评分，保留历史版本用于追踪简历或岗位上下文变化。",
        f"- 当前岗位评分状态：`{scale['fit_status_counts']}`。",
        "",
        "## 增量路由",
        "",
        f"- 共 {routing['items']} 个 import item，模式分布：`{routing['modes']}`。",
        f"- 无变化跳过率：{skipped * 100:.2f}%。" if skipped is not None else "- 暂无增量样本。",
        "",
        "## 最新 Agent 运行",
        "",
        f"- 引擎 `{latest.get('engine')}`，状态 `{latest.get('status')}`，耗时 {latest.get('duration_ms')} ms。",
        f"- LLM {latest.get('llm_call_count')} 次，Tool Call {latest.get('tool_call_count')} 次，Prompt/Completion tokens 为 {latest.get('prompt_tokens')}/{latest.get('completion_tokens')}。",
        (
            f"- 新版工具效用埋点：{payload['agent_metrics'].get('tool_utility', {}).get('measured_calls', 0)} 次已测 Observation，"
            f"{payload['agent_metrics'].get('tool_utility', {}).get('useful_calls', 0)} 次有效。"
        ),
        "",
        "## Live Eval",
        "",
        f"- {live.get('cases')} 个中文场景，Structured Output 接受率 {float(live.get('accepted_rate') or 0) * 100:.2f}%。",
        f"- 阶段准确率 {float(live.get('stage_accuracy') or 0) * 100:.2f}%，下一步行动准确率 {float(live.get('action_accuracy') or 0) * 100:.2f}%。",
        "",
        "## 离线契约与岗位评分",
        "",
        f"- 路由与安全契约：{offline.get('passed')}/{offline.get('total')} 通过。",
        f"- 最新岗位评分引擎 `{latest_fit.get('engine')}`，状态 `{latest_fit.get('status')}`，耗时 {latest_fit.get('duration_ms')} ms，LLM {latest_fit.get('llm_call_count')} 次。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts/evidence"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = collect_evidence()
    (args.output_dir / "evidence_snapshot.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output_dir / "EVIDENCE_SNAPSHOT.md").write_text(to_markdown(payload), encoding="utf-8")
    print(args.output_dir.resolve())


if __name__ == "__main__":
    main()
