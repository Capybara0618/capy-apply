"""Chronological A/B benchmark for incremental tracking versus one-shot analysis."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.engine import make_url

from capybot.apply.opportunity_service import OpportunityAnalysisService
from capybot.apply.postgres import apply_database_url
from capybot.apply.store import ApplyStore
from capybot.evaluation.cold_start_benchmark import (
    BenchmarkCase,
    OneShotBaseline,
    _build_case,
    _tool_utility_summary,
    benchmark_database_url,
    bootstrap_opportunities,
    clone_real_l1,
    reset_benchmark_database,
)
from capybot.evaluation.gold_set import case_id_for_conversation, load_json


@dataclass(frozen=True)
class TurnBatch:
    conversation_id: str
    rows: tuple[dict[str, Any], ...]
    human_message_ids: tuple[str, ...]
    speaker: str


def speaker_turn_batches(rows: list[dict[str, Any]]) -> list[TurnBatch]:
    """Group platform rows around consecutive human messages from the same speaker."""

    batches: list[TurnBatch] = []
    current: list[dict[str, Any]] = []
    current_speaker = ""
    for row in rows:
        is_human = bool(row.get("is_human_message", True))
        speaker = "me" if bool(row.get("from_me")) else "hr"
        if is_human and current_speaker and speaker != current_speaker:
            batches.append(_turn_batch(current, current_speaker))
            current = []
        current.append(row)
        if is_human:
            current_speaker = speaker
    if current and current_speaker:
        batches.append(_turn_batch(current, current_speaker))
    return batches


def _turn_batch(rows: list[dict[str, Any]], speaker: str) -> TurnBatch:
    return TurnBatch(
        conversation_id=str(rows[0]["conversation_id"]),
        rows=tuple(rows),
        human_message_ids=tuple(
            str(row["message_id"])
            for row in rows
            if bool(row.get("is_human_message", True)) and row.get("message_id")
        ),
        speaker=speaker,
    )


async def run_dynamic_benchmark(
    *,
    source_url: str | None = None,
    target_url: str | None = None,
    source_account_id: str | None = None,
    reference_set_path: str | Path,
    concurrency: int = 3,
    limit: int | None = None,
    allow_external: bool = True,
) -> dict[str, Any]:
    """Replay real conversations chronologically in an isolated PostgreSQL database."""

    source_url = source_url or apply_database_url()
    target_url = target_url or benchmark_database_url(
        source_url,
        database_name="capybot_apply_benchmark_dynamic",
    )
    if make_url(source_url).database == make_url(target_url).database:
        raise ValueError("生产数据库和动态基准数据库不能相同")

    references = _load_references(reference_set_path)
    reset_benchmark_database(target_url)
    clone = clone_real_l1(
        source_url=source_url,
        target_url=target_url,
        account_id=source_account_id,
    )
    rows_by_conversation = _hide_and_collect_messages(target_url)
    store = ApplyStore(account_id=clone["account_id"], database_url=target_url)
    bootstrap_opportunities(store)

    selected = [
        conversation_id
        for conversation_id in rows_by_conversation
        if case_id_for_conversation(conversation_id) in references
    ]
    if limit and limit > 0:
        selected = selected[:limit]

    service = OpportunityAnalysisService(
        store,
        mcp_env={"CAPYBOT_APPLY_DATABASE_URL": target_url},
    )
    baseline = OneShotBaseline(service.model)
    model_slots = asyncio.Semaphore(max(1, concurrency))

    async def limited_agent(
        opportunity_id: str,
        trigger: dict[str, Any],
    ) -> dict[str, Any]:
        async with model_slots:
            return await service.analyze(opportunity_id, trigger=trigger)

    async def limited_baseline(case: BenchmarkCase) -> dict[str, Any]:
        async with model_slots:
            return await baseline.run(case)

    async def replay_conversation(conversation_id: str) -> dict[str, Any]:
        opportunity_ids = store.opportunity_ids_for_conversation(conversation_id)
        if not opportunity_ids:
            return {"conversation_id": conversation_id, "error": "机会创建失败"}
        opportunity_id = opportunity_ids[0]
        episodes: list[dict[str, Any]] = []
        for index, batch in enumerate(
            speaker_turn_batches(rows_by_conversation[conversation_id])
        ):
            _insert_messages(target_url, batch.rows)
            case = _build_case(store, opportunity_id)
            if case is None:
                continue
            trigger = {
                "type": "cold_start" if index == 0 else "import_delta",
                "new_message_ids": list(batch.human_message_ids),
                "allow_external": allow_external,
            }
            agent_value, baseline_value = await asyncio.gather(
                limited_agent(opportunity_id, trigger),
                limited_baseline(case),
            )
            episodes.append(
                {
                    "index": index + 1,
                    "speaker": batch.speaker,
                    "new_message_count": len(batch.human_message_ids),
                    "agent": agent_value,
                    "baseline": baseline_value,
                }
            )
        return {
            "conversation_id": conversation_id,
            "case_id": case_id_for_conversation(conversation_id),
            "opportunity_id": opportunity_id,
            "episodes": episodes,
        }

    results = await asyncio.gather(*(replay_conversation(value) for value in selected))
    valid_results = [value for value in results if value.get("episodes")]
    agent_runs = [
        episode["agent"]
        for value in valid_results
        for episode in value["episodes"]
    ]
    baseline_runs = [
        episode["baseline"]
        for value in valid_results
        for episode in value["episodes"]
    ]
    final_agent = {
        value["case_id"]: value["episodes"][-1]["agent"]
        for value in valid_results
    }
    final_baseline = {
        value["case_id"]: value["episodes"][-1]["baseline"]
        for value in valid_results
    }
    agent_summary = aggregate_runs(agent_runs)
    baseline_summary = aggregate_runs(baseline_runs)
    return {
        "benchmark": {
            "kind": "chronological_real_l1_tracking",
            "database": str(make_url(target_url).database),
            "source_account_selected_explicitly": bool(source_account_id),
            "conversation_count": len(valid_results),
            "episode_count": len(agent_runs),
            "reference_kind": "llm_double_annotated_adjudicated_reference",
            "external_mcp_enabled": allow_external,
            "grouping": "consecutive_same_speaker_turns",
            "agent_input": "new_turn+previous_projection+on_demand_memory",
            "baseline_input": "all_history+all_job_cards+all_skills_every_turn",
        },
        "source_snapshot": clone["counts"],
        "agent": {
            **agent_summary,
            "final_quality": final_quality(final_agent, references),
        },
        "baseline": {
            **baseline_summary,
            "final_quality": final_quality(final_baseline, references),
        },
        "comparison": comparison(agent_summary, baseline_summary),
        "tool_utility": _tool_utility_summary(store),
        "failures": [
            {"conversation_id": value.get("conversation_id"), "error": value.get("error")}
            for value in results
            if value.get("error")
        ],
    }


def _hide_and_collect_messages(database_url: str) -> dict[str, list[dict[str, Any]]]:
    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["boss_messages"])
    table = metadata.tables["boss_messages"]
    try:
        with engine.begin() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    select(table).order_by(
                        table.c.conversation_id,
                        table.c.sent_at.asc().nullsfirst(),
                        table.c.created_at,
                        table.c.message_id,
                    )
                ).mappings()
            ]
            connection.execute(table.delete())
    finally:
        engine.dispose()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["conversation_id"])].append(row)
    return dict(grouped)


def _insert_messages(database_url: str, rows: tuple[dict[str, Any], ...]) -> None:
    if not rows:
        return
    engine = create_engine(database_url, pool_pre_ping=True)
    metadata = MetaData()
    metadata.reflect(bind=engine, only=["boss_messages"])
    try:
        with engine.begin() as connection:
            connection.execute(metadata.tables["boss_messages"].insert(), list(rows))
    finally:
        engine.dispose()


def _load_references(path: str | Path) -> dict[str, dict[str, str]]:
    payload = load_json(path)
    if payload.get("dataset_kind") != "llm_double_annotated_adjudicated_reference":
        raise ValueError("动态基准只接受双标注裁决参考集")
    return {
        str(item["case_id"]): {
            "stage": str(item["stage"]),
            "action": str(item["action"]),
        }
        for item in payload.get("annotations") or []
        if item.get("include") and item.get("stage") and item.get("action")
    }


def aggregate_runs(values: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = [value.get("metrics") or {} for value in values]
    durations = [int(item.get("duration_ms") or 0) for item in metrics]
    prompt_tokens = sum(int(item.get("prompt_tokens") or 0) for item in metrics)
    completion_tokens = sum(int(item.get("completion_tokens") or 0) for item in metrics)
    return {
        "runs": len(values),
        "accepted": sum(bool(value.get("accepted")) for value in values),
        "llm_calls": sum(
            int(item.get("llm_call_count") or item.get("iterations") or 0)
            for item in metrics
        ),
        "zero_llm_runs": sum(
            not int(item.get("llm_call_count") or item.get("iterations") or 0)
            for item in metrics
        ),
        "tool_calls": sum(int(item.get("tool_call_count") or 0) for item in metrics),
        "final_repairs": sum(int(item.get("final_repair_count") or 0) for item in metrics),
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        },
        "duration_ms": {
            "total": sum(durations),
            "average": round(sum(durations) / len(durations)) if durations else 0,
            "p95": percentile(durations, 0.95),
        },
    }


def final_quality(
    values: dict[str, dict[str, Any]],
    references: dict[str, dict[str, str]],
) -> dict[str, Any]:
    stage_hits = action_hits = joint_hits = 0
    compared = 0
    for case_id, value in values.items():
        reference = references.get(case_id)
        if not reference:
            continue
        decision = value.get("decision") or {}
        action = str((decision.get("next") or {}).get("action") or "")
        stage_hit = decision.get("stage") == reference["stage"]
        action_hit = action == reference["action"]
        stage_hits += stage_hit
        action_hits += action_hit
        joint_hits += stage_hit and action_hit
        compared += 1
    return {
        "cases": compared,
        "stage_accuracy": rate(stage_hits, compared),
        "action_accuracy": rate(action_hits, compared),
        "stage_action_accuracy": rate(joint_hits, compared),
    }


def comparison(agent: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        "token_reduction": reduction(
            agent["tokens"]["total"],
            baseline["tokens"]["total"],
        ),
        "llm_call_reduction": reduction(agent["llm_calls"], baseline["llm_calls"]),
        "model_duration_reduction": reduction(
            agent["duration_ms"]["total"],
            baseline["duration_ms"]["total"],
        ),
        "note": "质量仅在每个会话最终时间点与双标注裁决参考集比较。",
    }


def reduction(current: int, baseline: int) -> float | None:
    if not baseline:
        return None
    return round(1 - current / baseline, 4)


def rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * quantile))))
    return ordered[index]


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    benchmark = report["benchmark"]
    agent = report["agent"]
    baseline = report["baseline"]
    compare = report["comparison"]
    utility = report["tool_utility"]
    external_enabled = bool(benchmark.get("external_mcp_enabled"))
    mcp_line = (
        "- 按生产策略开放 BOSS 会话刷新、岗位详情和公司研究 MCP，由 Planner 决定是否调用。"
        if external_enabled
        else "- 本轮通过 `--offline` 关闭外部 MCP，仅验证可重复的上下文管理。"
    )
    mcp_boundary = (
        "- 在线 MCP 结果受 BOSS 30 天窗口、岗位上下架和公开网页变化影响，"
        "工具成功率与决策质量分开报告。"
        if external_enabled
        else "- 外部 MCP 的可用性由独立真实集成实验验证。"
    )
    content = f"""# Capybot Apply 动态追踪 A/B 报告

> 使用脱敏真实 L1 数据，在隔离 PostgreSQL `{benchmark["database"]}` 中按发言轮次回放。
> 参考集为 LLM 双标注裁决集，不是人工 Gold Set。

## 实验设计

- 会话：{benchmark["conversation_count"]}；动态增量轮次：{benchmark["episode_count"]}。
- Agent：只输入本轮新增消息、上一轮机会投影，并按需读取 Memory/岗位/Skill。
- One-shot：每轮重新输入截至当前的全部消息、岗位卡和全部 Skill。
- 两组使用同一模型、JSON Schema 和 CommitGate。
{mcp_line}

## 结果

| 指标 | 增量 Agent | 全量 One-shot |
| --- | ---: | ---: |
| 最终阶段+行动一致率 | {_pct(agent["final_quality"]["stage_action_accuracy"])} | {_pct(baseline["final_quality"]["stage_action_accuracy"])} |
| LLM 调用数 | {agent["llm_calls"]} | {baseline["llm_calls"]} |
| Token | {agent["tokens"]["total"]} | {baseline["tokens"]["total"]} |
| 累计模型处理时间 | {agent["duration_ms"]["total"] / 1000:.3f}s | {baseline["duration_ms"]["total"] / 1000:.3f}s |
| 工具调用数 | {agent["tool_calls"]} | {baseline["tool_calls"]} |

- Token 减少 {_pct(compare["token_reduction"])}。
- LLM 调用减少 {_pct(compare["llm_call_reduction"])}。
- 累计模型处理时间减少 {_pct(compare["model_duration_reduction"])}。
- Agent 实际执行 {utility["calls"]} 次工具调用，拦截
  {utility.get("duplicate_prevented", 0)} 次重复请求，空结果
  {utility["empty_calls"]} 次；{utility["evidence_used_calls"]} 次调用的证据进入最终决策。

## 边界

- 质量指标只在每个会话最终时间点计算，中间状态没有冒充人工 Gold。
- 当前样本量有限，数字用于项目内对照，不宣称生产泛化准确率。
{mcp_boundary}
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"
