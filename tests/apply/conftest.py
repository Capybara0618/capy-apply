from __future__ import annotations

import os
from pathlib import Path

import pytest

PURE_TEST_FILES = {
    "test_normalizer_eval.py",
    "test_realtime_events.py",
    "test_celery_priority.py",
    "test_agent_runtime.py",
    "test_eval_harness.py",
    "test_context_projection_isolation.py",
    "test_mcp_servers.py",
    "test_fastapi_server.py",
    "test_cold_start_benchmark.py",
    "test_dynamic_tracking_benchmark.py",
    "test_grounded_benchmark.py",
    "test_gold_set.py",
    "test_boss_cdp_resilience.py",
    "test_demo.py",
    "test_conversation_signals.py",
    "test_decision_router.py",
    "test_toolbox_policy.py",
    "test_openai_agents_runtime.py",
    "test_fit_evaluator_v2.py",
}
APPLY_TEST_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    test_url = os.getenv("CAPYBOT_APPLY_TEST_DATABASE_URL")
    if test_url:
        os.environ["CAPYBOT_APPLY_DATABASE_URL"] = test_url
        return
    for item in items:
        if Path(str(item.path)).resolve().parent != APPLY_TEST_DIR:
            continue
        if (
            item.path.name == "test_postgres_runtime_boundary.py"
            or item.path.name in PURE_TEST_FILES
        ):
            continue
        item.add_marker(
            pytest.mark.skip(reason="需要 CAPYBOT_APPLY_TEST_DATABASE_URL 指向 PostgreSQL 测试库")
        )


@pytest.fixture(autouse=True)
def clean_postgres_apply_store(request):
    if not os.getenv("CAPYBOT_APPLY_TEST_DATABASE_URL"):
        yield
        return
    if request.node.path.name == "test_postgres_runtime_boundary.py":
        yield
        return

    from capybot.apply.store import ApplyStore

    store = ApplyStore()
    _reset_test_store(store)
    store.upsert_account({"id": "test_account", "account_uid": "test", "display_name": "测试账号"})
    yield
    _reset_test_store(store)


def _reset_test_store(store) -> None:
    """Reset all mutable state so profile data cannot leak between tests."""

    store.clear()
    with store.connect() as db:
        db.execute("DELETE FROM candidate_profile")
        db.execute("DELETE FROM job_preferences")
