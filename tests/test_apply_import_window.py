from datetime import datetime, timedelta, timezone

from capybot.apply.importer import SnapshotImporter
from capybot.apply.models import ImportReport
from capybot.apply.normalizer import BossMessageNormalizer


class RecordingStore:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.jobs: list[dict] = []

    def upsert_message(self, message: dict) -> tuple[str, bool]:
        self.messages.append(message)
        return message["message_id"], True

    def upsert_job_card(self, conversation_id: str, job: dict) -> tuple[str, bool]:
        self.jobs.append(job)
        return "job", True


def test_normalizer_converts_millisecond_epoch_to_iso() -> None:
    result = BossMessageNormalizer.normalize(
        "c1",
        {"mid": "m1", "time": 1_779_243_995_235, "body": {"text": "您好"}},
    )

    assert result["sent_at"].startswith("2026-")
    assert result["sent_at"].endswith("+00:00")


def test_importer_excludes_messages_older_than_requested_window() -> None:
    store = RecordingStore()
    importer = SnapshotImporter(store=store, boss=object())
    now = datetime.now(timezone.utc)
    messages = [
        {
            "mid": "old",
            "time": int((now - timedelta(days=40)).timestamp() * 1000),
            "body": {"text": "旧消息"},
        },
        {
            "mid": "new",
            "time": int((now - timedelta(days=2)).timestamp() * 1000),
            "body": {"text": "新消息"},
        },
    ]

    new_ids, in_window_count = importer._save_messages_and_jobs(
        "c1",
        messages,
        ImportReport(),
        "run1",
        now - timedelta(days=30),
    )

    assert new_ids == ["new"]
    assert in_window_count == 1
    assert [message["message_id"] for message in store.messages] == ["new"]
