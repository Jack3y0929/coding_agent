"""调用并解析官方 SWE-bench Docker Harness。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from backend.config import SWEBENCH_DOCKER_REQUIRED, SWEBENCH_TRIAL_TIMEOUT
from backend.evaluation.swebench.models import HarnessResult, SWEbenchInstance


class HarnessError(RuntimeError):
    """官方 Harness 配置、执行或报告解析失败。"""


async def run_harness(
    instance: SWEbenchInstance,
    model_patch: str,
    run_id: str,
    output_dir: Path,
    dataset_name: str,
    max_workers: int = 1,
    timeout: int = SWEBENCH_TRIAL_TIMEOUT,
) -> HarnessResult:
    """生成 predictions 并通过官方模块执行指定实例。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    if SWEBENCH_DOCKER_REQUIRED:
        await _check_docker()

    predictions_path = output_dir / "predictions.json"
    predictions_path.write_text(json.dumps([{
        "instance_id": instance.instance_id,
        "model_name_or_path": "just_codding",
        "model_patch": model_patch,
    }], ensure_ascii=False, indent=2), encoding="utf-8")

    command = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset_name,
        "--predictions_path", str(predictions_path),
        "--max_workers", str(max_workers),
        "--run_id", run_id,
        "--instance_ids", instance.instance_id,
    ]
    stdout, stderr = await _run(command, output_dir, timeout)
    (output_dir / "harness.stdout.log").write_text(stdout, encoding="utf-8")
    (output_dir / "harness.stderr.log").write_text(stderr, encoding="utf-8")
    report_path, report = _find_report(output_dir, instance.instance_id)
    return _normalize_report(report_path, report, instance.instance_id)


async def _check_docker() -> None:
    stdout, _ = await _run(["docker", "info", "--format", "{{.ServerVersion}}"], None, 30)
    if not stdout.strip():
        raise HarnessError("Docker 服务不可用或未返回 ServerVersion")


async def _run(
    command: list[str], cwd: Path | None, timeout: int
) -> tuple[str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as exc:
        raise HarnessError(f"无法启动命令 {command[0]}: {exc}") from exc
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise HarnessError(f"Harness 执行超过 {timeout} 秒") from exc
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise HarnessError(
            f"命令执行失败({process.returncode}): {' '.join(command[:3])}\n{stderr[-3000:]}"
        )
    return stdout, stderr


def _find_report(output_dir: Path, instance_id: str) -> tuple[Path, dict[str, Any]]:
    candidates = sorted(output_dir.rglob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in candidates:
        if path.name == "predictions.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if _contains_instance(data, instance_id):
            return path, data
    raise HarnessError(f"Harness 成功退出但未找到实例 {instance_id} 的 JSON 报告")


def _contains_instance(value: Any, instance_id: str) -> bool:
    if isinstance(value, dict):
        return instance_id in value or any(_contains_instance(item, instance_id) for item in value.values())
    if isinstance(value, list):
        return any(_contains_instance(item, instance_id) for item in value)
    return value == instance_id


def _normalize_report(path: Path, report: dict[str, Any], instance_id: str) -> HarnessResult:
    payload: Any = report.get(instance_id, report)
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict):
        raise HarnessError("Harness 报告中实例结果不是对象")

    resolved = _find_bool(payload, ("resolved", "is_resolved"))
    if resolved is None:
        resolved_ids = report.get("resolved_ids", [])
        unresolved_ids = report.get("unresolved_ids", [])
        if instance_id in resolved_ids:
            resolved = True
        elif instance_id in unresolved_ids:
            resolved = False
    if resolved is None:
        raise HarnessError("Harness 报告缺少 resolved 判定")
    return HarnessResult(
        resolved=resolved,
        fail_to_pass=_find_bool(payload, ("fail_to_pass", "FAIL_TO_PASS")),
        pass_to_pass=_find_bool(payload, ("pass_to_pass", "PASS_TO_PASS")),
        report_path=path,
        raw_report=report,
    )


def _find_bool(value: Any, keys: tuple[str, ...]) -> bool | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, bool):
            return candidate
        if isinstance(candidate, dict):
            for nested_key in ("success", "passed"):
                nested = candidate.get(nested_key)
                if isinstance(nested, bool):
                    return nested
    for nested in value.values():
        result = _find_bool(nested, keys)
        if result is not None:
            return result
    return None
