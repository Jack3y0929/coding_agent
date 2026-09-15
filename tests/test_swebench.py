from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.evaluation.swebench.dataset import instance_from_record
from backend.evaluation.swebench.harness import HarnessError, _normalize_report
from backend.evaluation.swebench.metrics import summarize
from backend.evaluation.swebench.models import SWEbenchTrialResult
from backend.evaluation.swebench.prompt import issue_prompt
from backend.evaluation.swebench.workspace import _is_test_path, trial_path


def test_dataset_record_does_not_expose_reference_patches() -> None:
    instance = instance_from_record({
        "instance_id": "owner__repo-1",
        "repo": "owner/repo",
        "base_commit": "abc123",
        "problem_statement": "公开问题描述",
        "fail_to_pass": json.dumps(["test_a"]),
        "pass_to_pass": ["test_b"],
        "patch": "绝不能暴露的参考修复",
        "test_patch": "绝不能暴露的测试补丁",
    })

    dumped = instance.model_dump()
    prompt = issue_prompt(instance)
    assert "patch" not in dumped
    assert "test_patch" not in dumped
    assert "绝不能暴露" not in prompt
    assert "test_a" not in prompt


def test_trial_path_is_sanitized_and_scoped(tmp_path: Path) -> None:
    path = trial_path(tmp_path, "run/../id", "repo/../../task", 0)

    assert path.is_relative_to(tmp_path.resolve())
    assert path.name == "trial-0"
    assert ".." not in path.parts


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_feature.py", True),
        ("package/test/parser.py", True),
        ("src/test_parser.py", True),
        ("src/parser_test.py", True),
        ("src/testing_helpers.py", False),
    ],
)
def test_test_file_detection(path: str, expected: bool) -> None:
    assert _is_test_path(path) is expected


def test_harness_report_is_normalized(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report = {
        "owner__repo-1": {
            "resolved": True,
            "tests_status": {
                "FAIL_TO_PASS": {"success": True},
                "PASS_TO_PASS": {"success": True},
            },
        }
    }

    result = _normalize_report(report_path, report, "owner__repo-1")

    assert result.resolved is True
    assert result.fail_to_pass is True
    assert result.pass_to_pass is True


def test_harness_report_without_verdict_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(HarnessError, match="resolved"):
        _normalize_report(tmp_path / "report.json", {"owner__repo-1": {}}, "owner__repo-1")


def test_metrics_distinguish_pass_at_k_and_pass_power_k(tmp_path: Path) -> None:
    results = [
        _trial(tmp_path, "task-a", 0, True),
        _trial(tmp_path, "task-a", 1, False),
        _trial(tmp_path, "task-b", 0, False),
        _trial(tmp_path, "task-b", 1, False),
    ]

    metrics = summarize(results)

    assert metrics["resolved_rate"] == 0.25
    assert metrics["pass_at_k"] == 0.5
    assert metrics["pass_power_k"] == 0.0


def _trial(
    root: Path, instance_id: str, trial_index: int, resolved: bool
) -> SWEbenchTrialResult:
    return SWEbenchTrialResult(
        run_id="run",
        instance_id=instance_id,
        trial_index=trial_index,
        status="completed",
        workspace_path=root / instance_id / str(trial_index),
        resolved=resolved,
        duration_ms=100,
    )
