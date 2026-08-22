"""Benchmark the read paths used by the Apply WebUI.

The benchmark calls the same payload functions as the HTTP gateway while
excluding browser networking and rendering. It is safe to run against real
data because it performs no writes and exports only aggregate timings.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from capybot.webui.apply_api import (
    apply_agent_runs,
    apply_health_payload,
    apply_jobs_payload,
    apply_opportunities,
    apply_overview,
    apply_tasks,
)

ReadPath = Callable[[], dict[str, Any]]


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] * (1 - (index - lower)) + ordered[upper] * (index - lower)


def benchmark(read: ReadPath, *, warmup: int, runs: int) -> dict[str, float | int]:
    for _ in range(warmup):
        read()
    samples: list[float] = []
    for _ in range(runs):
        started = time.perf_counter()
        read()
        samples.append((time.perf_counter() - started) * 1000)
    return {
        "samples": len(samples),
        "min_ms": round(min(samples), 3),
        "average_ms": round(statistics.mean(samples), 3),
        "p50_ms": round(percentile(samples, 0.5), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "max_ms": round(max(samples), 3),
    }


def collect(*, warmup: int, runs: int) -> dict[str, Any]:
    paths: dict[str, ReadPath] = {
        "overview": apply_overview,
        "opportunities": apply_opportunities,
        "tasks": apply_tasks,
        "agent_runs": apply_agent_runs,
        "jobs": apply_jobs_payload,
        "health_cached": apply_health_payload,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Gateway payload read paths; excludes network and browser rendering.",
        "warmup_runs": warmup,
        "measured_runs": runs,
        "results": {
            name: benchmark(read, warmup=warmup, runs=runs) for name, read in paths.items()
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Capybot Apply 读路径性能基准",
        "",
        f"生成时间：`{payload['generated_at']}`",
        "",
        f"> {payload['scope']}",
        "",
        "| 读路径 | 样本 | P50(ms) | P95(ms) | 平均(ms) | 最大(ms) |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, result in payload["results"].items():
        lines.append(
            f"| {name} | {result['samples']} | {result['p50_ms']} | "
            f"{result['p95_ms']} | {result['average_ms']} | {result['max_ms']} |"
        )
    lines.extend(
        [
            "",
            "说明：首次进程启动、浏览器网络和 React 渲染不计入本报告；"
            "该报告用于比较 PostgreSQL 查询与 Gateway 聚合逻辑的版本回归。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    args = parser.parse_args()
    if args.warmup < 0 or args.runs < 1:
        parser.error("--warmup must be >= 0 and --runs must be >= 1")
    payload = collect(warmup=args.warmup, runs=args.runs)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    markdown = render_markdown(payload)
    if args.markdown_output:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
