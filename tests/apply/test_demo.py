from datetime import datetime, timedelta, timezone

from capybot.apply.demo import DEMO_ACCOUNT_ID, ApplyDemoService
from capybot.apply.tasks import _opportunity_mcp_env
from capybot.connectors.boss import BossConnector


def test_demo_rebases_all_messages_into_current_window() -> None:
    source = {
        "conversations": [{"last_message_at": "2025-01-02T10:00:00+08:00"}],
        "messages": {
            "hr": [
                {"time": "2025-01-01T10:00:00+08:00"},
                {"time": "2025-01-02T10:00:00+08:00"},
            ]
        },
    }
    now = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)

    rebased = ApplyDemoService.rebase_timestamps(source, now=now)

    first = datetime.fromisoformat(rebased["messages"]["hr"][0]["time"])
    second = datetime.fromisoformat(rebased["messages"]["hr"][1]["time"])
    assert second == now - timedelta(minutes=30)
    assert second - first == datetime(2025, 1, 2, 2, tzinfo=timezone.utc) - datetime(
        2025, 1, 1, 2, tzinfo=timezone.utc
    )


def test_demo_account_identifier_is_explicit() -> None:
    assert DEMO_ACCOUNT_ID == "capybot_demo"


def test_fixture_account_keeps_explicit_demo_source() -> None:
    connector = BossConnector(
        fixture_data={
            "account": {
                "id": DEMO_ACCOUNT_ID,
                "source": "demo_fixture",
            }
        }
    )

    assert connector.account_snapshot()["source"] == "demo_fixture"


def test_demo_account_routes_mcp_to_packaged_fixture() -> None:
    class DemoStore:
        @staticmethod
        def current_account() -> dict[str, str]:
            return {"source": "demo_fixture"}

    env = _opportunity_mcp_env(DemoStore())  # type: ignore[arg-type]

    assert env["CAPYBOT_BOSS_FIXTURE"].endswith("demo_scenarios_zh.json")
