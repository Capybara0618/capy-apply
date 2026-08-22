"""Command line entry points for the standalone Capybot Apply product."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import typer
from rich.console import Console
from rich.table import Table

from capybot import __version__

app = typer.Typer(
    name="capybot",
    help="Capybot Apply：证据驱动的 BOSS 求职机会 Agent",
    no_args_is_help=True,
    invoke_without_command=True,
)
apply_app = typer.Typer(help="启动、同步和运行机会 Agent")
db_app = typer.Typer(help="PostgreSQL schema 管理")
app.add_typer(apply_app, name="apply")
app.add_typer(db_app, name="db")
console = Console()


def _running_apply_url(port: int) -> str | None:
    url = f"http://127.0.0.1:{port}"
    try:
        with urlopen(f"{url}/health", timeout=0.8) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return None
    return url if payload.get("service") == "capybot-apply" else None


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="显示版本",
        is_eager=True,
    ),
) -> None:
    if version:
        console.print(f"Capybot Apply {__version__}")
        raise typer.Exit()


@db_app.command("upgrade")
def db_upgrade() -> None:
    """创建或升级 Apply PostgreSQL schema。"""

    from capybot.apply.postgres import apply_database_url, upgrade_database

    upgrade_database()
    console.print("[green]PostgreSQL schema 已就绪[/green]")
    console.print(f"[dim]{apply_database_url()}[/dim]")


@apply_app.command("configure")
def configure(
    api_key: str = typer.Option(
        "",
        "--api-key",
        prompt=True,
        hide_input=True,
        help="OpenAI-compatible API key",
    ),
    base_url: str = typer.Option(
        "",
        "--base-url",
        prompt="OpenAI-compatible Base URL",
    ),
    model: str = typer.Option("gpt-4o-mini", "--model"),
) -> None:
    """保存本机模型配置，不写入 Git 仓库。"""

    from capybot.apply.agent_runtime.model import OpenAIPlannerModel

    path = Path.home() / ".capybot" / "apply" / "local_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "openai_api_key": api_key.strip(),
                "openai_base_url": base_url.strip(),
                "model": model.strip(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if not OpenAIPlannerModel().available:
        console.print("[yellow]配置已保存，但 API key 为空。[/yellow]")
        return
    console.print(f"[green]模型配置已保存：{model}[/green]")
    console.print(f"[dim]{path}[/dim]")


@apply_app.command("worker")
def worker(
    loglevel: str = typer.Option("INFO", "--loglevel"),
    pool: str | None = typer.Option(None, "--pool"),
    concurrency: int = typer.Option(
        2,
        "--concurrency",
        min=1,
        max=4,
        help="Worker 并发数；本地默认 2，避免模型任务完全串行",
    ),
) -> None:
    """启动 Apply Celery Worker。"""

    from capybot.apply.agent_runs import AgentRunRepository
    from capybot.apply.celery_app import celery_app
    from capybot.apply.events import worker_heartbeat
    from capybot.apply.jobs import ApplyJobStore

    released = ApplyJobStore().fail_stale()
    stale_runs = AgentRunRepository().fail_stale()
    if released or stale_runs:
        console.print(f"[yellow]已回收后台任务 {released} 个、Agent run {stale_runs} 个。[/yellow]")
    worker_heartbeat("default")
    pool_name = pool or ("threads" if sys.platform == "win32" else "prefork")
    celery_app.worker_main(
        [
            "worker",
            "-Q",
            "capybot_apply",
            "--loglevel",
            loglevel,
            "--pool",
            pool_name,
            "--concurrency",
            str(concurrency),
        ]
    )


@apply_app.command("serve")
def serve(
    port: int = typer.Option(8765, "--port", "-p"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """启动独立 FastAPI/WebSocket WebUI。"""

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(
        "capybot.server:app",
        host="127.0.0.1",
        port=port,
        reload=False,
    )


@apply_app.command("start")
def start(
    port: int = typer.Option(8765, "--port", "-p"),
    no_docker: bool = typer.Option(False, "--no-docker"),
    no_worker: bool = typer.Option(False, "--no-worker"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """一键启动 PostgreSQL、Redis、Worker 和 Capybot Apply WebUI。"""

    from capybot.apply.events import redis_ready, worker_ready
    from capybot.apply.postgres import database_ready, upgrade_database

    running_url = _running_apply_url(port)
    if running_url:
        console.print(f"[green]Capybot Apply 已在运行：{running_url}/apply[/green]")
        if open_browser:
            webbrowser.open(f"{running_url}/apply")
        return
    if not _port_available(port):
        console.print(
            f"[red]端口 {port} 已被其他程序占用。"
            f"请关闭占用程序，或使用 --port 指定其他端口。[/red]"
        )
        raise typer.Exit(1)

    if not no_docker:
        console.print("[cyan]启动 PostgreSQL 与 Redis[/cyan]")
        try:
            subprocess.run(
                ["docker", "compose", "up", "-d", "postgres", "redis"],
                check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            console.print("[red]Docker 依赖启动失败，请确认 Docker Desktop 已启动。[/red]")
            raise typer.Exit(1) from exc

    pg_error = _wait_for_database(upgrade_database, database_ready)
    if pg_error:
        console.print(f"[red]PostgreSQL 未就绪：{pg_error}[/red]")
        raise typer.Exit(1)
    redis_error = _wait_for_redis(redis_ready)
    if redis_error:
        console.print(f"[red]Redis 未就绪：{redis_error}[/red]")
        raise typer.Exit(1)

    worker_process: subprocess.Popen[Any] | None = None
    if not no_worker:
        worker_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "capybot.cli.commands",
                "apply",
                "worker",
            ],
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        worker_error = _wait_for_worker(worker_ready, worker_process)
        if worker_error:
            worker_process.terminate()
            console.print(f"[red]Celery Worker 未就绪：{worker_error}[/red]")
            raise typer.Exit(1)

    console.print(f"[green]依赖已就绪，正在启动：http://127.0.0.1:{port}/apply[/green]")
    try:
        serve(port=port, open_browser=open_browser)
    finally:
        if worker_process and worker_process.poll() is None:
            worker_process.terminate()
            with suppress(Exception):
                worker_process.wait(timeout=5)
            if worker_process.poll() is None:
                worker_process.kill()


@apply_app.command("doctor")
def doctor() -> None:
    """检查数据库、队列、Worker、模型和 BOSS 浏览器状态。"""

    from capybot.apply.agent_runtime.model import OpenAIPlannerModel
    from capybot.apply.events import redis_ready, worker_ready
    from capybot.apply.postgres import apply_database_url, apply_redis_url, database_ready
    from capybot.connectors.boss import BossConnector

    postgres_ok, postgres_error = database_ready()
    redis_ok, redis_error = redis_ready()
    celery_ok, celery_error = worker_ready() if redis_ok else (False, "Redis 不可用")
    model = OpenAIPlannerModel()
    boss = BossConnector().login_status()
    table = Table(title="Capybot Apply 运行检查")
    table.add_column("组件")
    table.add_column("状态")
    table.add_column("详情")
    table.add_row(
        "PostgreSQL",
        "正常" if postgres_ok else "失败",
        apply_database_url() if postgres_ok else str(postgres_error),
    )
    table.add_row(
        "Redis",
        "正常" if redis_ok else "失败",
        apply_redis_url() if redis_ok else str(redis_error),
    )
    table.add_row(
        "Celery Worker",
        "正常" if celery_ok else "未就绪",
        "心跳正常" if celery_ok else str(celery_error),
    )
    table.add_row(
        "Planner 模型",
        "已配置" if model.available else "未配置",
        model.model_label,
    )
    table.add_row(
        "BOSS 浏览器",
        "可读" if boss.get("logged_in") else "需登录",
        str(boss.get("current_url") or boss.get("profile_dir") or ""),
    )
    console.print(table)


@apply_app.command("demo")
def demo(
    reset: bool = typer.Option(True, "--reset/--keep", help="重置演示账号后重新导入"),
    analyze: bool = typer.Option(True, "--analyze/--no-analyze", help="入队运行真实 Agent"),
) -> None:
    """加载隔离中文场景，并通过生产链路运行 Agent。"""

    from capybot.apply.demo import ApplyDemoService
    from capybot.apply.events import worker_ready
    from capybot.apply.tasks import enqueue_import_analysis

    report = ApplyDemoService().load(reset=reset)
    trigger_job = None
    if analyze:
        ready, error = worker_ready()
        if not ready:
            console.print(f"[yellow]演示证据已导入，但 Worker 未就绪：{error}[/yellow]")
        else:
            trigger_job = enqueue_import_analysis(str(report["import_run_id"]))
    console.print(
        "[green]中文演示已加载："
        f"{report['scanned_conversations']} 个会话、"
        f"{report['new_messages']} 条消息、"
        f"{report['opportunity_count']} 个机会。[/green]"
    )
    if trigger_job:
        console.print(f"[cyan]Agent 分析任务：{trigger_job['id']}[/cyan]")
    console.print("[dim]演示账号与真实 BOSS 账号数据完全隔离。[/dim]")


@apply_app.command("login")
def login() -> None:
    """打开 Capybot 专用 BOSS Chrome。"""

    from capybot.connectors.boss import BossConnector

    result = BossConnector().begin_login()
    console.print(
        "[green]BOSS 专用浏览器已打开。[/green]"
        if result.get("cdp_alive")
        else "[yellow]浏览器已启动，请完成扫码登录。[/yellow]"
    )


@apply_app.command("import")
def sync_boss(
    days: int = typer.Option(30, "--days", min=1, max=90),
) -> None:
    """将一次 BOSS 只读同步任务加入队列。"""

    from capybot.apply.jobs import ApplyJobStore, apply_health
    from capybot.apply.tasks import import_boss_snapshot

    health = apply_health()
    if not health.get("can_enqueue"):
        console.print(f"[red]{health.get('message')}[/red]")
        raise typer.Exit(1)
    jobs = ApplyJobStore()
    job, created = jobs.create_or_get(
        "import_boss_snapshot",
        idempotency_key=f"manual-sync:{days}",
        payload={"days": days},
        message=f"已加入近 {days} 天 BOSS 同步队列。",
    )
    if created:
        task = import_boss_snapshot.delay(job["id"], days, None, True)
        jobs.mark_task(job["id"], task.id)
    console.print(f"[green]同步任务：{job['id']}[/green]")


@apply_app.command("analyze")
def analyze(
    opportunity_id: str = typer.Argument(..., help="机会 ID"),
) -> None:
    """将单个机会的 Opportunity Agent 任务加入队列。"""

    from capybot.apply.jobs import ApplyJobStore, apply_health
    from capybot.apply.tasks import analyze_opportunity

    health = apply_health()
    if not health.get("can_enqueue"):
        console.print(f"[red]{health.get('message')}[/red]")
        raise typer.Exit(1)
    jobs = ApplyJobStore()
    job, created = jobs.create_or_get(
        "analyze_opportunity",
        idempotency_key=f"manual-analyze:{opportunity_id}",
        target_type="opportunity",
        target_id=opportunity_id,
        payload={"opportunity_id": opportunity_id, "trigger": "manual"},
        message="手动机会分析已入队。",
    )
    if created:
        task = analyze_opportunity.delay(
            job["id"],
            opportunity_id,
            {"type": "manual"},
        )
        jobs.mark_task(job["id"], task.id)
    console.print(f"[green]Agent 任务：{job['id']}[/green]")


@apply_app.command("eval")
def evaluate(
    live: bool = typer.Option(False, "--live", help="调用当前模型运行中文场景评测"),
    output: Path | None = typer.Option(None, "--output", help="保存 JSON 报告"),
) -> None:
    """运行 Opportunity Agent 契约与可选模型效果评测。"""

    from capybot.evaluation.eval_harness import format_eval, run_eval

    result = run_eval(live=live)
    rendered = format_eval(result)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        console.print(f"[green]评测报告已保存：{output}[/green]")
    console.print(rendered)


@apply_app.command("benchmark")
def benchmark(
    limit: int = typer.Option(
        0,
        "--limit",
        min=0,
        help="最多回放多少个真实机会，0 表示全部",
    ),
    concurrency: int = typer.Option(
        3,
        "--concurrency",
        min=1,
        max=6,
        help="并发模型请求数",
    ),
    output: Path = typer.Option(
        Path(".artifacts/benchmarks/cold_start.json"),
        "--output",
        help="仅含聚合指标的 JSON 报告路径",
    ),
    skip_baseline: bool = typer.Option(
        False,
        "--skip-baseline",
        help="只运行 Tool-Calling Agent，不运行一次性分析基线",
    ),
    reference_set: Path | None = typer.Option(
        None,
        "--reference-set",
        help="可选的 LLM 双标注裁决参考集；未提供时使用规则银标",
    ),
) -> None:
    """在隔离 PostgreSQL 中回放真实 L1，评估首次导入效果。"""

    import asyncio

    from capybot.evaluation.cold_start_benchmark import (
        run_benchmark,
        write_markdown_report,
        write_report,
    )

    console.print("[cyan]正在创建隔离 benchmark 数据库并复制真实 L1 证据…[/cyan]")
    result = asyncio.run(
        run_benchmark(
            limit=limit or None,
            concurrency=concurrency,
            include_baseline=not skip_baseline,
            reference_set_path=reference_set,
        )
    )
    write_report(result, output)
    write_markdown_report(result, output.with_suffix(".md"))
    agent = result["agent"]
    console.print(
        "[green]冷启动回放完成："
        f"{agent['accepted']}/{agent['runs']} 次 Agent 输出通过 CommitGate；"
        f"阶段/行动联合准确率 {agent['stage_action_accuracy']}。[/green]"
    )
    console.print(f"[dim]聚合报告：{output.resolve()}[/dim]")


@apply_app.command("benchmark-mcp")
def benchmark_mcp(
    job_limit: int = typer.Option(
        5,
        "--job-limit",
        min=1,
        max=10,
        help="受控 BOSS 岗位详情样本数",
    ),
    company_limit: int = typer.Option(
        3,
        "--company-limit",
        min=0,
        max=5,
        help="受控公司公开信息样本数",
    ),
    output: Path = typer.Option(
        Path(".artifacts/benchmarks/mcp_value.json"),
        "--output",
        help="匿名聚合实验报告路径",
    ),
    database_name: str = typer.Option(
        "capybot_apply_benchmark",
        "--database-name",
        help="隔离 benchmark 数据库名",
    ),
) -> None:
    """在冷启动隔离库中验证 MCP 是否取得并贡献新证据。"""

    import asyncio

    from capybot.evaluation.cold_start_benchmark import benchmark_database_url
    from capybot.evaluation.mcp_value_benchmark import (
        run_mcp_value_benchmark,
        write_markdown_report,
        write_report,
    )

    console.print("[cyan]正在隔离冷启动库中运行受控 MCP 价值实验…[/cyan]")
    result = asyncio.run(
        run_mcp_value_benchmark(
            database_url=benchmark_database_url(database_name=database_name),
            job_limit=job_limit,
            company_limit=company_limit,
        )
    )
    write_report(result, output)
    write_markdown_report(result, output.with_suffix(".md"))
    job = result["boss_job_detail"]
    company = result["company_research"]
    console.print(
        "[green]MCP 实验完成："
        f"BOSS 岗位详情 {job['tool_successes']}/{job['cases']}；"
        f"公司信息 {company['tool_successes']}/{company['cases']}。[/green]"
    )
    console.print(f"[dim]聚合报告：{output.resolve()}[/dim]")


@apply_app.command("benchmark-dynamic")
def benchmark_dynamic(
    concurrency: int = typer.Option(
        3,
        "--concurrency",
        min=1,
        max=6,
        help="并发模型请求数",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        min=0,
        help="最多回放多少个真实会话，0 表示全部",
    ),
    reference_set: Path = typer.Option(
        Path(".artifacts/gold_set/reference_set.json"),
        "--reference-set",
        help="LLM 双标注裁决参考集",
    ),
    output: Path = typer.Option(
        Path(".artifacts/benchmarks/dynamic_agent_vs_oneshot.json"),
        "--output",
        help="仅含聚合指标的 JSON 报告路径",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="关闭在线 MCP，仅用于完全可重复的离线回放",
    ),
    account_id: str | None = typer.Option(
        None,
        "--account-id",
        help="明确指定回放账号，避免演示账号污染真实数据基准",
    ),
) -> None:
    """按真实消息时间顺序对比增量 Agent 与全量 One-shot。"""

    import asyncio

    from capybot.evaluation.dynamic_tracking_benchmark import (
        run_dynamic_benchmark,
        write_markdown_report,
        write_report,
    )

    console.print("[cyan]正在隔离数据库中逐轮回放真实招聘会话…[/cyan]")
    result = asyncio.run(
        run_dynamic_benchmark(
            concurrency=concurrency,
            limit=limit or None,
            reference_set_path=reference_set,
            allow_external=not offline,
            source_account_id=account_id,
        )
    )
    write_report(result, output)
    write_markdown_report(result, output.with_suffix(".md"))
    agent = result["agent"]
    baseline = result["baseline"]
    console.print(
        "[green]动态回放完成："
        f"{result['benchmark']['episode_count']} 个增量轮次；"
        f"Agent 最终联合一致率 {agent['final_quality']['stage_action_accuracy']}，"
        f"One-shot {baseline['final_quality']['stage_action_accuracy']}。[/green]"
    )
    console.print(f"[dim]聚合报告：{output.resolve()}[/dim]")


@apply_app.command("benchmark-grounded")
def benchmark_grounded(
    limit: int = typer.Option(50, "--limit", min=10, max=50),
    research_limit: int = typer.Option(10, "--research-limit", min=0, max=20),
    concurrency: int = typer.Option(4, "--concurrency", min=1, max=6),
    scenario_profile: str = typer.Option(
        "realistic",
        "--scenario-profile",
        help="场景分布：realistic 保留真实静默比例；tool_stress 强化互动和工具调用",
    ),
    output: Path = typer.Option(
        Path(".artifacts/benchmarks/grounded.json"),
        "--output",
    ),
    fixture: Path = typer.Option(
        Path(".artifacts/grounded/current_jobs_zh.json"),
        "--fixture",
    ),
    input_fixture: Path | None = typer.Option(
        None,
        "--input-fixture",
        help="复用此前冻结的真实实习岗位，跳过在线采集",
    ),
) -> None:
    """用当前真实岗位和受控合成聊天运行 Grounded Agent 基准。"""

    import asyncio

    from capybot.evaluation.grounded_benchmark import (
        SCENARIO_PROFILES,
        run_grounded_benchmark,
        write_report,
    )

    if scenario_profile not in SCENARIO_PROFILES:
        choices = "、".join(SCENARIO_PROFILES)
        raise typer.BadParameter(f"场景分布必须是：{choices}")
    console.print("[cyan]正在读取当前岗位并运行 Grounded Agent Benchmark…[/cyan]")
    result = asyncio.run(
        run_grounded_benchmark(
            limit=limit,
            research_limit=research_limit,
            concurrency=concurrency,
            fixture_path=fixture,
            input_fixture_path=input_fixture,
            scenario_profile=scenario_profile,
        )
    )
    write_report(result, output)
    quality = result["quality"]
    mcp = result["job_mcp"]
    console.print(
        "[green]Grounded Benchmark 完成："
        f"{quality['stage_action_accuracy']:.2%} 阶段+行动命中；"
        f"岗位 MCP {mcp['tool_successes']}/{mcp['cases']}。[/green]"
    )
    console.print(f"[dim]聚合报告：{output.resolve()}[/dim]")


@apply_app.command("reference-export")
def reference_export(
    output: Path = typer.Option(
        Path(".artifacts/gold_set/source.json"),
        "--output",
        help="本地脱敏标注源路径；不要提交到 Git",
    ),
) -> None:
    """从 PostgreSQL 只读导出真实会话的本地脱敏标注源。"""

    from capybot.evaluation.gold_set import export_annotation_source

    result = export_annotation_source(output)
    message_count = sum(
        int(case["source_stats"]["messages"]) for case in result["cases"]
    )
    console.print(
        f"[green]已导出 {len(result['cases'])} 个会话、"
        f"{message_count} 条消息到 {output.resolve()}。[/green]"
    )


@apply_app.command("reference-finalize")
def reference_finalize(
    source: Path = typer.Option(
        Path(".artifacts/gold_set/source.json"), "--source"
    ),
    annotator_a: Path = typer.Option(
        Path(".artifacts/gold_set/annotations_a.json"), "--annotator-a"
    ),
    annotator_b: Path = typer.Option(
        Path(".artifacts/gold_set/annotations_b.json"), "--annotator-b"
    ),
    adjudication: Path = typer.Option(
        Path(".artifacts/gold_set/adjudication.json"), "--adjudication"
    ),
    output: Path = typer.Option(
        Path(".artifacts/gold_set/reference_set.json"), "--output"
    ),
    report: Path = typer.Option(
        Path(".artifacts/gold_set/reference_set_report.md"), "--report"
    ),
) -> None:
    """校验双路标注和裁决结果，生成本地参考集与匿名聚合报告。"""

    from capybot.evaluation.gold_set import (
        finalize_reference_set,
        load_json,
        write_json,
        write_reference_report,
    )

    result = finalize_reference_set(
        load_json(source),
        load_json(annotator_a),
        load_json(annotator_b),
        load_json(adjudication),
    )
    write_json(result, output)
    write_reference_report(result, report)
    metrics = result["metrics"]
    console.print(
        "[green]参考集已完成："
        f"{metrics['included_cases']} 条纳入，"
        f"{metrics['adjudicated_cases']} 条经过裁决。[/green]"
    )
    console.print(f"[dim]本地参考集：{output.resolve()}[/dim]")
    console.print(f"[dim]匿名报告：{report.resolve()}[/dim]")


@apply_app.command("clear")
def clear(
    include_login: bool = typer.Option(False, "--include-login"),
) -> None:
    """清空 PostgreSQL Apply 数据，可选清空 BOSS profile。"""

    from capybot.apply.store import ApplyStore
    from capybot.connectors.boss import BossConnector

    ApplyStore().clear()
    if include_login:
        BossConnector().clear_login_state()
    console.print("[green]Apply 数据已清空。[/green]")


def _wait_for_database(upgrade, check) -> str | None:
    last_error: str | None = None
    for _ in range(45):
        try:
            upgrade()
            ok, last_error = check()
            if ok:
                return None
        except Exception as exc:
            last_error = str(exc)
        time.sleep(1)
    return last_error or "等待超时"


def _wait_for_redis(check) -> str | None:
    last_error: str | None = None
    for _ in range(30):
        ok, last_error = check()
        if ok:
            return None
        time.sleep(1)
    return last_error or "等待超时"


def _wait_for_worker(check, process: subprocess.Popen[Any]) -> str | None:
    last_error: str | None = None
    for _ in range(120):
        if process.poll() is not None:
            return f"进程提前退出，退出码 {process.returncode}"
        ok, last_error = check()
        if ok:
            return None
        time.sleep(0.5)
    return last_error or "等待超时"


if __name__ == "__main__":
    app()
