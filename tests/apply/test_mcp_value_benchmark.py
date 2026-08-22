from capybot.evaluation.mcp_value_benchmark import _summarize


def test_mcp_benchmark_summary_tracks_evidence_and_fit_improvement() -> None:
    summary = _summarize(
        [
            {
                "case": "job-01",
                "accepted": True,
                "tool_success": True,
                "error": None,
                "agent_errors": [],
                "job_signal_delta": 3,
                "stage_changed": False,
                "action_changed": False,
                "fit_before": {"status": "needs_review"},
                "fit_after": {"status": "ok"},
                "observations": [
                    {
                        "duration_ms": 1200,
                        "novel_evidence_count": 1,
                        "used_evidence_count": 1,
                        "empty_result": False,
                        "utility": "evidence_used",
                    }
                ],
            },
            {
                "case": "job-02",
                "accepted": False,
                "tool_success": False,
                "error": "timeout",
                "agent_errors": [],
                "job_signal_delta": 0,
                "stage_changed": False,
                "action_changed": False,
                "fit_before": {"status": "needs_review"},
                "fit_after": {"status": "needs_review"},
                "observations": [],
            },
        ]
    )

    assert summary["cases"] == 2
    assert summary["tool_successes"] == 1
    assert summary["novel_evidence"] == 1
    assert summary["used_evidence"] == 1
    assert summary["job_signal_gain"] == 3
    assert summary["fit_status_improved"] == 1
    assert summary["failures"] == [
        {"case": "job-02", "error": "timeout", "agent_errors": []}
    ]
