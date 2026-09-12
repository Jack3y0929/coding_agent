"""工作流终态与审查评测回归测试。"""

from backend.evaluation.service import _build_metrics
from backend.fallback.engine import after_human_acceptance, after_intervention
from backend.main import resolve_terminal_status
from backend.worker import resolve_task_status


def test_human_stop_is_not_successful_completion() -> None:
    decision = after_intervention("stop")

    assert decision.action == "stop"
    assert decision.next_stage == "output"


def test_human_acceptance_only_approved_can_finish_successfully() -> None:
    approved = after_human_acceptance("approved", 0, 3)
    rejected = after_human_acceptance("rejected", 0, 3)

    assert approved.next_stage == "output"
    assert rejected.next_stage == "fix"


def test_review_metrics_use_aggregate_events() -> None:
    metrics = _build_metrics([
        {
            "event_type": "review_aggregated",
            "stage": "review_aggregate",
            "payload": {"passed": True},
        },
    ], None)

    review_metric = next(item for item in metrics if item["metric_name"] == "review_passed")

    assert review_metric["score"] == 1.0
    assert review_metric["evidence"]["review_rounds"] == 1


def test_terminal_status_requires_explicit_output_state() -> None:
    assert resolve_terminal_status(None) == ("failed", "工作流未产生明确成功终态")
    assert resolve_terminal_status("done") == ("done", None)
    assert resolve_terminal_status("failed") == ("failed", "人工终止自动化任务")
    assert resolve_terminal_status("done", budget_interrupted=True) == (
        "failed",
        "Token 预算超限，工作流未完成",
    )
    assert resolve_task_status(None) == "failed"
    assert resolve_task_status("running") == "failed"
    assert resolve_task_status("done") == "done"
