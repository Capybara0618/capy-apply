"""Grounded benchmark built from current BOSS jobs and controlled chat deltas."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from capybot.apply.opportunity_service import OpportunityAnalysisService
from capybot.apply.store import ApplyStore
from capybot.connectors.boss import BossConnector
from capybot.evaluation.cold_start_benchmark import (
    benchmark_database_url,
    reset_benchmark_database,
)

SEARCH_QUERIES = (
    "Agent 实习",
    "AI Agent 实习",
    "大模型 实习",
    "RAG 实习",
    "LLM 实习",
    "AI 应用开发 实习",
    "Python AI 实习",
    "智能体 实习",
    "算法 实习",
    "AI 后端 实习",
)
SCENARIOS = (
    ("silent_auto_followup", 26, "discovered", "wait"),
    ("silent_manual_followup", 10, "discovered", "wait"),
    ("resume_request", 5, "need_my_action", "send_material"),
    ("material_sent", 2, "waiting_feedback", "wait"),
    ("interview_invite", 2, "interviewing", "confirm_interview"),
    ("project_question", 2, "need_my_action", "reply"),
    ("availability", 1, "need_my_action", "reply"),
    ("rejected", 1, "closed", "close"),
    ("employment_identity", 1, "need_my_action", "verify"),
)
TOOL_STRESS_SCENARIOS = (
    ("interview_invite", 8, "interviewing", "confirm_interview"),
    ("project_question", 8, "need_my_action", "reply"),
    ("employment_identity", 8, "need_my_action", "verify"),
    ("job_condition_gap", 8, "need_my_action", "reply"),
    ("ambiguous_event", 6, "need_my_action", "reply"),
    ("resume_request", 5, "need_my_action", "send_material"),
    ("material_sent", 3, "waiting_feedback", "wait"),
    ("availability", 2, "need_my_action", "reply"),
    ("rejected", 1, "closed", "close"),
    ("silent_manual_followup", 1, "discovered", "wait"),
)
SCENARIO_PROFILES = {
    "realistic": SCENARIOS,
    "tool_stress": TOOL_STRESS_SCENARIOS,
}


@dataclass(frozen=True)
class GroundedMessage:
    from_me: bool
    text: str | None
    message_type: str = "text"
    is_human_message: bool = True


@dataclass(frozen=True)
class GroundedCase:
    case_id: str
    job: dict[str, str]
    scenario: str
    history: tuple[GroundedMessage, ...]
    delta: tuple[GroundedMessage, ...]
    expected_stage: str
    expected_action: str


async def collect_current_jobs(limit: int = 50) -> list[dict[str, str]]:
    connector = BossConnector()
    candidates: dict[str, dict[str, str]] = {}
    for query in SEARCH_QUERIES:
        rows: list[dict[str, str]] = []
        for attempt in range(3):
            rows = await connector._search_jobs_page_async(query)
            if rows:
                break
            await asyncio.sleep(2.0 * (attempt + 1))
        if not rows:
            raise RuntimeError(f"BOSS 当前岗位搜索连续返回空页：{query}")
        for row in rows:
            href = str(row.get("href") or "")
            title = str(row.get("title") or "").strip()
            if not href or not _is_internship_title(title):
                continue
            candidates.setdefault(
                href,
                {
                    "title": title,
                    "company": str(row.get("company") or "").strip(),
                    "href": href,
                    "summary": str(row.get("summary") or ""),
                    "platform_job_id": _job_id(href),
                    "query": query,
                },
            )
        await asyncio.sleep(1.2)

    jobs: list[dict[str, str]] = []
    candidate_rows = list(candidates.values())
    failed = 0
    batch_size = 3
    print(
        f"[岗位采集] 搜索得到 {len(candidate_rows)} 个去重候选，开始读取岗位详情。",
        flush=True,
    )
    for offset in range(0, len(candidate_rows), batch_size):
        batch = candidate_rows[offset : offset + batch_size]
        results = await asyncio.gather(
            *(_collect_job_detail(connector, candidate) for candidate in batch)
        )
        for result in results:
            if result is None:
                failed += 1
                continue
            jobs.append(result)
            if len(jobs) >= limit:
                break
        scanned = min(offset + len(batch), len(candidate_rows))
        print(
            f"[岗位采集] 已扫描 {scanned}/{len(candidate_rows)}，"
            f"有效 {len(jobs)}/{limit}，失败 {failed}。",
            flush=True,
        )
        if len(jobs) >= limit:
            break
        await asyncio.sleep(0.8)
    return jobs


async def _collect_job_detail(
    connector: BossConnector,
    candidate: dict[str, str],
) -> dict[str, str] | None:
    detail: dict[str, Any] | None = None
    for attempt in range(2):
        try:
            detail = await asyncio.wait_for(
                connector._read_rendered_job_page_async(candidate["href"]),
                timeout=20.0,
            )
            break
        except Exception:
            if attempt == 0:
                await asyncio.sleep(1.0)
    if detail is None:
        return None
    job_info = detail.get("jobInfo") or {}
    company = str((detail.get("brandComInfo") or {}).get("brandName") or "").strip()
    title = str(job_info.get("jobName") or candidate["title"]).strip()
    description = str(
        job_info.get("postDescription") or detail.get("jobDetail") or ""
    ).strip()
    if not _is_internship_title(title) or not company or not description:
        return None
    return {
        **candidate,
        "title": title,
        "company": company,
        "salary": str(job_info.get("salaryDesc") or ""),
        "city": str(job_info.get("cityName") or ""),
        "experience": str(job_info.get("experienceName") or ""),
        "education": str(job_info.get("degreeName") or ""),
        "description": description,
    }


def _is_internship_title(title: str) -> bool:
    normalized = str(title or "").strip().lower()
    return "实习" in normalized or re.search(r"\bintern(ship)?\b", normalized) is not None


def build_cases(
    jobs: list[dict[str, str]],
    *,
    scenario_profile: str = "realistic",
) -> list[GroundedCase]:
    scenarios = SCENARIO_PROFILES.get(scenario_profile)
    if scenarios is None:
        raise ValueError(f"未知场景分布: {scenario_profile}")
    scenario_plan = _scenario_plan(len(jobs), scenarios=scenarios)
    cases: list[GroundedCase] = []
    for index, job in enumerate(jobs):
        scenario, stage, action = scenario_plan[index]
        focus = _job_focus(job)
        cases.append(
            GroundedCase(
                case_id=f"grounded-{index + 1:03d}",
                job=job,
                scenario=scenario,
                history=_conversation_history(job, index, focus),
                delta=_conversation_delta(job, scenario, focus),
                expected_stage=stage,
                expected_action=action,
            )
        )
    return cases


async def run_grounded_benchmark(
    *,
    limit: int = 50,
    research_limit: int = 10,
    concurrency: int = 4,
    fixture_path: Path | None = None,
    input_fixture_path: Path | None = None,
    scenario_profile: str = "realistic",
) -> dict[str, Any]:
    jobs = (
        _load_jobs_fixture(input_fixture_path, limit)
        if input_fixture_path
        else await collect_current_jobs(limit)
    )
    if len(jobs) < limit:
        raise RuntimeError(f"当前 BOSS 搜索仅取得 {len(jobs)} 个去重岗位，未达到 {limit}")
    cases = build_cases(jobs, scenario_profile=scenario_profile)
    scenarios = SCENARIO_PROFILES[scenario_profile]
    target_url = benchmark_database_url(
        database_name="capybot_apply_benchmark_grounded"
    )
    reset_benchmark_database(target_url)
    store, opportunity_ids = _seed_database(target_url, cases)
    if fixture_path:
        _write_fixture(fixture_path, cases)

    service = OpportunityAnalysisService(
        store,
        mcp_env={"CAPYBOT_APPLY_DATABASE_URL": target_url},
    )
    slots = asyncio.Semaphore(max(1, concurrency))

    async def analyze(opportunity_id: str, trigger: dict[str, Any]) -> dict[str, Any]:
        async with slots:
            return await service.analyze(opportunity_id, trigger=trigger)

    initial = await asyncio.gather(
        *(
            analyze(
                opportunity_ids[case.case_id],
                {"type": "cold_start", "allow_external": True},
            )
            for case in cases
        )
    )
    delta_message_ids = _insert_deltas(store, cases, opportunity_ids)
    final = await asyncio.gather(
        *(
            analyze(
                opportunity_ids[case.case_id],
                {
                    "type": "import_delta",
                    "new_message_ids": delta_message_ids[case.case_id],
                    "allow_external": True,
                },
            )
            for case in cases
        )
    )
    quality = _quality(cases, final)

    research_rows = []
    for case in cases[: max(0, research_limit)]:
        opportunity_id = opportunity_ids[case.case_id]
        result = await analyze(
            opportunity_id,
            {"type": "research", "focus": "job", "allow_external": True},
        )
        observations = service.runs.payload(str(result.get("run_id") or "")).get(
            "tool_observations"
        ) or []
        selected = [
            row for row in observations if row.get("tool_name") == "boss_fetch_job_detail"
        ]
        research_rows.append(
            {
                "accepted": bool(result.get("accepted")),
                "tool_called": bool(selected),
                "tool_success": bool(
                    selected
                    and selected[-1].get("status") == "ok"
                    and not selected[-1].get("empty_result")
                ),
                "novel_evidence": sum(
                    int(row.get("novel_evidence_count") or 0) for row in selected
                ),
                "used_evidence": sum(
                    int(row.get("used_evidence_count") or 0) for row in selected
                ),
                "duration_ms": sum(int(row.get("duration_ms") or 0) for row in selected),
            }
        )
        await asyncio.sleep(0.8)

    return {
        "benchmark": {
            "kind": (
                "current_job_tool_stress_synthetic_chat"
                if scenario_profile == "tool_stress"
                else "current_job_grounded_synthetic_chat"
            ),
            "scenario_profile": scenario_profile,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "current_job_count": len(jobs),
            "scenario_count": len(scenarios),
            "episode_count": len(cases) * 2,
            "synthetic_message_count": sum(
                len(case.history) + len(case.delta) for case in cases
            ),
            "database": "capybot_apply_benchmark_grounded",
            "chat_source": "controlled_synthetic",
            "job_source": (
                "frozen_live_boss_search" if input_fixture_path else "live_boss_search"
            ),
            "internship_title_verified": all(
                _is_internship_title(case.job["title"]) for case in cases
            ),
            "real_pattern_reference": {
                "source_conversations": 41,
                "valid_conversations": 40,
                "source_messages": 267,
                "no_hr_reply": 29,
                "has_hr_reply": 11,
                "generated_no_hr_reply": sum(
                    case.scenario.startswith("silent_") for case in cases
                ),
                "generated_has_hr_reply": sum(
                    not case.scenario.startswith("silent_") for case in cases
                ),
            },
        },
        "quality": quality,
        "quality_by_scenario": _quality_by_scenario(cases, final),
        "runtime": {
            "initial": _runtime(initial),
            "delta": _runtime(final),
        },
        "agent_tool_observations": _tool_observation_summary(
            service,
            [*initial, *final],
        ),
        "job_mcp": _research_summary(research_rows),
        "tool_coverage": {
            "stage_analysis_mcp_calls": sum(
                int((value.get("metrics") or {}).get("external_tool_call_count") or 0)
                for value in [*initial, *final]
            ),
            "stage_analysis_boss_mcp_calls": sum(
                int((value.get("metrics") or {}).get("boss_tool_call_count") or 0)
                for value in [*initial, *final]
            ),
            "stage_analysis_company_mcp_calls": sum(
                int((value.get("metrics") or {}).get("intel_tool_call_count") or 0)
                for value in [*initial, *final]
            ),
            "stage_analysis_local_tool_calls": sum(
                max(
                    0,
                    int((value.get("metrics") or {}).get("tool_call_count") or 0)
                    - int(
                        (value.get("metrics") or {}).get(
                            "external_tool_call_count"
                        )
                        or 0
                    ),
                )
                for value in [*initial, *final]
            ),
            "explicit_job_research_mcp_calls": sum(
                row["tool_called"] for row in research_rows
            ),
            "policy": "由 LLM 根据信息缺口按需调用，不为展示而强制调用外部 MCP",
        },
        "scenario_distribution": {
            scenario: sum(case.scenario == scenario for case in cases)
            for scenario, *_ in scenarios
        },
    }


def _seed_database(
    database_url: str,
    cases: list[GroundedCase],
) -> tuple[ApplyStore, dict[str, str]]:
    store = ApplyStore(database_url=database_url)
    account_id = store.upsert_account(
        {
            "id": "grounded_benchmark_account",
            "account_uid": "grounded-benchmark",
            "display_name": "Grounded Benchmark",
            "source": "benchmark",
        }
    )
    opportunity_ids: dict[str, str] = {}
    for index, case in enumerate(cases):
        conversation_id = store.upsert_conversation(
            {
                "account_id": account_id,
                "conversation_id": f"conv-{case.case_id}",
                "boss_uid": f"hr-{case.case_id}",
                "contact_name": f"招聘者{index + 1:02d}",
                "contact_role": "招聘者",
                "company": case.job["company"],
                "raw_payload": {"benchmark_case": case.case_id},
            }
        )
        store.upsert_contact_from_conversation(conversation_id)
        store.upsert_job_card(
            conversation_id,
            {
                "platform_job_id": case.job["platform_job_id"],
                "title": case.job["title"],
                "company": case.job["company"],
                "raw_payload": {
                    "source_url": case.job["href"],
                    "search_summary": case.job["summary"],
                    "benchmark_case": case.case_id,
                },
            },
        )
        for message_index, message in enumerate(case.history, start=1):
            store.upsert_message(
                {
                    "conversation_id": conversation_id,
                    "message_id": f"{case.case_id}-history-{message_index:02d}",
                    "from_me": message.from_me,
                    "sender_name": "候选人" if message.from_me else "招聘者",
                    "text": message.text,
                    "message_type": message.message_type,
                    "from_me_confidence": 1.0,
                    "is_human_message": message.is_human_message,
                    "sent_at": (
                        f"2026-07-26T09:{index % 60:02d}:{message_index:02d}+00:00"
                    ),
                    "raw_payload": {
                        "benchmark_case": case.case_id,
                        "synthetic_message_type": message.message_type,
                    },
                }
            )
        opportunity_ids[case.case_id] = store.ensure_opportunities_for_conversation(
            conversation_id
        )[0]
    return store, opportunity_ids


def _insert_deltas(
    store: ApplyStore,
    cases: list[GroundedCase],
    opportunity_ids: dict[str, str],
) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for index, case in enumerate(cases):
        stored = store.opportunity_context(
            opportunity_ids[case.case_id]
        ).get("conversations") or []
        if not stored:
            raise RuntimeError(f"Grounded case 缺少会话: {case.case_id}")
        conversation_id = str(stored[0]["id"])
        message_ids: list[str] = []
        for message_index, message in enumerate(case.delta, start=1):
            message_id = f"{case.case_id}-delta-{message_index:02d}"
            store.upsert_message(
                {
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "from_me": message.from_me,
                    "sender_name": "候选人" if message.from_me else "招聘者",
                    "text": message.text,
                    "message_type": message.message_type,
                    "from_me_confidence": 1.0,
                    "is_human_message": message.is_human_message,
                    "sent_at": (
                        f"2026-07-26T10:{index % 60:02d}:{message_index:02d}+00:00"
                    ),
                    "raw_payload": {
                        "benchmark_case": case.case_id,
                        "synthetic_message_type": message.message_type,
                    },
                }
            )
            message_ids.append(message_id)
        values[case.case_id] = message_ids
    return values


def _quality(cases: list[GroundedCase], values: list[dict[str, Any]]) -> dict[str, Any]:
    stage_hits = action_hits = joint_hits = 0
    for case, value in zip(cases, values, strict=True):
        decision = value.get("decision") or {}
        stage_hit = decision.get("stage") == case.expected_stage
        action_hit = (decision.get("next") or {}).get("action") == case.expected_action
        stage_hits += stage_hit
        action_hits += action_hit
        joint_hits += stage_hit and action_hit
    count = len(cases)
    return {
        "cases": count,
        "accepted": sum(bool(value.get("accepted")) for value in values),
        "stage_accuracy": round(stage_hits / count, 4),
        "action_accuracy": round(action_hits / count, 4),
        "stage_action_accuracy": round(joint_hits / count, 4),
    }


def _quality_by_scenario(
    cases: list[GroundedCase],
    values: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, tuple[list[GroundedCase], list[dict[str, Any]]]] = {}
    for case, value in zip(cases, values, strict=True):
        case_group, value_group = grouped.setdefault(case.scenario, ([], []))
        case_group.append(case)
        value_group.append(value)
    return {
        scenario: _quality(case_group, value_group)
        for scenario, (case_group, value_group) in sorted(grouped.items())
    }


def _runtime(values: list[dict[str, Any]]) -> dict[str, int]:
    metrics = [value.get("metrics") or {} for value in values]
    tool_calls = sum(int(item.get("tool_call_count") or 0) for item in metrics)
    external_tool_calls = sum(
        int(item.get("external_tool_call_count") or 0) for item in metrics
    )
    return {
        "runs": len(values),
        "llm_calls": sum(
            int(item.get("llm_call_count") or item.get("iterations") or 0)
            for item in metrics
        ),
        "tool_calls": tool_calls,
        "external_tool_calls": external_tool_calls,
        "local_tool_calls": max(0, tool_calls - external_tool_calls),
        "tokens": sum(
            int(item.get("prompt_tokens") or 0)
            + int(item.get("completion_tokens") or 0)
            for item in metrics
        ),
        "duration_ms": sum(int(item.get("duration_ms") or 0) for item in metrics),
    }


def _research_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "cases": len(rows),
        "tool_calls": sum(row["tool_called"] for row in rows),
        "tool_successes": sum(row["tool_success"] for row in rows),
        "accepted": sum(row["accepted"] for row in rows),
        "novel_evidence": sum(row["novel_evidence"] for row in rows),
        "used_evidence": sum(row["used_evidence"] for row in rows),
        "duration_ms": sum(row["duration_ms"] for row in rows),
    }


def _tool_observation_summary(
    service: OpportunityAnalysisService,
    values: list[dict[str, Any]],
) -> dict[str, dict[str, int | float]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        run_id = str(value.get("run_id") or "")
        if not run_id:
            continue
        observations = service.runs.payload(run_id).get("tool_observations") or []
        for observation in observations:
            if (
                int(observation.get("duration_ms") or 0) == 0
                and "相同参数调用过"
                in str(observation.get("result_summary") or "")
            ):
                continue
            name = str(observation.get("tool_name") or "unknown")
            grouped.setdefault(name, []).append(observation)

    summary: dict[str, dict[str, int | float]] = {}
    for name, observations in sorted(grouped.items()):
        durations = [int(row.get("duration_ms") or 0) for row in observations]
        summary[name] = {
            "calls": len(observations),
            "successes": sum(row.get("status") == "ok" for row in observations),
            "novel_evidence": sum(
                int(row.get("novel_evidence_count") or 0) for row in observations
            ),
            "used_evidence": sum(
                int(row.get("used_evidence_count") or 0) for row in observations
            ),
            "context_uses": sum(
                str(row.get("utility") or "")
                in {"routing_context", "rule_context"}
                for row in observations
            ),
            "average_duration_ms": (
                round(sum(durations) / len(durations), 1) if durations else 0.0
            ),
        }
    return summary


def _write_fixture(path: Path, cases: list[GroundedCase]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "dataset_kind": "current_job_grounded_synthetic_chat",
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "cases": [
                    {
                        "case_id": case.case_id,
                        "job": case.job,
                        "scenario": case.scenario,
                        "history": [
                            {
                                "speaker": "me" if message.from_me else "hr",
                                "text": message.text,
                                "message_type": message.message_type,
                                "is_human_message": message.is_human_message,
                            }
                            for message in case.history
                        ],
                        "delta": [
                            {
                                "speaker": "me" if message.from_me else "hr",
                                "text": message.text,
                                "message_type": message.message_type,
                                "is_human_message": message.is_human_message,
                            }
                            for message in case.delta
                        ],
                        "expected": {
                            "stage": case.expected_stage,
                            "action": case.expected_action,
                        },
                    }
                    for case in cases
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown_report(report, path.with_suffix(".md"))


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    benchmark = report["benchmark"]
    quality = report["quality"]
    initial = report["runtime"]["initial"]
    delta = report["runtime"]["delta"]
    mcp = report["job_mcp"]
    coverage = report.get("tool_coverage") or {}
    observations = report.get("agent_tool_observations") or {}
    scenario_quality = report.get("quality_by_scenario") or {}
    retest = report.get("job_mcp_retest")
    lines = [
        "# Grounded Agent Benchmark",
        "",
        "## 数据边界",
        "",
        f"- 当前 BOSS 实习岗位：{benchmark['current_job_count']} 个。",
        f"- 场景分布：`{benchmark.get('scenario_profile', 'realistic')}`。",
        f"- 受控合成中文消息：{benchmark['synthetic_message_count']} 条。",
        f"- 冷启动与增量回合：{benchmark['episode_count']} 个。",
        "- 岗位事实来自登录后的 BOSS 搜索与详情页快照；聊天为受控合成数据，不冒充真实 HR 对话。",
        (
            f"- 标签由 {benchmark['scenario_count']} 类预定义招聘场景产生，"
            "用于验证路由、结构化输出和工具闭环，不等同于生产准确率。"
        ),
        "",
        "## 结果",
        "",
        f"- 阶段命中率：{quality['stage_accuracy']:.2%}",
        f"- 下一步行动命中率：{quality['action_accuracy']:.2%}",
        f"- 联合命中率：{quality['stage_action_accuracy']:.2%}",
        (
            f"- 冷启动：{initial['runs']} runs，{initial['llm_calls']} 次 LLM，"
            f"{initial['tokens']} tokens。"
        ),
        (
            f"- 增量：{delta['runs']} runs，{delta['llm_calls']} 次 LLM，"
            f"{delta['tokens']} tokens。"
        ),
        (
            f"- 核心分析工具调用：{delta.get('local_tool_calls', 0)} 次本地工具，"
            f"{delta.get('external_tool_calls', 0)} 次外部 MCP。"
        ),
        (
            f"- 外部 MCP 分布：岗位详情 "
            f"{coverage.get('stage_analysis_boss_mcp_calls', 0)} 次，公司研究 "
            f"{coverage.get('stage_analysis_company_mcp_calls', 0)} 次。"
        ),
    ]
    if mcp["cases"]:
        lines.append(
            f"- 独立岗位研究回归：{mcp['tool_successes']}/{mcp['cases']} 成功，"
            f"{mcp['used_evidence']} 条新增证据被最终输出引用。"
        )
    if retest:
        lines.append(
            f"- 修复后岗位 MCP：{retest['tool_successes']}/{retest['cases']} 成功，"
            f"{retest['used_evidence']} 条新增证据被最终输出引用。"
        )
    if observations:
        lines.extend(["", "## Tool Observation", ""])
        for name, metrics in observations.items():
            if metrics.get("context_uses"):
                lines.append(
                    f"- `{name}`：{metrics['calls']} 次调用，"
                    f"{metrics['successes']} 次成功，作为规划/规则上下文使用 "
                    f"{metrics['context_uses']} 次，"
                    f"平均耗时 {metrics['average_duration_ms']} ms。"
                )
            else:
                lines.append(
                    f"- `{name}`：{metrics['calls']} 次调用，"
                    f"{metrics['successes']} 次成功，新增证据 "
                    f"{metrics['novel_evidence']} 条，最终采用 "
                    f"{metrics['used_evidence']} 条，"
                    f"平均耗时 {metrics['average_duration_ms']} ms。"
                )
    if scenario_quality:
        lines.extend(["", "## 分场景质量", ""])
        for name, metrics in scenario_quality.items():
            lines.append(
                f"- `{name}`：{metrics['accepted']}/{metrics['cases']} 接受，"
                f"阶段与行动联合命中率 {metrics['stage_action_accuracy']:.2%}。"
            )
    lines.extend(
        [
            "",
            "## Agent Loop 含义",
            "",
            "- 首轮 LLM 读取本轮增量、机会状态和可用工具目录，自主判断是否存在信息缺口。",
            "- 需要补证据时，LLM 发出 Tool Call；Runtime 执行本地工具或只读 MCP，并把 Observation 返回下一轮 LLM。",
            "- 最终结构化输出经过 Self-check 与 CommitGate 后才能写入阶段、任务、草稿和摘要。",
            "- 并非每条对话都调用 MCP：拒绝、材料请求或静默追问已有充分聊天证据，强制外查只会增加延迟和噪声。",
            "",
            "## 可复现边界",
            "",
            "- `.artifacts/grounded/` 保存本地冻结数据，不提交公开仓库。",
            "- 结果受 BOSS 页面可用性、模型服务和网络状态影响。",
            "- 该基准用于回归 Agent 路由、工具调用、证据闭环和故障降级，不能替代独立人工 Gold Set。",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _job_id(href: str) -> str:
    match = re.search(r"/job_detail/([^./?]+)", href)
    return match.group(1) if match else href


def _job_focus(job: dict[str, str]) -> str:
    text = f"{job.get('title', '')} {job.get('description', '')}".lower()
    candidates = (
        ("MCP", "mcp"),
        ("Agent", "agent"),
        ("RAG", "rag"),
        ("LangGraph", "langgraph"),
        ("LangChain", "langchain"),
        ("大模型", "大模型"),
        ("Python", "python"),
        ("Java", "java"),
        ("Go", "golang"),
        ("NLP", "nlp"),
        ("PyTorch", "pytorch"),
        ("向量检索", "向量"),
    )
    matched = [label for label, marker in candidates if marker in text]
    return "、".join(matched[:2]) or "AI 应用开发"


def _conversation_history(
    job: dict[str, str],
    index: int,
    focus: str,
) -> tuple[GroundedMessage, ...]:
    intro = (
        f"您好，看到贵司在招{job['title']}。"
        f"我做过{focus}相关项目，可以连续实习四个月以上，想进一步了解岗位。"
    )
    messages = [
        GroundedMessage(True, intro),
        GroundedMessage(False, "BOSS 岗位沟通卡", "platform_card", False),
        GroundedMessage(False, job["title"], "job_card", False),
    ]
    if index < 43:
        messages.append(
            GroundedMessage(
                True,
                f"我的项目里主要负责{focus}的实现和评估，简历附后，感谢。",
            )
        )
    if index < 49:
        messages.append(GroundedMessage(True, None, "image"))
    if index < 6:
        messages.append(
            GroundedMessage(False, "平台求职竞争力提示", "platform_card", False)
        )
    return tuple(messages)


def _conversation_delta(
    job: dict[str, str],
    scenario: str,
    focus: str,
) -> tuple[GroundedMessage, ...]:
    title = job["title"]
    city = job.get("city") or "岗位所在城市"
    if scenario == "silent_auto_followup":
        return (
            GroundedMessage(
                False,
                "VIP 求职助手已在招聘者活跃时帮你追问",
                "auto_followup",
                False,
            ),
            GroundedMessage(
                True,
                f"您好，我对{title}仍然很感兴趣，期待您的回复。",
                "auto_followup",
                False,
            ),
        )
    if scenario == "silent_manual_followup":
        return (
            GroundedMessage(True, f"您好，请问{title}目前还在招聘吗？感谢。"),
        )
    if scenario == "resume_request":
        return (
            GroundedMessage(
                False,
                f"方便发一份 PDF 简历和项目介绍吗？我们会结合{focus}方向一起评估。",
            ),
        )
    if scenario == "material_sent":
        return (
            GroundedMessage(False, "方便把简历和项目材料发过来吗？"),
            GroundedMessage(True, None, "image"),
            GroundedMessage(True, "好的，简历和项目介绍已经发您了。"),
        )
    if scenario == "interview_invite":
        return (
            GroundedMessage(False, f"你的{focus}项目经历和岗位比较匹配。"),
            GroundedMessage(
                False,
                f"想约你明天下午三点参加{title}的线上面试，时间方便吗？",
            ),
        )
    if scenario == "project_question":
        return (
            GroundedMessage(False, f"这个岗位主要会做{focus}相关开发。"),
            GroundedMessage(
                False,
                f"可以具体介绍一下你做过的{focus}项目吗？最好说明你的具体工作。",
            ),
        )
    if scenario == "availability":
        return (
            GroundedMessage(False, "这个岗位希望候选人尽快到岗。"),
            GroundedMessage(
                False,
                f"工作地点在{city}，每周至少四天、连续实习四个月，可以接受吗？",
            ),
        )
    if scenario == "job_condition_gap":
        return (
            GroundedMessage(
                False,
                "这个岗位目前还在推进，不过实际工作内容可能会随项目安排调整。",
            ),
            GroundedMessage(
                False,
                "你可以先结合岗位职责、技术要求和薪资范围，说明一下是否接受吗？",
            ),
        )
    if scenario == "ambiguous_event":
        return (
            GroundedMessage(
                False,
                f"我们本周有一场{focus}技术交流会，也会介绍团队正在做的项目。",
            ),
            GroundedMessage(False, "你有兴趣参加吗？后续是否进入面试需要再评估。"),
        )
    if scenario == "rejected":
        return (
            GroundedMessage(False, "我们已经看过你的资料了。"),
            GroundedMessage(
                False,
                "感谢关注，目前这个岗位已经招满了，后续有机会再联系。",
            ),
        )
    return (
        GroundedMessage(
            False,
            "这个岗位由合作项目组招聘，具体入职主体需要后续确认，你可以接受吗？",
        ),
    )


def _scenario_plan(
    count: int,
    *,
    scenarios: tuple[tuple[str, int, str, str], ...] = SCENARIOS,
) -> list[tuple[str, str, str]]:
    weighted = [
        (name, stage, action)
        for name, amount, stage, action in scenarios
        for _ in range(amount)
    ]
    interleaved = [weighted[(index * 17) % len(weighted)] for index in range(len(weighted))]
    return interleaved[:count]


def _load_jobs_fixture(path: Path, limit: int) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Grounded fixture 缺少 cases 列表")
    jobs = [
        dict(row["job"])
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("job"), dict)
        and _is_internship_title(str(row["job"].get("title") or ""))
    ]
    if len(jobs) < limit:
        raise ValueError(f"Grounded fixture 仅包含 {len(jobs)} 个明确实习岗位")
    return jobs[:limit]
