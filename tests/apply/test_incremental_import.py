from pathlib import Path

from capybot.apply.importer import SnapshotImporter
from capybot.apply.normalizer import BossMessageNormalizer
from capybot.apply.store import ApplyStore


class FakeBoss:
    def __init__(self, messages):
        self.messages = messages

    def account_snapshot(self):
        return {"id": "fake-boss-account", "display_name": "测试账号"}

    def list_conversations(self, days=30, limit=200):
        return [
            {
                "conversation_id": "conv_1",
                "id": "boss_1",
                "boss_uid": "boss_1",
                "contact_name": "王女士",
                "company": "示例科技",
            }
        ]

    def fetch_messages(self, boss_uid, max_pages=20, **_context):
        return self.messages


def test_boss_image_and_platform_cards_are_not_system():
    image = BossMessageNormalizer.normalize(
        "conv",
        {
            "mid": "img1",
            "received": False,
            "body": {
                "type": 3,
                "image": {
                    "originImage": {
                        "url": "https://example.test/resume.jpg",
                        "width": 100,
                        "height": 200,
                    }
                },
            },
            "from": {"uid": "me", "name": "我"},
        },
    )
    card = BossMessageNormalizer.normalize(
        "conv",
        {
            "mid": "card1",
            "received": True,
            "body": {
                "type": 16,
                "articles": [{"title": "你与该职位竞争力", "description": "优秀竞争者会完善简历"}],
            },
            "from": {"uid": "boss", "name": "王女士"},
        },
    )

    assert image["message_type"] == "image"
    assert image["is_human_message"] == 1
    assert image["attachment_meta"]["kind"] == "image"
    assert card["message_type"] == "platform_card"
    assert card["is_human_message"] == 0


def test_boss_job_card_is_context_not_human_reply():
    card = BossMessageNormalizer.normalize(
        "conv",
        {
            "mid": "job1",
            "received": True,
            "body": {
                "type": 8,
                "jobDesc": {"jobId": "j1", "title": "Agent 开发实习生", "company": "示例科技"},
            },
            "from": {"uid": "boss", "name": "王女士"},
        },
    )

    assert card["message_type"] == "job_card"
    assert card["is_human_message"] == 0


def test_job_card_identity_hints_override_ambiguous_status_and_received():
    messages = [
        {
            "mid": "job1",
            "status": 2,
            "received": True,
            "body": {
                "type": 8,
                "jobDesc": {
                    "jobId": "j1",
                    "title": "Agent 开发实习生",
                    "geek": {"uid": 1001, "name": "候选人"},
                    "boss": {"uid": 2002, "name": "王女士"},
                },
            },
        },
        {
            "mid": "candidate-reply",
            "status": 2,
            "received": True,
            "body": {"text": "可以接受"},
            "from": {"uid": 1001, "name": "候选人"},
        },
        {
            "mid": "recruiter-reply",
            "status": 2,
            "received": True,
            "body": {"text": "方便发一份简历吗？"},
            "from": {"uid": 2002, "name": "王女士"},
        },
    ]
    hints = BossMessageNormalizer.identity_hints(messages)
    candidate = BossMessageNormalizer.normalize("conv", messages[1], identity_hints=hints)
    recruiter = BossMessageNormalizer.normalize("conv", messages[2], identity_hints=hints)

    assert candidate["from_me"] is True
    assert candidate["from_me_confidence"] == 0.99
    assert recruiter["from_me"] is False
    assert recruiter["from_me_confidence"] == 0.99


def test_resume_request_dialog_is_recruiter_text_not_system_or_file():
    raw = {
        "mid": "resume-request",
        "bizType": 13,
        "status": 2,
        "received": True,
        "body": {"type": 7, "dialog": {"text": "我想要一份您的附件简历，您是否同意"}},
        "from": {"uid": "boss", "name": "王女士"},
    }
    normalized = BossMessageNormalizer.normalize(
        "conv",
        raw,
        identity_hints={"recruiter_uid": "boss"},
    )

    assert normalized["message_type"] == "text"
    assert normalized["text"] == "我想要一份您的附件简历，您是否同意"
    assert normalized["from_me"] is False
    assert normalized["is_human_message"] == 1


def test_resume_sent_hyperlink_is_candidate_file_action():
    raw = {
        "mid": "resume-sent",
        "status": 2,
        "received": True,
        "body": {
            "hyperLink": {
                "text": "您的附件简历 实习简历 已发送给Boss点击查看附件",
                "url": "https://example.invalid/resume",
            },
        },
        "from": {"uid": "boss", "name": "王女士"},
    }
    normalized = BossMessageNormalizer.normalize(
        "conv",
        raw,
        identity_hints={"candidate_uid": "me", "candidate_name": "候选人", "recruiter_uid": "boss"},
    )

    assert normalized["message_type"] == "file"
    assert normalized["from_me"] is True
    assert normalized["sender_uid"] == "me"
    assert normalized["is_human_message"] == 1


def test_resume_read_receipt_is_non_human_system_message():
    normalized = BossMessageNormalizer.normalize(
        "conv",
        {
            "mid": "resume-read",
            "bizType": 21050035,
            "status": 2,
            "received": True,
            "body": {"templateId": 3, "text": "对方已查看了您的附件简历"},
            "from": {"uid": "boss", "name": "王女士"},
        },
    )

    assert normalized["message_type"] == "system"
    assert normalized["is_human_message"] == 0
    assert normalized["from_me"] is False


def test_second_import_without_new_messages_skips_analysis(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CAPYBOT_APPLY_DISABLE_LLM", "1")
    messages = [
        {
            "mid": "m1",
            "received": True,
            "body": {"text": "方便发一份简历过来吗？"},
            "from": {"uid": "boss_1", "name": "王女士"},
        }
    ]
    store = ApplyStore()

    first = SnapshotImporter(store=store, boss=FakeBoss(messages)).import_boss()
    second = SnapshotImporter(store=store, boss=FakeBoss(messages)).import_boss()

    assert first["new_messages"] == 1
    assert first["analyzed_opportunities"] == 0
    assert second["new_messages"] == 0
    assert second["skipped_conversations"] == 1
    assert second["analyzed_opportunities"] == 0
    latest_items = store.latest_import_delta_panel()["items"]
    assert latest_items[0]["analysis_mode"] == "skipped"


def test_only_my_new_message_uses_lightweight_analysis(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CAPYBOT_APPLY_DISABLE_LLM", "1")
    first_messages = [
        {
            "mid": "m1",
            "received": True,
            "body": {"text": "方便发一份简历过来吗？"},
            "from": {"uid": "boss_1", "name": "王女士"},
        }
    ]
    second_messages = [
        *first_messages,
        {
            "mid": "m2",
            "received": False,
            "body": {"text": "好的，我已整理好简历和项目介绍，可以发您。"},
            "from": {"uid": "me", "name": "我"},
        },
    ]
    store = ApplyStore()

    SnapshotImporter(store=store, boss=FakeBoss(first_messages)).import_boss()
    second = SnapshotImporter(store=store, boss=FakeBoss(second_messages)).import_boss()

    assert second["new_messages"] == 1
    assert second["analyzed_opportunities"] == 0
    item = store.latest_import_delta_panel()["items"][0]
    assert item["analysis_mode"] == "opportunity_agent"
    assert item["opportunity_id"]
