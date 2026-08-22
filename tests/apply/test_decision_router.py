from __future__ import annotations

from capybot.apply.decision_router import DecisionRouter


def test_router_skips_non_human_delta_without_calling_agent() -> None:
    route = DecisionRouter.route_delta(
        [
            {
                "message_type": "auto_followup",
                "is_human_message": 0,
                "from_me": True,
            }
        ],
        source_quality=None,
    )

    assert route.mode == "skip"
    assert route.trigger_type == "non_human_delta"


def test_router_reactivates_cold_opportunity_on_real_hr_reply() -> None:
    route = DecisionRouter.route_delta(
        [
            {
                "message_type": "text",
                "is_human_message": 1,
                "from_me": False,
            }
        ],
        source_quality="cold_outreach_no_reply",
    )

    assert route.mode == "agent"
    assert route.trigger_type == "hr_message"
    assert route.reactivated is True


def test_router_sends_candidate_delta_for_context_inspection() -> None:
    route = DecisionRouter.route_delta(
        [
            {
                "message_type": "file",
                "is_human_message": 1,
                "from_me": True,
            }
        ],
        source_quality=None,
    )

    assert route.mode == "agent"
    assert route.trigger_type == "candidate_message"


def test_manual_analysis_is_never_replaced_by_cold_projection() -> None:
    route = DecisionRouter.route_context(
        {
            "messages": [
                {
                    "message_id": "m1",
                    "message_type": "text",
                    "is_human_message": 1,
                    "from_me": True,
                }
            ]
        },
        {"type": "manual"},
    )

    assert route.mode == "agent"
    assert route.decision is None
