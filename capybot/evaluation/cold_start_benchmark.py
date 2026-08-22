"""Isolated cold-start replay and one-shot baseline for real local L1 data."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from json_repair import repair_json
from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine import make_url

from capybot.apply.agent_runtime.commit_gate import CommitGate
from capybot.apply.agent_runtime.model import OpenAIPlannerModel
from capybot.apply.agent_runtime.schema import decision_json_schema
from capybot.apply.agent_runtime.sdk_runtime import OpenAIAgentsLoop
from capybot.apply.agent_runtime.skills import ApplySkillLibrary
from capybot.apply.conversation_signals import ConversationSignals
from capybot.apply.normalizer import BossMessageNormalizer
from capybot.apply.opportunity_service import OpportunityAnalysisService
from capybot.apply.postgres import apply_database_url, upgrade_database
from capybot.apply.store import ApplyStore
from capybot.evaluation.gold_set import case_id_for_conversation, load_json

L1_TABLES = (
    "apply_accounts",
    "boss_conversations",
    "contacts",
    "boss_messages",
    "boss_job_cards",
    "candidate_profile",
    "job_preferences",
)
SAFE_DATABASE = re.compile(r"^capybot_apply_benchmark(?:_[a-z0-9_]+)?$")
IGNORED_MESSAGE_TYPES = {"platform_card", "system", "auto_followup"}


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    opportunity_id: str
    prompt: dict[str, Any]
    valid_refs: set[str]
    pending_hr_question_refs: set[str]
    reference: dict[str, str] | None


def benchmark_database_url(
    source_url: str | None = None,
    database_name: str = "capybot_apply_benchmark",
) -> str:
    if not SAFE_DATABASE.fullmatch(database_name):
        raise ValueError("基准数据库名必须以 capybot_apply_benchmark 开头")
    return make_url(source_url or apply_database_url()).set(
        database=database_name
    ).render_as_string(hide_password=False)


def reset_benchmark_database(target_url: str) -> None:
    target = make_url(target_url)
    database_name = str(target.database or "")
    if not SAFE_DATABASE.fullmatch(database_name):
        raise ValueError("拒绝重置非 benchmark 数据库")
    admin_url = target.set(database="postgres")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    finally:
        admin.dispose()
    upgrade_database(target_url)


def clone_real_l1(
    *,
    source_url: str,
    target_url: str,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Copy one account's raw evidence directly between databases."""

    source = create_engine(source_url, pool_pre_ping=True)
    target = create_engine(target_url, pool_pre_ping=True)
    source_meta = MetaData()
    target_meta = MetaData()
    source_meta.reflect(bind=source, only=list(L1_TABLES))
    target_meta.reflect(bind=target, only=list(L1_TABLES))
    counts: dict[str, int] = {}
    try:
        with source.connect() as source_connection:
            accounts = source_meta.tables["apply_accounts"]
            if account_id:
                account = source_connection.execute(
                    select(accounts).where(accounts.c.id == account_id)
                ).mappings().first()
            else:
                account = source_connection.execute(
                    select(accounts).order_by(
                        accounts.c.last_import_at.desc().nullslast(),
                        accounts.c.last_seen_at.desc(),
                    )
                ).mappings().first()
            if not account:
                raise RuntimeError("生产库中没有可回放的 BOSS 账号")
            selected_account_id = str(account["id"])
            with target.begin() as target_connection:
                for table_name in L1_TABLES:
                    source_table = source_meta.tables[table_name]
                    target_table = target_meta.tables[table_name]
                    if table_name == "apply_accounts":
                        rows = [dict(account)]
                    else:
                        rows = [
                            dict(row)
                            for row in source_connection.execute(
                                select(source_table).where(
                                    source_table.c.account_id == selected_account_id
                                )
                            ).mappings()
                        ]
                    if rows:
                        target_connection.execute(target_table.insert(), rows)
                    counts[table_name] = len(rows)
    finally:
        source.dispose()
        target.dispose()
    return {"account_id": selected_account_id, "counts": counts}


def bootstrap_opportunities(store: ApplyStore) -> list[str]:
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT id FROM boss_conversations WHERE account_id=? ORDER BY updated_at DESC",
            (store.current_account_id(),),
        ).fetchall()
    opportunity_ids: list[str] = []
    for row in rows:
        opportunity_ids.extend(store.ensure_opportunities_for_conversation(str(row["id"])))
    return list(dict.fromkeys(opportunity_ids))


def build_cases(store: ApplyStore, opportunity_ids: list[str]) -> list[BenchmarkCase]:
    return [
        case
        for opportunity_id in opportunity_ids
        if (case := _build_case(store, opportunity_id)) is not None
    ]


def _build_case(store: ApplyStore, opportunity_id: str) -> BenchmarkCase | None:
    context = store.opportunity_context(opportunity_id)
    opportunity = context.get("opportunity") or {}
    messages = [
        message
        for message in context.get("messages") or []
        if bool(message.get("is_human_message", 1))
        and str(message.get("message_type") or "text") not in IGNORED_MESSAGE_TYPES
    ]
    if not messages:
        return None
    message_payload = [
        {
            "ref": f"boss_message:{message['message_id']}",
            "speaker": "me" if bool(message.get("from_me")) else "hr",
            "type": str(message.get("message_type") or "text"),
            "content": _redact(str(message.get("text") or "")),
            "sent_at": message.get("sent_at"),
        }
        for message in messages[-40:]
    ]
    job_payload = []
    for job in (context.get("jobs") or [])[:3]:
        ref = f"boss_job_snapshot:{job['id']}"
        job_payload.append(
            {
                "ref": ref,
                "title": job.get("title"),
                "company": job.get("company"),
                "salary": job.get("salary"),
                "city": job.get("city"),
                "experience": job.get("experience"),
                "education": job.get("education"),
            }
        )
    valid_refs = {item["ref"] for item in message_payload + job_payload}
    latest = messages[-1]
    pending_refs = (
        {f"boss_message:{latest['message_id']}"}
        if not bool(latest.get("from_me"))
        and ConversationSignals.requires_reply(str(latest.get("text") or ""))
        else set()
    )
    skill_library = ApplySkillLibrary()
    skills = [
        {
            "name": name,
            "content": skill_library.load(name, scope="opportunity")["content"],
        }
        for name in skill_library.names("opportunity")
    ]
    return BenchmarkCase(
        case_id=case_id_for_conversation(
            str((context.get("conversations") or [{}])[0].get("id") or opportunity_id)
        ),
        opportunity_id=opportunity_id,
        prompt={
            "goal": "一次性读取全部已提供证据，直接判断当前机会阶段和下一步行动，不可调用工具",
            "opportunity": {
                "id": opportunity_id,
                "title": opportunity.get("title"),
                "company": opportunity.get("company"),
            },
            "messages": message_payload,
            "jobs": job_payload,
            "skills": skills,
            "pending_hr_question_refs": sorted(pending_refs),
        },
        valid_refs=valid_refs,
        pending_hr_question_refs=pending_refs,
        reference=_deterministic_reference(messages),
    )


class OneShotBaseline:
    """Same model, schema and CommitGate, but no planning or tools."""

    def __init__(self, model: OpenAIPlannerModel | None = None) -> None:
        self.model = model or OpenAIPlannerModel()
        self.gate = CommitGate()

    async def run(self, case: BenchmarkCase) -> dict[str, Any]:
        started = time.perf_counter()
        messages = [
            {
                "role": "system",
                "content": (
                    OpenAIAgentsLoop.SYSTEM_POLICY
                    + "\n\n这是一次性基线：不得调用工具，只能使用用户输入中的证据。"
                    + "\n最终 JSON Schema：\n"
                    + json.dumps(decision_json_schema(), ensure_ascii=False)
                ),
            },
            {"role": "user", "content": json.dumps(case.prompt, ensure_ascii=False)},
        ]
        prompt_tokens = 0
        completion_tokens = 0
        errors: list[str] = []
        result = None
        decision = None
        calls = 0
        for _ in range(2):
            calls += 1
            turn = await self.model.complete_json(
                messages,
                schema=decision_json_schema(),
                schema_name="capybot_cold_start_baseline",
            )
            prompt_tokens += turn.prompt_tokens
            completion_tokens += turn.completion_tokens
            try:
                decision = json.loads(repair_json(turn.content or ""))
                result = self.gate.validate(
                    decision,
                    valid_evidence_refs=case.valid_refs,
                    pending_hr_question_refs=case.pending_hr_question_refs,
                )
            except Exception as exc:
                errors = [str(exc)]
            else:
                errors = result.errors
                if result.accepted:
                    break
            messages.extend(
                [
                    {"role": "assistant", "content": turn.content or ""},
                    {
                        "role": "user",
                        "content": (
                            "只修正最终 JSON。CommitGate 错误："
                            + "；".join(errors)
                            + "。可用 evidence ref："
                            + json.dumps(sorted(case.valid_refs), ensure_ascii=False)
                        ),
                    },
                ]
            )
        return {
            "accepted": bool(result and result.accepted),
            "status": result.status if result else "rejected",
            "decision": result.decision if result else decision,
            "errors": errors,
            "metrics": {
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "llm_call_count": calls,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "tool_call_count": 0,
                "final_repair_count": max(0, calls - 1),
            },
        }


async def run_benchmark(
    *,
    source_url: str | None = None,
    target_url: str | None = None,
    limit: int | None = None,
    concurrency: int = 3,
    include_baseline: bool = True,
    reference_set_path: str | Path | None = None,
) -> dict[str, Any]:
    source_url = source_url or apply_database_url()
    target_url = target_url or benchmark_database_url(source_url)
    if make_url(source_url).database == make_url(target_url).database:
        raise ValueError("生产数据库和基准数据库不能相同")
    reset_benchmark_database(target_url)
    clone = clone_real_l1(source_url=source_url, target_url=target_url)
    store = ApplyStore(account_id=clone["account_id"], database_url=target_url)
    opportunity_ids = bootstrap_opportunities(store)
    if limit and limit > 0:
        opportunity_ids = opportunity_ids[:limit]
    cases = build_cases(store, opportunity_ids)
    reference_kind = "deterministic_high_confidence_silver"
    if reference_set_path:
        external_references = _load_external_references(reference_set_path)
        cases = [
            replace(case, reference=external_references.get(case.case_id))
            for case in cases
        ]
        reference_kind = "llm_double_annotated_adjudicated_reference"

    agent_service = OpportunityAnalysisService(store)
    baseline = OneShotBaseline(agent_service.model)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_agent(case: BenchmarkCase) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                value = await agent_service.analyze(
                    case.opportunity_id,
                    trigger={"type": "cold_start", "allow_external": False},
                )
            except Exception as exc:
                value = {"accepted": False, "errors": [str(exc)], "metrics": {}}
            return case.opportunity_id, value

    async def run_baseline(case: BenchmarkCase) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            try:
                value = await baseline.run(case)
            except Exception as exc:
                value = {"accepted": False, "errors": [str(exc)], "metrics": {}}
            return case.opportunity_id, value

    agent_values = dict(await asyncio.gather(*(run_agent(case) for case in cases)))
    baseline_values = (
        dict(await asyncio.gather(*(run_baseline(case) for case in cases)))
        if include_baseline
        else {}
    )
    references = {
        case.opportunity_id: case.reference for case in cases if case.reference
    }
    interactive_ids = {
        opportunity_id
        for opportunity_id, value in agent_values.items()
        if (value.get("metrics") or {}).get("routing_mode")
        != "cold_projection"
    }
    interactive_agent = {
        opportunity_id: value
        for opportunity_id, value in agent_values.items()
        if opportunity_id in interactive_ids
    }
    interactive_baseline = {
        opportunity_id: value
        for opportunity_id, value in baseline_values.items()
        if opportunity_id in interactive_ids
    }
    interactive_references = {
        opportunity_id: value
        for opportunity_id, value in references.items()
        if opportunity_id in interactive_ids
    }
    return {
        "benchmark": {
            "kind": "isolated_real_l1_cold_start",
            "case_count": len(cases),
            "reference_case_count": len(references),
            "reference_kind": reference_kind,
            "database": str(make_url(target_url).database),
            "external_mcp_enabled": False,
            "agent_strategy": "decision_router+bounded_tool_calling",
        },
        "source_snapshot": clone["counts"],
        "agent": _aggregate(agent_values, references),
        "agent_interactive_subset": _aggregate(
            interactive_agent,
            interactive_references,
        ),
        "baseline": _aggregate(baseline_values, references) if include_baseline else None,
        "baseline_interactive_subset": (
            _aggregate(interactive_baseline, interactive_references)
            if include_baseline
            else None
        ),
        "comparison": _compare(
            agent_values,
            baseline_values,
            references,
            reference_kind=reference_kind,
        )
        if include_baseline
        else None,
        "comparison_interactive_subset": (
            _compare(
                interactive_agent,
                interactive_baseline,
                interactive_references,
                reference_kind=reference_kind,
            )
            if include_baseline
            else None
        ),
        "tool_utility": _tool_utility_summary(store),
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_markdown_report(report: dict[str, Any], path: Path) -> None:
    agent = report["agent"]
    baseline = report.get("baseline") or {}
    interactive = report["agent_interactive_subset"]
    baseline_interactive = report.get("baseline_interactive_subset") or {}
    benchmark = report["benchmark"]
    source = report["source_snapshot"]
    utility = report.get("tool_utility") or {}
    reference_kind = benchmark.get("reference_kind")
    reference_label = (
        "5.6-sol 双标注裁决参考集"
        if reference_kind == "llm_double_annotated_adjudicated_reference"
        else "高置信规则银标"
    )
    llm_reduction = _reduction(agent.get("llm_calls"), baseline.get("llm_calls"))
    token_reduction = _reduction(
        (agent.get("tokens") or {}).get("total"),
        (baseline.get("tokens") or {}).get("total"),
    )
    duration_reduction = _reduction(
        (agent.get("duration_ms") or {}).get("total"),
        (baseline.get("duration_ms") or {}).get("total"),
    )
    content = f"""# Capybot Apply 真实 L1 冷启动 A/B 报告

> 数据源为本人本机 PostgreSQL 中的真实 BOSS L1 证据；报告不包含聊天正文、简历、
> Cookie、Prompt 或 API Key。所有运行都在隔离数据库
> `{benchmark["database"]}` 中完成，生产库只读。

## 数据与方法

- 原始数据：{source["boss_conversations"]} 个会话、{source["boss_messages"]} 条消息、
  {source["boss_job_cards"]} 个岗位卡。
- 有效冷启动机会：{benchmark["case_count"]}；{reference_label}：
  {benchmark["reference_case_count"]}。
- 实验组：DecisionRouter + 受限 Tool-Calling Agent。
- 基线组：同模型、同 Schema、同 CommitGate，一次性预装上下文且不调用工具。
- 为保证可复现，本基准关闭外部 MCP；因此本报告不证明外部网页或 BOSS MCP 的质量。

## 结果

| 指标 | 混合 Agent | 一次性基线 |
| --- | ---: | ---: |
| CommitGate 通过率 | {_pct(agent["acceptance_rate"])} | {_pct(baseline.get("acceptance_rate"))} |
| 参考集阶段+行动一致率 | {_pct(agent["stage_action_accuracy"])} | {_pct(baseline.get("stage_action_accuracy"))} |
| LLM 调用 | {agent["llm_calls"]} | {baseline.get("llm_calls")} |
| Token | {agent["tokens"]["total"]} | {(baseline.get("tokens") or {}).get("total")} |
| 累计模型处理时间 | {agent["duration_ms"]["total"] / 1000:.3f}s | {(baseline.get("duration_ms") or {}).get("total", 0) / 1000:.3f}s |
| 首轮通过率 | {_pct(agent["first_pass_acceptance_rate"])} | {_pct(baseline.get("first_pass_acceptance_rate"))} |

- {agent["cold_outreach_routed"]}/{agent["runs"]} 个单向冷会话由确定性 Router
  以 0 LLM 投影，避免把“没有 HR 回复”误判成进展。
- 相比基线，LLM 调用减少 {_pct(llm_reduction)}，Token 减少
  {_pct(token_reduction)}，累计模型处理时间减少 {_pct(duration_reduction)}。

## 核心交互子集

剔除 0 LLM 冷会话后，剩余 {interactive["runs"]} 个含真实 HR 互动的机会：

- Tool Agent：{interactive["accepted"]}/{interactive["runs"]} 通过 CommitGate；
  {interactive["reference_coverage"]} 个参考集阶段+行动一致率
  {_pct(interactive["stage_action_accuracy"])}。
- 一次性基线：{baseline_interactive.get("accepted")}/{baseline_interactive.get("runs")}
  通过；参考集一致率 {_pct(baseline_interactive.get("stage_action_accuracy"))}。
- Tool Agent 首轮通过率 {_pct(interactive["first_pass_acceptance_rate"])}，
  P50 {interactive["duration_ms"]["p50"] / 1000:.3f}s；仍有模型延迟与修复空间。

## 工具价值

- 共 {utility.get("calls", 0)} 次工具调用；
  {utility.get("evidence_used_calls", 0)} 次返回证据进入最终决策。
- 返回 {utility.get("novel_evidence", 0)} 条新增证据，采用
  {utility.get("used_evidence", 0)} 条。
- Skill 调用属于规则/路由上下文，不伪装成消息证据。

## 解释边界

- Reference Set 类型：`{reference_kind}`。即使使用 5.6-sol 双标注裁决，
  仍不是人工 Gold Set，不应表述为真实生产准确率。
- 交互子集仅 {interactive["runs"]} 条，不能宣称生产泛化率。
- 总体 100% 包含确定性 Router；面试时必须同时报告交互子集。
- 外部 MCP 被关闭，MCP 成功/失败证据应引用独立集成测试与生产 Trace。
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _tool_utility_summary(store: ApplyStore) -> dict[str, Any]:
    with store.connect() as connection:
        rows = connection.execute(
            """
            SELECT tool_name, status, fact_count, novel_evidence_count,
                   used_evidence_count, empty_result, utility
            FROM tool_observations
            """
        ).fetchall()
    duplicate_rows = [row for row in rows if str(row["status"]) == "duplicate"]
    executed_rows = [row for row in rows if str(row["status"]) != "duplicate"]
    return {
        "calls": len(executed_rows),
        "requested_calls": len(rows),
        "duplicate_prevented": len(duplicate_rows),
        "evidence_used_calls": sum(
            str(row["utility"] or "") == "evidence_used" for row in executed_rows
        ),
        "empty_calls": sum(bool(row["empty_result"]) for row in executed_rows),
        "novel_evidence": sum(
            int(row["novel_evidence_count"] or 0) for row in executed_rows
        ),
        "used_evidence": sum(
            int(row["used_evidence_count"] or 0) for row in executed_rows
        ),
        "by_tool": dict(
            sorted(
                {
                    str(name): sum(
                        str(row["tool_name"]) == str(name) for row in executed_rows
                    )
                    for name in {row["tool_name"] for row in executed_rows}
                }.items()
            )
        ),
    }


def _aggregate(
    values: dict[str, dict[str, Any]],
    references: dict[str, dict[str, str]],
) -> dict[str, Any]:
    accepted = [value for value in values.values() if value.get("accepted")]
    durations = [
        int((value.get("metrics") or {}).get("duration_ms") or 0)
        for value in values.values()
    ]
    prompt_tokens = sum(
        int((value.get("metrics") or {}).get("prompt_tokens") or 0)
        for value in values.values()
    )
    completion_tokens = sum(
        int((value.get("metrics") or {}).get("completion_tokens") or 0)
        for value in values.values()
    )
    reference_results = []
    for opportunity_id, reference in references.items():
        decision = values.get(opportunity_id, {}).get("decision") or {}
        next_step = decision.get("next") or {}
        reference_results.append(
            {
                "stage": decision.get("stage") == reference["stage"],
                "action": next_step.get("action") == reference["action"],
            }
        )
    return {
        "runs": len(values),
        "accepted": len(accepted),
        "acceptance_rate": _rate(len(accepted), len(values)),
        "reference_coverage": len(reference_results),
        "stage_accuracy": _rate(
            sum(item["stage"] for item in reference_results),
            len(reference_results),
        ),
        "action_accuracy": _rate(
            sum(item["action"] for item in reference_results),
            len(reference_results),
        ),
        "stage_action_accuracy": _rate(
            sum(item["stage"] and item["action"] for item in reference_results),
            len(reference_results),
        ),
        "duration_ms": {
            "p50": _percentile(durations, 0.5),
            "p95": _percentile(durations, 0.95),
            "total": sum(durations),
        },
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        },
        "llm_calls": sum(
            int(
                (value.get("metrics") or {}).get("llm_call_count")
                or (value.get("metrics") or {}).get("iterations")
                or 0
            )
            for value in values.values()
        ),
        "zero_llm_runs": sum(
            not int(
                (value.get("metrics") or {}).get("llm_call_count")
                or (value.get("metrics") or {}).get("iterations")
                or 0
            )
            for value in values.values()
        ),
        "cold_outreach_routed": sum(
            (value.get("metrics") or {}).get("routing_mode")
            == "cold_projection"
            for value in values.values()
        ),
        "tool_calls": sum(
            int((value.get("metrics") or {}).get("tool_call_count") or 0)
            for value in values.values()
        ),
        "final_repairs": sum(
            int((value.get("metrics") or {}).get("final_repair_count") or 0)
            for value in values.values()
        ),
        "first_pass_acceptance_rate": _rate(
            sum(
                bool(value.get("accepted"))
                and not int(
                    (value.get("metrics") or {}).get("final_repair_count") or 0
                )
                for value in values.values()
            ),
            len(values),
        ),
        "failed_error_groups": _error_groups(values),
    }


def _compare(
    agent: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]],
    references: dict[str, dict[str, str]],
    *,
    reference_kind: str = "deterministic_high_confidence_silver",
) -> dict[str, Any]:
    wins = losses = ties = 0
    for opportunity_id, reference in references.items():
        agent_score = _reference_score(agent.get(opportunity_id), reference)
        baseline_score = _reference_score(baseline.get(opportunity_id), reference)
        if agent_score > baseline_score:
            wins += 1
        elif agent_score < baseline_score:
            losses += 1
        else:
            ties += 1
    return {
        "agent_wins": wins,
        "baseline_wins": losses,
        "ties": ties,
        "note": (
            "Reference Set 是 LLM 双标注裁决参考集，不是人工 Gold Set。"
            if reference_kind == "llm_double_annotated_adjudicated_reference"
            else "Reference Set 是代码生成的高置信银标，不是人工 Gold Set。"
        ),
    }


def _load_external_references(path: str | Path) -> dict[str, dict[str, str]]:
    payload = load_json(path)
    if payload.get("dataset_kind") != "llm_double_annotated_adjudicated_reference":
        raise ValueError("只接受经过双标注裁决的外部参考集")
    references: dict[str, dict[str, str]] = {}
    for item in payload.get("annotations", []):
        if not item.get("include"):
            continue
        case_id = str(item.get("case_id") or "")
        stage = str(item.get("stage") or "")
        action = str(item.get("action") or "")
        if not case_id or not stage or not action:
            raise ValueError("参考集包含不完整标签")
        references[case_id] = {"stage": stage, "action": action}
    return references


def _reference_score(value: dict[str, Any] | None, reference: dict[str, str]) -> int:
    decision = (value or {}).get("decision") or {}
    next_step = decision.get("next") or {}
    return int(decision.get("stage") == reference["stage"]) + int(
        next_step.get("action") == reference["action"]
    )


def _deterministic_reference(
    messages: list[dict[str, Any]] | dict[str, Any],
) -> dict[str, str] | None:
    values = messages if isinstance(messages, list) else [messages]
    values = [
        message
        for message in values
        if not any(
            marker in str(message.get("text") or "")
            for marker in BossMessageNormalizer.PLATFORM_CARD_MARKERS
        )
    ]
    if not values:
        return None
    real_hr = [message for message in values if not bool(message.get("from_me"))]
    if not real_hr:
        return {"stage": "discovered", "action": "wait"}
    latest = values[-1]
    latest_hr = real_hr[-1]
    latest_hr_text = str(latest_hr.get("text") or "")
    if re.search(
        r"不合适|不匹配|不完全吻合|不完全一致|暂不考虑|岗位已关闭|已经招满|流程结束",
        latest_hr_text,
    ):
        return {"stage": "closed", "action": "close"}
    if bool(latest.get("from_me")):
        return {"stage": "waiting_feedback", "action": "wait"}
    if re.search(r"简历|作品集|项目材料|发一份|附件", latest_hr_text):
        return {"stage": "need_my_action", "action": "send_material"}
    if re.search(r"面试|约个时间|几点方便|会议链接", latest_hr_text):
        return {"stage": "interviewing", "action": "confirm_interview"}
    if ConversationSignals.requires_reply(latest_hr_text):
        return {"stage": "need_my_action", "action": "reply"}
    if re.search(r"收到|已转交|帮你推|等反馈|后续联系", latest_hr_text):
        return {"stage": "waiting_feedback", "action": "wait"}
    return None


def _redact(value: str) -> str:
    output = value
    for pattern, replacement in (
        (r"1[3-9]\d{9}", "[手机号]"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱]"),
        (r"https?://\S+", "[链接]"),
        (r"\b\d{17}[\dXx]\b", "[身份证]"),
    ):
        output = re.sub(pattern, replacement, output)
    return output


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _reduction(value: int | None, baseline: int | None) -> float | None:
    if value is None or not baseline:
        return None
    return max(0.0, 1 - value / baseline)


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _percentile(values: list[int], quantile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return ordered[index]


def _error_groups(values: dict[str, dict[str, Any]]) -> dict[str, int]:
    groups: dict[str, int] = {}
    for value in values.values():
        if value.get("accepted"):
            continue
        error = str((value.get("errors") or ["unknown"])[0])
        key = error.splitlines()[0][:160]
        groups[key] = groups.get(key, 0) + 1
    return dict(sorted(groups.items(), key=lambda item: (-item[1], item[0])))
