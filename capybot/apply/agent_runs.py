"""Persistence and metrics for Agent runs, traces, and MCP observations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .models import parse_utc_datetime, utc_now_iso

if TYPE_CHECKING:
    from .store import ApplyStore


class AgentRunRepository:
    """Own the complete lifecycle of observable Agent executions."""

    def __init__(self, store: ApplyStore | None = None) -> None:
        if store is None:
            from .store import ApplyStore

            store = ApplyStore()
        self.store = store

    def create(
        self,
        target_type: str,
        target_id: str,
        *,
        conversation_id: str | None = None,
        opportunity_id: str | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        input_summary: str | None = None,
        engine: str | None = None,
        planner_mode: str | None = None,
    ) -> str:
        now = utc_now_iso()
        run_id = self.store._id("agentrun", target_type, target_id, now)
        with self.store.connect() as db:
            account_id = None
            if opportunity_id:
                row = db.execute(
                    "SELECT account_id FROM opportunities WHERE id=?",
                    (opportunity_id,),
                ).fetchone()
                account_id = row["account_id"] if row else None
            if not account_id and conversation_id:
                row = db.execute(
                    "SELECT account_id FROM boss_conversations WHERE id=?",
                    (conversation_id,),
                ).fetchone()
                account_id = row["account_id"] if row else None
            account_id = account_id or (self.store.current_account() or {}).get("id")
            db.execute(
                """
                INSERT INTO agent_runs
                (id, account_id, target_type, target_id, conversation_id, opportunity_id,
                 started_at, finished_at, status, model_provider, model_name, input_summary,
                 output_summary, confidence, error, created_at, engine, planner_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'running', ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    account_id,
                    target_type,
                    target_id,
                    conversation_id,
                    opportunity_id,
                    now,
                    model_provider,
                    model_name,
                    input_summary,
                    now,
                    engine,
                    planner_mode,
                ),
            )
        return run_id

    def add_trace(
        self,
        run_id: str,
        step_index: int,
        step_type: str,
        title: str,
        *,
        summary: str | None = None,
        input_ref: str | None = None,
        output_ref: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now_iso()
        safe_metadata = self.store._sanitize_trace_metadata(metadata or {})
        safe_summary = self.store._sanitize_trace_summary(summary)
        with self.store.connect() as db:
            db.execute(
                """
                INSERT INTO agent_trace_steps
                (id, run_id, step_index, step_type, title, summary, input_ref, output_ref,
                 metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.store._id("trace", run_id, step_index, title),
                    run_id,
                    step_index,
                    step_type,
                    title,
                    safe_summary,
                    input_ref,
                    output_ref,
                    self.store._json(safe_metadata),
                    now,
                ),
            )

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        output_summary: str | None = None,
        confidence: float | None = None,
        error: str | None = None,
        planner_mode: str | None = None,
        degraded_reason: str | None = None,
        tool_call_count: int | None = None,
        boss_tool_call_count: int | None = None,
        llm_call_count: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        finished_at = utc_now_iso()
        with self.store.connect() as db:
            row = db.execute("SELECT started_at FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            started_at = parse_utc_datetime(row["started_at"]) if row else None
            finished_dt = parse_utc_datetime(finished_at)
            duration_ms = (
                int((finished_dt - started_at).total_seconds() * 1000)
                if started_at and finished_dt
                else None
            )
            db.execute(
                """
                UPDATE agent_runs
                SET finished_at=?, status=?, output_summary=?, confidence=?, error=?,
                    planner_mode=COALESCE(?, planner_mode),
                    degraded_reason=COALESCE(?, degraded_reason),
                    tool_call_count=COALESCE(?, tool_call_count),
                    boss_tool_call_count=COALESCE(?, boss_tool_call_count),
                    llm_call_count=COALESCE(?, llm_call_count),
                    prompt_tokens=COALESCE(?, prompt_tokens),
                    completion_tokens=COALESCE(?, completion_tokens),
                    duration_ms=COALESCE(?, duration_ms)
                WHERE id=?
                """,
                (
                    finished_at,
                    status,
                    output_summary,
                    confidence,
                    error,
                    planner_mode,
                    degraded_reason,
                    tool_call_count,
                    boss_tool_call_count,
                    llm_call_count,
                    prompt_tokens,
                    completion_tokens,
                    duration_ms,
                    run_id,
                ),
            )

    def save_observation(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        tool_name: str,
        server_name: str,
        status: str,
        arguments_summary: dict[str, Any],
        result_summary: str,
        evidence_refs: list[str],
        duration_ms: int | None = None,
        fact_count: int = 0,
        novel_evidence_count: int = 0,
        empty_result: bool = False,
    ) -> None:
        now = utc_now_iso()
        observation_id = self.store._id("tool-observation", run_id, tool_call_id)
        with self.store.connect() as db:
            db.execute(
                """
                INSERT INTO tool_observations
                (id, run_id, tool_call_id, tool_name, server_name, status,
                 arguments_summary, result_summary, evidence_refs, duration_ms,
                 fact_count, novel_evidence_count, empty_result, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, tool_call_id) DO UPDATE SET
                  status=excluded.status,
                  result_summary=excluded.result_summary,
                  evidence_refs=excluded.evidence_refs,
                  duration_ms=excluded.duration_ms,
                  fact_count=excluded.fact_count,
                  novel_evidence_count=excluded.novel_evidence_count,
                  empty_result=excluded.empty_result
                """,
                (
                    observation_id,
                    run_id,
                    tool_call_id,
                    tool_name,
                    server_name,
                    status,
                    self.store._json(self.store._sanitize_trace_metadata(arguments_summary)),
                    self.store._sanitize_trace_summary(result_summary),
                    self.store._json(evidence_refs),
                    duration_ms,
                    fact_count,
                    novel_evidence_count,
                    empty_result,
                    now,
                ),
            )

    def save_observation_utility(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        used_evidence_count: int,
        utility: str,
    ) -> None:
        with self.store.connect() as db:
            db.execute(
                """
                UPDATE tool_observations
                SET used_evidence_count=?, utility=?
                WHERE run_id=? AND tool_call_id=?
                """,
                (used_evidence_count, utility, run_id, tool_call_id),
            )

    def fail_stale(self, max_age_minutes: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
        with self.store.connect() as db:
            result = db.execute(
                """
                UPDATE agent_runs
                SET status='failed', finished_at=?,
                    error='Agent Worker 中断，运行未正常结束。',
                    degraded_reason='stale_worker_run'
                WHERE status='running' AND started_at < ?
                """,
                (utc_now_iso(), cutoff),
            )
        return int(result.rowcount or 0)

    def payload(self, run_id: str | None = None) -> dict[str, Any]:
        account_id = self.store.current_account_id()
        with self.store.connect() as db:
            if run_id:
                run = db.execute(
                    "SELECT * FROM agent_runs WHERE id=? AND account_id=?",
                    (run_id, account_id),
                ).fetchone()
                if not run:
                    return {"run": None, "steps": []}
                steps = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM agent_trace_steps WHERE run_id=? ORDER BY step_index",
                        (run_id,),
                    ).fetchall()
                ]
                for step in steps:
                    step["metadata"] = self.store._loads(step.get("metadata"), {})
                observations = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM tool_observations WHERE run_id=? ORDER BY created_at",
                        (run_id,),
                    ).fetchall()
                ]
                for observation in observations:
                    observation["arguments_summary"] = self.store._loads(
                        observation.get("arguments_summary"),
                        {},
                    )
                    observation["evidence_refs"] = self.store._loads(
                        observation.get("evidence_refs"),
                        [],
                    )
                return {
                    "run": dict(run),
                    "steps": steps,
                    "tool_observations": observations,
                }
            runs = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT a.*, o.title AS opportunity_title, o.company AS opportunity_company,
                           o.stage AS opportunity_stage
                    FROM agent_runs a
                    LEFT JOIN opportunities o ON o.id=a.opportunity_id
                    WHERE a.account_id=?
                    ORDER BY a.started_at DESC
                    LIMIT 100
                    """,
                    (account_id,),
                ).fetchall()
            ]
        return {"runs": runs, "metrics": self.metrics()}

    def metrics(self, limit: int = 2000) -> dict[str, Any]:
        account_id = self.store.current_account_id()
        with self.store.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT status, engine, duration_ms, llm_call_count, tool_call_count,
                           boss_tool_call_count, prompt_tokens, completion_tokens
                    FROM agent_runs
                    WHERE account_id=?
                    ORDER BY started_at DESC
                    LIMIT ?
                    """,
                    (account_id, limit),
                ).fetchall()
            ]
            utility_rows = [
                dict(row)
                for row in db.execute(
                    """
                    SELECT t.utility, COUNT(*) AS count,
                           COALESCE(SUM(t.novel_evidence_count), 0) AS novel_evidence,
                           COALESCE(SUM(t.used_evidence_count), 0) AS used_evidence
                    FROM tool_observations t
                    JOIN agent_runs a ON a.id=t.run_id
                    WHERE a.account_id=?
                    GROUP BY t.utility
                    """,
                    (account_id,),
                ).fetchall()
            ]
        by_engine = {
            engine: self._aggregate(
                [row for row in rows if str(row.get("engine") or "unknown") == engine]
            )
            for engine in sorted({str(row.get("engine") or "unknown") for row in rows})
        }
        metrics = self._aggregate(rows)
        metrics.update(
            {
                "by_engine": by_engine,
                "engines": sorted({str(row.get("engine")) for row in rows}),
                "tool_utility": self._tool_utility(utility_rows),
            }
        )
        return metrics

    @staticmethod
    def _tool_utility(rows: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {
            str(row.get("utility") or "unknown"): int(row.get("count") or 0)
            for row in rows
        }
        measured = sum(
            count for utility, count in counts.items() if utility != "unknown"
        )
        useful = (
            counts.get("evidence_used", 0)
            + counts.get("rule_context", 0)
            + counts.get("routing_context", 0)
        )
        return {
            "counts": counts,
            "measured_calls": measured,
            "useful_calls": useful,
            "useful_rate": round(useful / measured, 4) if measured else None,
            "novel_evidence": sum(
                int(row.get("novel_evidence") or 0) for row in rows
            ),
            "used_evidence": sum(int(row.get("used_evidence") or 0) for row in rows),
        }

    @classmethod
    def _aggregate(cls, rows: list[dict[str, Any]]) -> dict[str, Any]:
        durations = sorted(
            int(row["duration_ms"])
            for row in rows
            if row.get("duration_ms") is not None and int(row["duration_ms"]) >= 0
        )
        status_counts: dict[str, int] = {}
        engine_counts: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            engine = str(row.get("engine") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            engine_counts[engine] = engine_counts.get(engine, 0) + 1
        terminal_count = sum(
            count for status, count in status_counts.items() if status != "running"
        )
        successful = sum(status_counts.get(status, 0) for status in ("ok", "needs_review"))
        completed = successful + status_counts.get("degraded", 0)
        return {
            "sample_size": len(rows),
            "measured_duration_runs": len(durations),
            "success_rate": round(successful / terminal_count, 4) if terminal_count else None,
            "completion_rate": round(completed / terminal_count, 4) if terminal_count else None,
            "duration_ms": {
                "average": round(sum(durations) / len(durations)) if durations else None,
                "p50": cls._percentile(durations, 0.50),
                "p95": cls._percentile(durations, 0.95),
            },
            "llm_calls": sum(int(row.get("llm_call_count") or 0) for row in rows),
            "tool_calls": sum(int(row.get("tool_call_count") or 0) for row in rows),
            "boss_tool_calls": sum(int(row.get("boss_tool_call_count") or 0) for row in rows),
            "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in rows),
            "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in rows),
            "status_counts": status_counts,
            "engine_counts": engine_counts,
        }

    @staticmethod
    def _percentile(values: list[int], quantile: float) -> int | None:
        if not values:
            return None
        index = round((len(values) - 1) * quantile)
        return values[max(0, min(index, len(values) - 1))]
