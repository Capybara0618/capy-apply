"""Local, privacy-preserving reference-set preparation and adjudication."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import MetaData, create_engine, select

from capybot.apply.postgres import apply_database_url

STAGES = {
    "discovered",
    "communicating",
    "need_my_action",
    "waiting_feedback",
    "interviewing",
    "closed",
}
ACTIONS = {
    "reply",
    "send_material",
    "wait",
    "follow_up",
    "confirm_interview",
    "prepare_interview",
    "verify",
    "close",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
IGNORED_TYPES = {"platform_card", "system", "auto_followup"}


def export_annotation_source(
    output: str | Path,
    *,
    database_url: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Export one account's conversations without names, raw payloads, or secrets."""

    engine = create_engine(database_url or apply_database_url(), pool_pre_ping=True)
    metadata = MetaData()
    metadata.reflect(
        bind=engine,
        only=[
            "apply_accounts",
            "boss_conversations",
            "boss_messages",
            "boss_job_cards",
        ],
    )
    accounts = metadata.tables["apply_accounts"]
    conversations = metadata.tables["boss_conversations"]
    messages = metadata.tables["boss_messages"]
    jobs = metadata.tables["boss_job_cards"]
    try:
        with engine.connect() as connection:
            account = _select_account(connection, accounts, account_id)
            if not account:
                raise RuntimeError("PostgreSQL 中没有可标注的 BOSS 账号")
            selected_account_id = str(account["id"])
            conversation_rows = connection.execute(
                select(conversations)
                .where(conversations.c.account_id == selected_account_id)
                .order_by(
                    conversations.c.last_message_at.asc().nullsfirst(),
                    conversations.c.id.asc(),
                )
            ).mappings()
            cases = [
                _build_source_case(
                    connection,
                    messages,
                    jobs,
                    selected_account_id,
                    dict(conversation),
                )
                for conversation in conversation_rows
            ]
    finally:
        engine.dispose()

    payload = {
        "schema_version": 1,
        "dataset_kind": "llm_adjudicated_reference_source",
        "account_ref": case_id_for_conversation(selected_account_id),
        "annotation_policy": {
            "snapshot_rule": "仅依据当前快照中已出现的消息，不推测 HR 内心或未来结果",
            "exclude_rule": "纯平台消息、BOSS 助手或没有有效人类消息的会话应排除",
            "cold_rule": "只有候选人发言且 HR 未回复时，不得标记为已拒绝",
            "ambiguity_rule": "证据不足时降低 confidence，不得编造事实",
        },
        "cases": cases,
    }
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def validate_annotations(
    source: dict[str, Any],
    annotations: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate annotation coverage, labels, and canonical evidence references."""

    values = annotations.get("annotations", []) if isinstance(annotations, dict) else annotations
    if not isinstance(values, list):
        raise ValueError("annotations 必须是数组")
    cases = {str(case["case_id"]): case for case in source.get("cases", [])}
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw in enumerate(values):
        if not isinstance(raw, dict):
            errors.append(f"annotations[{index}] 不是对象")
            continue
        case_id = str(raw.get("case_id") or "")
        if case_id not in cases:
            errors.append(f"未知 case_id: {case_id or '<empty>'}")
            continue
        if case_id in seen:
            errors.append(f"重复 case_id: {case_id}")
            continue
        seen.add(case_id)
        include = bool(raw.get("include"))
        stage = raw.get("stage")
        action = raw.get("action")
        confidence = str(raw.get("confidence") or "")
        evidence = [str(value) for value in raw.get("evidence", [])]
        reason = str(raw.get("reason") or "").strip()
        valid_refs = {
            str(item["ref"])
            for item in cases[case_id].get("messages", [])
            + cases[case_id].get("jobs", [])
        }
        if confidence not in CONFIDENCE_LEVELS:
            errors.append(f"{case_id}: confidence 非法")
        if not reason:
            errors.append(f"{case_id}: reason 为空")
        if include:
            if stage not in STAGES:
                errors.append(f"{case_id}: stage 非法")
            if action not in ACTIONS:
                errors.append(f"{case_id}: action 非法")
            if not evidence:
                errors.append(f"{case_id}: 纳入样本必须引用证据")
        elif stage is not None or action is not None:
            errors.append(f"{case_id}: 排除样本的 stage/action 必须为 null")
        unknown_refs = sorted(set(evidence) - valid_refs)
        if unknown_refs:
            errors.append(f"{case_id}: 引用了不存在的证据 {unknown_refs}")
        normalized.append(
            {
                "case_id": case_id,
                "include": include,
                "stage": stage,
                "action": action,
                "confidence": confidence,
                "evidence": evidence,
                "reason": reason,
            }
        )
    missing = sorted(set(cases) - seen)
    if missing:
        errors.append(f"缺少 {len(missing)} 个 case: {missing}")
    return {
        "ok": not errors,
        "errors": errors,
        "annotations": sorted(normalized, key=lambda item: item["case_id"]),
    }


def compare_annotators(
    source: dict[str, Any],
    annotator_a: dict[str, Any] | list[dict[str, Any]],
    annotator_b: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Return agreement metrics and only the cases requiring adjudication."""

    checked_a = validate_annotations(source, annotator_a)
    checked_b = validate_annotations(source, annotator_b)
    if not checked_a["ok"] or not checked_b["ok"]:
        raise ValueError(
            json.dumps(
                {"annotator_a": checked_a["errors"], "annotator_b": checked_b["errors"]},
                ensure_ascii=False,
            )
        )
    values_a = {item["case_id"]: item for item in checked_a["annotations"]}
    values_b = {item["case_id"]: item for item in checked_b["annotations"]}
    disagreements: list[dict[str, Any]] = []
    primary_matches = stage_matches = action_matches = 0
    for case_id in sorted(values_a):
        a = values_a[case_id]
        b = values_b[case_id]
        include_match = a["include"] == b["include"]
        stage_match = a["stage"] == b["stage"]
        action_match = a["action"] == b["action"]
        primary_match = include_match and stage_match and action_match
        primary_matches += int(primary_match)
        stage_matches += int(stage_match)
        action_matches += int(action_match)
        if not primary_match:
            disagreements.append({"case_id": case_id, "annotator_a": a, "annotator_b": b})
    count = len(values_a)
    return {
        "case_count": count,
        "primary_agreement": _rate(primary_matches, count),
        "stage_agreement": _rate(stage_matches, count),
        "action_agreement": _rate(action_matches, count),
        "stage_kappa": _cohen_kappa(
            [str(values_a[key]["stage"]) for key in sorted(values_a)],
            [str(values_b[key]["stage"]) for key in sorted(values_b)],
        ),
        "action_kappa": _cohen_kappa(
            [str(values_a[key]["action"]) for key in sorted(values_a)],
            [str(values_b[key]["action"]) for key in sorted(values_b)],
        ),
        "disagreements": disagreements,
    }


def finalize_reference_set(
    source: dict[str, Any],
    annotator_a: dict[str, Any] | list[dict[str, Any]],
    annotator_b: dict[str, Any] | list[dict[str, Any]],
    adjudication: dict[str, Any] | list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge exact consensus labels with independently adjudicated disagreements."""

    comparison = compare_annotators(source, annotator_a, annotator_b)
    checked_a = validate_annotations(source, annotator_a)
    values_a = {item["case_id"]: item for item in checked_a["annotations"]}
    disputed_ids = {item["case_id"] for item in comparison["disagreements"]}
    adjudication_values = (
        adjudication.get("annotations", [])
        if isinstance(adjudication, dict)
        else adjudication
    )
    adjudicated = {
        str(item.get("case_id")): item
        for item in adjudication_values
        if isinstance(item, dict)
    }
    if set(adjudicated) != disputed_ids:
        raise ValueError(
            "裁决集必须且只能覆盖分歧 case："
            f"missing={sorted(disputed_ids - set(adjudicated))}, "
            f"extra={sorted(set(adjudicated) - disputed_ids)}"
        )
    final_values = []
    for case_id in sorted(values_a):
        if case_id in disputed_ids:
            final_values.append(adjudicated[case_id])
        else:
            final_values.append(values_a[case_id])
    checked_final = validate_annotations(source, final_values)
    if not checked_final["ok"]:
        raise ValueError(json.dumps(checked_final["errors"], ensure_ascii=False))
    included = [item for item in checked_final["annotations"] if item["include"]]
    return {
        "schema_version": 1,
        "dataset_kind": "llm_double_annotated_adjudicated_reference",
        "provenance": {
            "is_human_gold": False,
            "annotation_method": "two independent gpt-5.6-sol passes plus gpt-5.6-sol adjudication",
            "human_review_required_for_gold": True,
        },
        "metrics": {
            **{key: value for key, value in comparison.items() if key != "disagreements"},
            "adjudicated_cases": len(disputed_ids),
            "included_cases": len(included),
            "excluded_cases": len(checked_final["annotations"]) - len(included),
            "high_confidence_included": sum(
                item["confidence"] == "high" for item in included
            ),
        },
        "annotations": checked_final["annotations"],
    }


def load_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 必须包含 JSON 对象")
    return value


def write_json(value: dict[str, Any], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_reference_report(value: dict[str, Any], path: str | Path) -> None:
    metrics = value["metrics"]
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"""# Capybot Apply 双标注裁决参考集

## 方法

- 数据来自本机 PostgreSQL 中 41 个真实 BOSS 会话快照。
- 两个 `gpt-5.6-sol` 标注者仅依据脱敏 L1 证据独立标注。
- 第三个 `gpt-5.6-sol` 只裁决主标签不一致的样本。
- 证据引用经过程序校验，必须存在于对应会话。
- 报告不包含聊天正文、联系人姓名、Cookie、简历或 API Key。

## 结果

| 指标 | 结果 |
| --- | ---: |
| 总会话 | {metrics["case_count"]} |
| 纳入评测 | {metrics["included_cases"]} |
| 排除会话 | {metrics["excluded_cases"]} |
| 主标签完全一致率 | {_format_percent(metrics["primary_agreement"])} |
| 阶段一致率 | {_format_percent(metrics["stage_agreement"])} |
| 行动一致率 | {_format_percent(metrics["action_agreement"])} |
| 阶段 Cohen's Kappa | {metrics["stage_kappa"]} |
| 行动 Cohen's Kappa | {metrics["action_kappa"]} |
| 第三轮裁决 | {metrics["adjudicated_cases"]} |
| 高置信纳入样本 | {metrics["high_confidence_included"]} |

## 解释边界

这是一套 **LLM 双标注裁决参考集**，不是人工 Gold Set。它比单条规则生成的银标更独立，
适合发现 Agent 与强模型判断的差异；但三位标注者仍属于同一模型家族，可能共享系统性偏差。
只有经过人工抽检或第二位人类标注者复核后，才应在简历中称为 Gold Set。
""",
        encoding="utf-8",
    )


def _select_account(connection: Any, accounts: Any, account_id: str | None) -> Any:
    statement = select(accounts)
    if account_id:
        statement = statement.where(accounts.c.id == account_id)
    else:
        statement = statement.order_by(
            accounts.c.last_import_at.desc().nullslast(),
            accounts.c.last_seen_at.desc().nullslast(),
        )
    return connection.execute(statement).mappings().first()


def _build_source_case(
    connection: Any,
    messages_table: Any,
    jobs_table: Any,
    account_id: str,
    conversation: dict[str, Any],
) -> dict[str, Any]:
    conversation_id = str(conversation["id"])
    message_rows = connection.execute(
        select(messages_table)
        .where(
            messages_table.c.account_id == account_id,
            messages_table.c.conversation_id == conversation_id,
        )
        .order_by(
            messages_table.c.sent_at.asc().nullsfirst(),
            messages_table.c.created_at.asc(),
            messages_table.c.id.asc(),
        )
    ).mappings()
    job_rows = connection.execute(
        select(jobs_table)
        .where(
            jobs_table.c.account_id == account_id,
            jobs_table.c.conversation_id == conversation_id,
        )
        .order_by(jobs_table.c.created_at.asc(), jobs_table.c.id.asc())
    ).mappings()
    messages = [
        {
            "ref": f"boss_message:{row['message_id']}",
            "speaker": "me" if bool(row["from_me"]) else "hr",
            "speaker_confidence": round(float(row["from_me_confidence"] or 0), 3),
            "type": str(row["message_type"] or "unknown"),
            "human": bool(row["is_human_message"]),
            "content": _redact(str(row["text"] or "")),
            "sent_at": _json_value(row["sent_at"]),
        }
        for row in message_rows
    ]
    jobs = [
        {
            "ref": f"boss_job_snapshot:{row['id']}",
            "title": _redact(str(row["title"] or "")),
            "salary": _redact(str(row["salary"] or "")),
            "city": _redact(str(row["city"] or "")),
            "experience": _redact(str(row["experience"] or "")),
            "education": _redact(str(row["education"] or "")),
        }
        for row in job_rows
    ]
    human_messages = [
        item
        for item in messages
        if item["human"] and item["type"] not in IGNORED_TYPES
    ]
    return {
        "case_id": case_id_for_conversation(conversation_id),
        "conversation_ref": case_id_for_conversation(conversation_id),
        "last_message_at": _json_value(conversation.get("last_message_at")),
        "source_stats": {
            "messages": len(messages),
            "human_messages": len(human_messages),
            "jobs": len(jobs),
        },
        "messages": messages,
        "jobs": jobs,
    }


def _redact(value: str) -> str:
    output = value
    patterns = (
        (r"1[3-9]\d{9}", "[手机号]"),
        (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱]"),
        (r"https?://\S+", "[链接]"),
        (r"\b\d{17}[\dXx]\b", "[身份证]"),
        (r"(?i)(微信|wx|wechat)\s*[:：]?\s*[A-Za-z0-9_-]{5,}", r"\1：[已脱敏]"),
    )
    for pattern, replacement in patterns:
        output = re.sub(pattern, replacement, output)
    return output[:2000]


def case_id_for_conversation(value: str) -> str:
    """Return the stable opaque identifier shared by export and replay."""

    return "case_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _json_value(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _format_percent(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def _cohen_kappa(a: Iterable[str], b: Iterable[str]) -> float | None:
    values_a = list(a)
    values_b = list(b)
    if len(values_a) != len(values_b) or not values_a:
        return None
    observed = sum(left == right for left, right in zip(values_a, values_b)) / len(
        values_a
    )
    counts_a = Counter(values_a)
    counts_b = Counter(values_b)
    labels = set(counts_a) | set(counts_b)
    expected = sum(
        (counts_a[label] / len(values_a)) * (counts_b[label] / len(values_b))
        for label in labels
    )
    if expected == 1:
        return 1.0
    return round((observed - expected) / (1 - expected), 4)
