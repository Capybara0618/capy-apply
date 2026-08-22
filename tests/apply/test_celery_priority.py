from capybot.apply.celery_app import APPLY_TASK_PRIORITIES, enqueue_apply_task
from capybot.apply.tasks import _enqueue_fit_if_stale


class _Task:
    def __init__(self) -> None:
        self.call = None

    def apply_async(self, **kwargs):
        self.call = kwargs
        return kwargs


def test_interactive_import_preempts_background_rebuild() -> None:
    assert (
        APPLY_TASK_PRIORITIES["import_boss_snapshot"]
        < APPLY_TASK_PRIORITIES["rebuild_derived_from_l1"]
    )
    task = _Task()

    result = enqueue_apply_task(task, "import_boss_snapshot", "job-1", 30, None)

    assert result["args"] == ("job-1", 30, None)
    assert result["priority"] == APPLY_TASK_PRIORITIES["import_boss_snapshot"]


def test_current_needs_review_fit_is_not_recomputed() -> None:
    class Store:
        @staticmethod
        def opportunity_context(_opportunity_id):
            return {
                "candidate_profile": {"resume_markdown": "# 简历"},
                "fit_analysis": {"status": "needs_review"},
            }

    class Jobs:
        @staticmethod
        def create_or_get(*_args, **_kwargs):
            raise AssertionError("当前 needs_review 评分不应重复入队")

    assert _enqueue_fit_if_stale(Store(), Jobs(), "opp-1") is None
