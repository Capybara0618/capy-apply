from capybot.evaluation.dynamic_tracking_benchmark import (
    aggregate_runs,
    comparison,
    final_quality,
    run_dynamic_benchmark,
    speaker_turn_batches,
)


def test_dynamic_benchmark_accepts_explicit_source_account() -> None:
    import inspect

    assert "source_account_id" in inspect.signature(run_dynamic_benchmark).parameters


def test_speaker_turn_batches_keep_platform_rows_with_neighboring_turn() -> None:
    rows = [
        {
            "conversation_id": "conv-1",
            "message_id": "job",
            "is_human_message": False,
            "from_me": False,
        },
        {
            "conversation_id": "conv-1",
            "message_id": "me-1",
            "is_human_message": True,
            "from_me": True,
        },
        {
            "conversation_id": "conv-1",
            "message_id": "card",
            "is_human_message": False,
            "from_me": False,
        },
        {
            "conversation_id": "conv-1",
            "message_id": "me-2",
            "is_human_message": True,
            "from_me": True,
        },
        {
            "conversation_id": "conv-1",
            "message_id": "hr-1",
            "is_human_message": True,
            "from_me": False,
        },
    ]

    batches = speaker_turn_batches(rows)

    assert [batch.speaker for batch in batches] == ["me", "hr"]
    assert batches[0].human_message_ids == ("me-1", "me-2")
    assert [row["message_id"] for row in batches[0].rows] == [
        "job",
        "me-1",
        "card",
        "me-2",
    ]
    assert batches[1].human_message_ids == ("hr-1",)


def test_dynamic_aggregation_and_final_quality() -> None:
    agent = [
        {
            "accepted": True,
            "metrics": {
                "llm_call_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "duration_ms": 0,
            },
        },
        {
            "accepted": True,
            "decision": {"stage": "need_my_action", "next": {"action": "reply"}},
            "metrics": {
                "llm_call_count": 1,
                "tool_call_count": 2,
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "duration_ms": 500,
            },
        },
    ]
    baseline = [
        {
            "accepted": True,
            "metrics": {
                "llm_call_count": 1,
                "prompt_tokens": 150,
                "completion_tokens": 20,
                "duration_ms": 700,
            },
        },
        {
            "accepted": True,
            "decision": {"stage": "communicating", "next": {"action": "reply"}},
            "metrics": {
                "llm_call_count": 1,
                "prompt_tokens": 250,
                "completion_tokens": 20,
                "duration_ms": 900,
            },
        },
    ]

    agent_summary = aggregate_runs(agent)
    baseline_summary = aggregate_runs(baseline)

    assert agent_summary["zero_llm_runs"] == 1
    assert agent_summary["tokens"]["total"] == 120
    assert comparison(agent_summary, baseline_summary)["token_reduction"] == 0.7273
    reference = {"case-1": {"stage": "need_my_action", "action": "reply"}}
    assert final_quality({"case-1": agent[-1]}, reference)["stage_action_accuracy"] == 1
    assert final_quality({"case-1": baseline[-1]}, reference)["stage_action_accuracy"] == 0
