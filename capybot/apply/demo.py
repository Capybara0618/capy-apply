"""Isolated, reproducible Chinese product demo using the production pipeline."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from capybot.connectors.boss import BossConnector
from capybot.runtime import runtime_dir

from .importer import SnapshotImporter
from .models import parse_utc_datetime
from .store import ApplyStore

DEMO_ACCOUNT_ID = "capybot_demo"
DEMO_ACCOUNT_UID = "capybot-demo-local"
DEMO_FIXTURE = Path(__file__).with_name("demo_scenarios_zh.json")

DEMO_RESUME = """# Agent 开发实习生

## 技能
- Python、FastAPI、PostgreSQL、Redis、Celery、React
- MCP Tool Calling、Agent Memory、RAG、LLM Eval

## 项目
- Capybot Apply：实现受限 Tool-Calling Agent、三层记忆和证据校验。
- 企业知识库问答：实现检索、重排、引用溯源和离线评测。

## 求职
- 可连续实习 6 个月，每周 5 天。
"""

DEMO_PREFERENCES = {
    "target_roles": "Agent 开发实习, 大模型应用开发, Python 后端",
    "cities": "杭州, 上海, 远程",
    "salary": "200-400 元/天",
    "internship_time": "每周 5 天，连续 6 个月",
    "excluded": "培训贷, 收费培训, 无薪",
}


class ApplyDemoService:
    """Load demo evidence without touching any real BOSS account."""

    def __init__(
        self,
        store: ApplyStore | None = None,
        *,
        fixture_path: Path = DEMO_FIXTURE,
    ) -> None:
        self.store = store or ApplyStore(account_id=DEMO_ACCOUNT_ID)
        self.fixture_path = fixture_path

    def load(self, *, reset: bool = True) -> dict[str, Any]:
        if reset:
            self.store.delete_account(DEMO_ACCOUNT_ID)
        fixture = self._fixture()
        connector = BossConnector(
            runtime_dir("browser") / "demo-profile",
            fixture_data=fixture,
        )
        report = SnapshotImporter(store=self.store, boss=connector).import_boss(days=30)
        self.store.update_profile(
            {
                "resume_markdown": DEMO_RESUME,
                "preferences": DEMO_PREFERENCES,
            }
        )
        return {
            **report,
            "demo": True,
            "account_id": DEMO_ACCOUNT_ID,
            "opportunity_count": len(self.store.opportunities()),
        }

    def _fixture(self) -> dict[str, Any]:
        source = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        fixture = self.rebase_timestamps(source)
        fixture["account"] = {
            "id": DEMO_ACCOUNT_ID,
            "account_uid": DEMO_ACCOUNT_UID,
            "display_name": "Capybot 中文演示账号",
            "source": "demo_fixture",
        }
        return fixture

    @staticmethod
    def rebase_timestamps(
        source: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Move fixture time as a block while preserving conversation intervals."""

        fixture = copy.deepcopy(source)
        timestamp_nodes: list[tuple[dict[str, Any], str, datetime]] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    parsed = parse_utc_datetime(item) if key in {"time", "last_message_at"} else None
                    if parsed is not None:
                        timestamp_nodes.append((value, key, parsed))
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(fixture)
        if not timestamp_nodes:
            return fixture
        latest = max(item[2] for item in timestamp_nodes)
        target = (now or datetime.now(timezone.utc)) - timedelta(minutes=30)
        shift = target.astimezone(timezone.utc) - latest.astimezone(timezone.utc)
        for container, key, parsed in timestamp_nodes:
            container[key] = (parsed.astimezone(timezone.utc) + shift).isoformat()
        return fixture
