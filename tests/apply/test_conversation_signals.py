from capybot.apply.conversation_signals import ConversationSignals


def test_activity_invitation_is_not_interview_signal() -> None:
    text = "周末有一场技术交流会，你有兴趣参加吗？"

    assert ConversationSignals.requires_reply(text)
    assert ConversationSignals.is_ambiguous_invitation(text)
    assert not ConversationSignals.is_interview_invitation(text)


def test_explicit_interview_invitation_is_detected() -> None:
    text = "明天下午三点方便参加线上面试吗？"

    assert ConversationSignals.requires_reply(text)
    assert ConversationSignals.is_interview_invitation(text)
    assert not ConversationSignals.is_ambiguous_invitation(text)


def test_conditional_interview_language_is_not_confirmed_interview() -> None:
    text = "后续是否进入面试还需要负责人评估。"

    assert not ConversationSignals.is_interview_invitation(text)


def test_critical_safety_risk_only_matches_explicit_payment_language() -> None:
    assert ConversationSignals.is_critical_safety_risk("培训费用可以分期")
    assert ConversationSignals.is_critical_safety_risk("入职前先交报名费")
    assert not ConversationSignals.is_critical_safety_risk("公司会提供免费培训")


def test_candidate_profile_is_requested_only_for_personalized_questions() -> None:
    assert ConversationSignals.requests_candidate_profile(
        "你先简单介绍下自己和最近做的项目吧。"
    )
    assert ConversationSignals.requests_candidate_profile(
        "可以说说你在 Agent 项目里的贡献吗？"
    )
    assert not ConversationSignals.requests_candidate_profile(
        "这个岗位主要负责 Agent 工具调用。"
    )


def test_boss_job_assistant_is_a_platform_conversation() -> None:
    assert ConversationSignals.is_platform_assistant_conversation(
        contact_name="10:51求职助手您正在与Boss求职助手沟通",
        preview="您正在与Boss求职助手沟通",
    )
    assert not ConversationSignals.is_platform_assistant_conversation(
        contact_name="林女士",
        preview="这个岗位主要做 Agent 工具调用。",
    )
