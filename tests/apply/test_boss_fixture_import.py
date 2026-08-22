import json
from pathlib import Path

import pytest

from capybot.apply.importer import SnapshotImporter
from capybot.apply.store import ApplyStore
from capybot.connectors.boss import BossConnector


def test_snapshot_importer_reads_fixture(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CAPYBOT_APPLY_DISABLE_LLM", "1")
    fixture = {
        "conversations": [
            {
                "conversation_id": "boss_42",
                "id": "boss_42",
                "boss_uid": "42",
                "contact_name": "王女士",
                "company": "示例科技",
            }
        ],
        "messages": {
            "42": [
                {
                    "mid": "1001",
                    "received": True,
                    "from": {"uid": "42", "name": "王女士"},
                    "body": {
                        "text": "请今晚发我简历和项目链接。",
                    },
                }
            ]
        },
    }
    path = tmp_path / "boss.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.setenv("CAPYBOT_BOSS_FIXTURE", str(path))

    store = ApplyStore()
    report = SnapshotImporter(store=store, boss=BossConnector(tmp_path / "profile")).import_boss()
    assert report["scanned_conversations"] == 1
    assert report["source_status"]["readable"] is True
    assert report["source_status"]["conversation_count"] == 1
    assert report["new_messages"] == 1
    assert len(store.opportunities()) == 1
    assert store.tasks_payload()["suggestions"] == []


@pytest.mark.asyncio
async def test_boss_connector_reads_job_detail_fixture(tmp_path: Path, monkeypatch):
    fixture = {
        "job_details": {
            "encrypted-job-1": {
                "code": 0,
                "zpData": {
                    "jobInfo": {
                        "encryptJobId": "encrypted-job-1",
                        "jobName": "Agent 开发实习生",
                    },
                    "jobDetail": "负责 Agent 工具调用与评估。",
                },
            }
        }
    }
    path = tmp_path / "boss-job-detail.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.setenv("CAPYBOT_BOSS_FIXTURE", str(path))

    detail = await BossConnector(tmp_path / "profile").fetch_job_detail_async(
        "encrypted-job-1"
    )

    assert detail["jobInfo"]["jobName"] == "Agent 开发实习生"
    assert detail["jobDetail"] == "负责 Agent 工具调用与评估。"


@pytest.mark.asyncio
async def test_boss_connector_uses_security_id_for_live_job_detail(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("CAPYBOT_BOSS_FIXTURE", raising=False)

    class FakePage:
        url = "https://www.zhipin.com/web/geek/chat"

        async def evaluate(self, source, arguments):
            assert "?securityId=" in source
            assert "encryptJobId=" not in source
            assert arguments["securityId"] == "security-1"
            return {
                "code": 0,
                "zpData": {
                    "jobInfo": {"jobName": "Agent 开发实习生"},
                    "jobDetail": "负责 Agent 工具调用与评估。",
                },
            }

    page = FakePage()
    connector = BossConnector(tmp_path / "profile")

    async def ensure_page():
        return page

    monkeypatch.setattr(connector, "_ensure_async_page", ensure_page)
    detail = await connector.fetch_job_detail_async("security-1")

    assert detail["jobInfo"]["jobName"] == "Agent 开发实习生"
