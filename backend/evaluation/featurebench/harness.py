"""调用 FeatureBench 官方 fb eval 并解析报告。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from backend.config import FEATUREBENCH_TASK_TIMEOUT
from backend.evaluation.featurebench.models import FeatureBenchEvalResult


class FeatureBenchHarnessError(RuntimeError):
    """fb CLI 不可用、评测失败或报告无判定。"""


async def evaluate(
    predictions_path: Path,
    instance_id: str,
    output_dir: Path,
    dataset_name: str,
    data_version: str,
    split: str,
    timeout: int = FEATUREBENCH_TASK_TIMEOUT,
) -> FeatureBenchEvalResult:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "fb", "eval",
        "--predictions-path", str(predictions_path.resolve()),
        "--dataset", dataset_name,
        "--data-version", data_version,
        "--split", split,
        "--n-concurrent", "1",
    ]
    stdout, stderr = await _run(command, output_dir, timeout)
    (output_dir / "eval.stdout.log").write_text(stdout, encoding="utf-8")
    (output_dir / "eval.stderr.log").write_text(stderr, encoding="utf-8")
    report_path, report = _find_report(output_dir, instance_id)
    payload = _instance_payload(report, instance_id)
    passed = _find_bool(payload, ("passed", "is_passed"))
    resolved = _find_bool(payload, ("resolved", "is_resolved"))
    if passed is None or resolved is None:
        raise FeatureBenchHarnessError("FeatureBench report.json 缺少 passed/resolved 判定")
    test_output = next(iter(sorted(output_dir.rglob("test_output.txt"))), None)
    return FeatureBenchEvalResult(
        passed=passed, resolved=resolved, report_path=report_path,
        test_output_path=test_output, raw_report=report,
    )


async def _run(command: list[str], cwd: Path, timeout: int) -> tuple[str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise FeatureBenchHarnessError(f"无法启动 fb CLI: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise FeatureBenchHarnessError(f"fb eval 超过 {timeout} 秒") from exc
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise FeatureBenchHarnessError(f"fb eval 失败({process.returncode}): {err[-3000:]}")
    return out, err


def _find_report(output_dir: Path, instance_id: str) -> tuple[Path, dict[str, Any]]:
    for path in sorted(output_dir.rglob("report.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _contains(report, instance_id):
            return path, report
    raise FeatureBenchHarnessError(f"fb eval 未生成任务 {instance_id} 的 report.json")


def _instance_payload(report: dict[str, Any], instance_id: str) -> dict[str, Any]:
    direct = report.get(instance_id)
    if isinstance(direct, dict):
        return direct
    for key in ("results", "instances", "tasks"):
        value = report.get(key)
        if isinstance(value, dict) and isinstance(value.get(instance_id), dict):
            return value[instance_id]
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and (item.get("instance_id") == instance_id or item.get("task_id") == instance_id):
                    return item
    return report


def _contains(value: Any, instance_id: str) -> bool:
    if isinstance(value, dict):
        return instance_id in value or any(_contains(item, instance_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, instance_id) for item in value)
    return value == instance_id


def _find_bool(value: Any, keys: tuple[str, ...]) -> bool | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, bool):
            return candidate
    for nested in value.values():
        found = _find_bool(nested, keys)
        if found is not None:
            return found
    return None
