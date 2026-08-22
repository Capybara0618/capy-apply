from capybot.evaluation.gold_set import (
    compare_annotators,
    finalize_reference_set,
    validate_annotations,
)


def _source():
    return {
        "cases": [
            {
                "case_id": "case_a",
                "messages": [{"ref": "boss_message:1"}],
                "jobs": [],
            },
            {
                "case_id": "case_b",
                "messages": [{"ref": "boss_message:2"}],
                "jobs": [],
            },
        ]
    }


def _label(case_id: str, stage: str, action: str, ref: str):
    return {
        "case_id": case_id,
        "include": True,
        "stage": stage,
        "action": action,
        "confidence": "high",
        "evidence": [ref],
        "reason": "证据明确",
    }


def test_annotation_validator_rejects_unknown_evidence() -> None:
    labels = {
        "annotations": [
            _label("case_a", "waiting_feedback", "wait", "boss_message:missing"),
            _label("case_b", "need_my_action", "reply", "boss_message:2"),
        ]
    }
    result = validate_annotations(_source(), labels)
    assert result["ok"] is False
    assert "不存在的证据" in result["errors"][0]


def test_double_annotation_disagreement_is_adjudicated() -> None:
    annotator_a = {
        "annotations": [
            _label("case_a", "waiting_feedback", "wait", "boss_message:1"),
            _label("case_b", "need_my_action", "reply", "boss_message:2"),
        ]
    }
    annotator_b = {
        "annotations": [
            _label("case_a", "waiting_feedback", "wait", "boss_message:1"),
            _label("case_b", "communicating", "reply", "boss_message:2"),
        ]
    }
    comparison = compare_annotators(_source(), annotator_a, annotator_b)
    assert comparison["primary_agreement"] == 0.5
    assert [item["case_id"] for item in comparison["disagreements"]] == ["case_b"]

    adjudication = {
        "annotations": [
            _label("case_b", "need_my_action", "reply", "boss_message:2")
        ]
    }
    final = finalize_reference_set(
        _source(), annotator_a, annotator_b, adjudication
    )
    assert final["provenance"]["is_human_gold"] is False
    assert final["metrics"]["adjudicated_cases"] == 1
    assert final["metrics"]["included_cases"] == 2
