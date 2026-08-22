from capybot.evaluation.eval_harness import run_offline_eval


def test_offline_harness_contract_eval_passes() -> None:
    result = run_offline_eval()

    assert result["passed"] == result["total"]
    assert result["adversarial_safety"]["passed"] == 2
