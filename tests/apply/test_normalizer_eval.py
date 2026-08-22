from capybot.apply.normalizer import BossMessageNormalizer
from capybot.evaluation.normalizer_eval import run_normalizer_eval


def test_normalizer_eval_is_reproducible_and_passes() -> None:
    result = run_normalizer_eval()

    assert result["dataset"]["cases"] >= 18
    assert len(result["dataset"]["sha256"]) == 64
    assert result["metrics"]["message_type_accuracy"] == 1.0
    assert result["metrics"]["direction_accuracy"] == 1.0
    assert result["failures"] == []


def test_boss_job_assistant_intro_is_non_human_platform_content() -> None:
    message = BossMessageNormalizer.normalize(
        "assistant-conversation",
        {
            "mid": "assistant-message",
            "received": True,
            "from": {"uid": "assistant", "name": "求职助手"},
            "body": {
                "text": "我是你的求职助手，我可以根据开聊和收藏岗位推送相似岗位。"
            },
        },
    )

    assert message["message_type"] == "platform_card"
    assert message["is_human_message"] == 0
