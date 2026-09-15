from __future__ import annotations

import json
import asyncio
from pathlib import Path

import pytest

from backend.evaluation.featurebench.container import (
    FeatureBenchContainerError,
    _is_test_path,
    _safe_path,
)
from backend.evaluation.featurebench.dataset import task_from_record
from backend.evaluation.featurebench.harness import _instance_payload
from backend.evaluation.featurebench.metrics import summarize
from backend.evaluation.featurebench.models import FeatureBenchTrialResult
from backend.evaluation.featurebench.output import write_output
from backend.evaluation.featurebench.patch import FeatureBenchPatchError, extract_patch
from backend.evaluation.featurebench.prompt import task_prompt


def test_dataset_does_not_expose_reference_answers() -> None:
    task = task_from_record({
        "task_id": "feature-1", "problem_statement": "实现公开功能",
        "image_name": "featurebench/task:1", "level": "2",
        "fail_to_pass": '["hidden_test"]', "patch": "gold secret",
        "test_patch": "hidden secret", "repo": "owner/repo",
    })

    dumped = task.model_dump()
    prompt = task_prompt(task)
    assert dumped["instance_id"] == "feature-1"
    assert dumped["level"] == 2
    assert "patch" not in dumped and "test_patch" not in dumped
    assert "hidden_test" not in prompt
    assert "secret" not in prompt


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "src/../../secret", ""])
def test_container_path_rejects_escape(path: str) -> None:
    with pytest.raises(FeatureBenchContainerError):
        _safe_path(path)


@pytest.mark.parametrize(
    ("path", "expected"),
    [("tests/test_api.py", True), ("src/new_test.py", True), ("src/testing.py", False)],
)
def test_test_path_detection(path: str, expected: bool) -> None:
    assert _is_test_path(path) is expected


def test_patch_rejects_modified_baseline_test(tmp_path: Path) -> None:
    container = _FakeContainer(["tests/test_api.py"], {"tests/test_api.py"})
    with pytest.raises(FeatureBenchPatchError, match="基线测试"):
        asyncio.run(extract_patch(container, tmp_path / "patch.diff"))


def test_patch_allows_new_test_and_includes_untracked_file(tmp_path: Path) -> None:
    container = _FakeContainer(["tests/test_new_feature.py", "src/feature.py"], set())
    path = tmp_path / "patch.diff"
    patch = asyncio.run(extract_patch(container, path))
    assert patch == "diff --git a/src/feature.py b/src/feature.py\n"
    assert path.read_text(encoding="utf-8") == patch


def test_output_jsonl_contract(tmp_path: Path) -> None:
    path = write_output(tmp_path / "output.jsonl", "feature-1", "diff data", 1)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["instance_id"] == "feature-1"
    assert record["model_patch"] == "diff data"
    assert record["agent"] == "just_codding"
    assert record["n_attempt"] == 1


def test_harness_extracts_list_payload() -> None:
    report = {"results": [{"instance_id": "feature-1", "passed": True, "resolved": False}]}
    assert _instance_payload(report, "feature-1")["passed"] is True


def test_metrics_distinguish_pass_at_k(tmp_path: Path) -> None:
    results = [
        _trial(tmp_path, "a", 1, True), _trial(tmp_path, "a", 2, False),
        _trial(tmp_path, "b", 1, False), _trial(tmp_path, "b", 2, False),
    ]
    metrics = summarize(results)
    assert metrics["passed_rate"] == 0.25
    assert metrics["pass_at_k"] == 0.5
    assert metrics["pass_power_k"] == 0.0


class _FakeContainer:
    def __init__(self, paths: list[str], baseline_tests: set[str]) -> None:
        self._paths = paths
        self.baseline_tests = baseline_tests

    async def status_paths(self) -> list[str]:
        return self._paths

    async def exec(self, command: list[str]) -> str:
        if command[:2] == ["git", "diff"]:
            return "diff --git a/src/feature.py b/src/feature.py\n"
        return ""


def _trial(root: Path, task: str, attempt: int, passed: bool) -> FeatureBenchTrialResult:
    return FeatureBenchTrialResult(
        run_id="run", instance_id=task, attempt=attempt, status="completed",
        image_name="image", patch_path=root / "patch.diff", passed=passed,
        resolved=passed, duration_ms=100,
    )
