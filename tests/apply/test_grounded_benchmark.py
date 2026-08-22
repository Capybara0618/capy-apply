from capybot.evaluation.grounded_benchmark import (
    SCENARIOS,
    TOOL_STRESS_SCENARIOS,
    _is_internship_title,
    _job_focus,
    _job_id,
    _runtime,
    build_cases,
)


def test_grounded_cases_rotate_deterministic_scenarios() -> None:
    jobs = [
        {
            "title": f"岗位{i}",
            "company": f"公司{i}",
            "href": f"https://www.zhipin.com/job_detail/job{i}.html",
            "summary": "当前招聘",
            "platform_job_id": f"job{i}",
            "query": "Agent 实习",
        }
        for i in range(50)
    ]

    cases = build_cases(jobs)

    assert len(cases) == 50
    assert {case.scenario for case in cases} == {item[0] for item in SCENARIOS}
    assert {
        scenario: sum(case.scenario == scenario for case in cases)
        for scenario, *_ in SCENARIOS
    } == {scenario: amount for scenario, amount, *_ in SCENARIOS}
    assert sum(len(case.history) + len(case.delta) for case in cases) == 334
    assert (
        sum(
            message.is_human_message and not message.from_me
            for case in cases
            for message in (*case.history, *case.delta)
        )
        == 20
    )
    assert (
        sum(
            message.message_type == "auto_followup"
            for case in cases
            for message in (*case.history, *case.delta)
        )
        == 52
    )
    assert "岗位0" in str(cases[0].history[0].text)


def test_tool_stress_profile_keeps_jobs_but_prioritizes_hr_interactions() -> None:
    jobs = [
        {
            "title": f"Agent 实习生{i}",
            "company": f"公司{i}",
            "href": f"https://www.zhipin.com/job_detail/job{i}.html",
            "summary": "当前招聘",
            "platform_job_id": f"job{i}",
            "query": "Agent 实习",
        }
        for i in range(50)
    ]

    cases = build_cases(jobs, scenario_profile="tool_stress")

    assert len(cases) == 50
    assert {
        scenario: sum(case.scenario == scenario for case in cases)
        for scenario, *_ in TOOL_STRESS_SCENARIOS
    } == {scenario: amount for scenario, amount, *_ in TOOL_STRESS_SCENARIOS}
    assert sum(
        any(message.is_human_message and not message.from_me for message in case.delta)
        for case in cases
    ) == 49
    assert sum(
        case.scenario
        in {
            "interview_invite",
            "project_question",
            "job_condition_gap",
            "employment_identity",
        }
        for case in cases
    ) == 32


def test_job_id_is_extracted_from_current_boss_url() -> None:
    assert (
        _job_id("https://www.zhipin.com/job_detail/abc123.html?ka=search")
        == "abc123"
    )


def test_only_explicit_internship_titles_are_accepted() -> None:
    assert _is_internship_title("AI Agent 实习生")
    assert _is_internship_title("LLM Engineer Intern")
    assert not _is_internship_title("AI 应用开发工程师")
    assert not _is_internship_title("AI 应用开发工程师（有经验优先）")


def test_job_focus_uses_live_job_description_keywords() -> None:
    assert (
        _job_focus(
            {
                "title": "AI Agent 实习生",
                "description": "负责 MCP 工具接入与 RAG 检索。",
            }
        )
        == "MCP、Agent"
    )


def test_runtime_separates_local_tools_from_external_mcp() -> None:
    result = _runtime(
        [
            {
                "metrics": {
                    "llm_call_count": 2,
                    "tool_call_count": 3,
                    "external_tool_call_count": 1,
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "duration_ms": 500,
                }
            }
        ]
    )

    assert result["tool_calls"] == 3
    assert result["external_tool_calls"] == 1
    assert result["local_tool_calls"] == 2
