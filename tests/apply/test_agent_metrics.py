from capybot.apply.agent_runs import AgentRunRepository
from capybot.apply.store import ApplyStore
from capybot.evaluation.cold_start_benchmark import _tool_utility_summary


def test_agent_metrics_aggregate_persisted_runtime_data():
    store = ApplyStore()
    runs = AgentRunRepository(store)
    run_id = runs.create(
        "heartbeat",
        "metrics-test",
        engine="opportunity_agent_v2",
        planner_mode="single_llm_delta",
    )
    runs.finish(
        run_id,
        status="ok",
        llm_call_count=1,
        tool_call_count=2,
        boss_tool_call_count=0,
        prompt_tokens=120,
        completion_tokens=40,
    )

    metrics = runs.metrics()

    assert metrics["sample_size"] == 1
    assert metrics["measured_duration_runs"] == 1
    assert metrics["success_rate"] == 1.0
    assert metrics["llm_calls"] == 1
    assert metrics["tool_calls"] == 2
    assert metrics["prompt_tokens"] == 120
    assert metrics["completion_tokens"] == 40
    assert metrics["engine_counts"] == {"opportunity_agent_v2": 1}


def test_stale_agent_run_is_reconciled_without_polluting_duration_metrics():
    store = ApplyStore()
    runs = AgentRunRepository(store)
    run_id = runs.create("heartbeat", "stale-run", engine="opportunity_agent_v2")
    with store.connect() as db:
        db.execute(
            "UPDATE agent_runs SET started_at=? WHERE id=?",
            ("2020-01-01T00:00:00+00:00", run_id),
        )

    assert runs.fail_stale(max_age_minutes=30) == 1

    payload = runs.payload(run_id)
    assert payload["run"]["status"] == "failed"
    assert payload["run"]["degraded_reason"] == "stale_worker_run"
    assert payload["run"]["duration_ms"] is None


def test_agent_metrics_distinguish_degraded_completion_from_success():
    store = ApplyStore()
    runs = AgentRunRepository(store)
    for index, status in enumerate(("ok", "degraded", "failed")):
        run_id = runs.create("heartbeat", f"rate-{index}")
        runs.finish(run_id, status=status)

    metrics = runs.metrics()

    assert metrics["success_rate"] == 0.3333
    assert metrics["completion_rate"] == 0.6667


def test_agent_metrics_group_runs_by_engine():
    store = ApplyStore()
    runs = AgentRunRepository(store)
    run_id = runs.create(
        "opportunity",
        "current",
        engine="opportunity_agent_v2",
    )
    runs.finish(run_id, status="ok", tool_call_count=2)

    metrics = runs.metrics()

    assert metrics["sample_size"] == 1
    assert metrics["by_engine"]["opportunity_agent_v2"]["success_rate"] == 1.0
    assert metrics["by_engine"]["opportunity_agent_v2"]["tool_calls"] == 2
    assert metrics["engines"] == ["opportunity_agent_v2"]


def test_tool_observation_persists_measured_utility():
    store = ApplyStore()
    runs = AgentRunRepository(store)
    run_id = runs.create("heartbeat", "tool-utility")
    runs.save_observation(
        run_id,
        tool_call_id="call-1",
        tool_name="memory_read",
        server_name="local",
        status="ok",
        arguments_summary={"layer": "l1"},
        result_summary="读取到 2 条历史消息。",
        evidence_refs=["boss_message:1", "boss_message:2"],
        duration_ms=12,
        fact_count=2,
        novel_evidence_count=1,
        empty_result=False,
    )
    runs.save_observation_utility(
        run_id,
        tool_call_id="call-1",
        used_evidence_count=1,
        utility="evidence_used",
    )

    observation = runs.payload(run_id)["tool_observations"][0]

    assert observation["fact_count"] == 2
    assert observation["novel_evidence_count"] == 1
    assert observation["used_evidence_count"] == 1
    assert observation["empty_result"] is False
    assert observation["utility"] == "evidence_used"


def test_tool_utility_summary_separates_prevented_duplicates():
    store = ApplyStore()
    runs = AgentRunRepository(store)
    run_id = runs.create("heartbeat", "tool-duplicates")
    for call_id, status in (("executed", "ok"), ("duplicate", "duplicate")):
        runs.save_observation(
            run_id,
            tool_call_id=call_id,
            tool_name="memory_read",
            server_name="memory",
            status=status,
            arguments_summary={"layer": "l1"},
            result_summary="done",
            evidence_refs=["boss_message:1"] if status == "ok" else [],
            fact_count=1 if status == "ok" else 0,
            novel_evidence_count=1 if status == "ok" else 0,
        )
    runs.save_observation_utility(
        run_id,
        tool_call_id="executed",
        used_evidence_count=1,
        utility="evidence_used",
    )

    summary = _tool_utility_summary(store)

    assert summary["requested_calls"] == 2
    assert summary["calls"] == 1
    assert summary["duplicate_prevented"] == 1
    assert summary["empty_calls"] == 0
    assert summary["by_tool"] == {"memory_read": 1}
