"""Deterministic benchmark for raw BOSS message normalization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from capybot.apply.normalizer import BossMessageNormalizer

DEFAULT_NORMALIZER_CASES_PATH = Path(__file__).with_name("normalizer_eval_cases_zh.json")


def run_normalizer_eval(cases_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(cases_path) if cases_path else DEFAULT_NORMALIZER_CASES_PATH
    raw = path.read_bytes()
    cases = json.loads(raw.decode("utf-8"))
    if not isinstance(cases, list):
        raise ValueError("Normalizer eval dataset must be a JSON array.")

    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        normalized = BossMessageNormalizer.normalize(
            f"normalizer-eval-{index}",
            dict(case.get("raw") or {}),
            identity_hints=dict(case.get("identity_hints") or {}),
        )
        type_ok = normalized["message_type"] == case.get("expected_type")
        direction_ok = normalized["from_me"] is bool(case.get("expected_from_me"))
        results.append(
            {
                "name": case.get("name"),
                "type_ok": type_ok,
                "direction_ok": direction_ok,
                "ok": type_ok and direction_ok,
                "expected_type": case.get("expected_type"),
                "actual_type": normalized["message_type"],
                "expected_from_me": bool(case.get("expected_from_me")),
                "actual_from_me": normalized["from_me"],
                "from_me_confidence": normalized["from_me_confidence"],
            }
        )

    total = len(results)
    return {
        "dataset": {
            "path": str(path),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "cases": total,
        },
        "passed": sum(row["ok"] for row in results),
        "total": total,
        "metrics": {
            "message_type_accuracy": _rate(results, "type_ok"),
            "direction_accuracy": _rate(results, "direction_ok"),
            "joint_accuracy": _rate(results, "ok"),
        },
        "failures": [row for row in results if not row["ok"]],
        "results": results,
    }


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    if not rows:
        return None
    return round(sum(bool(row.get(key)) for row in rows) / len(rows), 4)
