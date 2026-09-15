"""FeatureBench 单任务 attempt 生命周期编排。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from backend.budget import TokenBudget, bind_budget, unbind_budget
from backend.config import (
    FEATUREBENCH_DATASET,
    FEATUREBENCH_DATA_VERSION,
    FEATUREBENCH_SPLIT,
    FEATUREBENCH_WORK_ROOT,
    MAX_COST_YUAN,
)
from backend.db.models import Database, init_db
from backend.evaluation.featurebench.container import FeatureBenchContainer
from backend.evaluation.featurebench.dataset import load_task
from backend.evaluation.featurebench.dev_runner import run_feature_development
from backend.evaluation.featurebench.harness import evaluate
from backend.evaluation.featurebench.models import FeatureBenchTrialResult
from backend.evaluation.featurebench.output import write_output
from backend.evaluation.featurebench.patch import extract_patch
from backend.trace import bind_trace, create_trace, finish_trace, record_event, unbind_trace


async def run_trial(
    task_id: str,
    attempt: int = 1,
    run_id: str | None = None,
    dataset_name: str = FEATUREBENCH_DATASET,
    data_version: str = FEATUREBENCH_DATA_VERSION,
    split: str = FEATUREBENCH_SPLIT,
    work_root: Path | None = None,
) -> FeatureBenchTrialResult:
    """执行容器内开发、patch 生成和官方 fb eval。"""
    started = time.perf_counter()
    resolved_run_id = run_id or f"featurebench_{uuid.uuid4().hex[:12]}"
    root = (work_root or Path(FEATUREBENCH_WORK_ROOT)).resolve()
    artifact_dir = root / "runs" / _safe_id(resolved_run_id) / "run_outputs" / _safe_id(task_id) / f"attempt-{attempt}"
    result_path = artifact_dir / "result.json"
    budget = TokenBudget(max_cost=MAX_COST_YUAN)
    budget_token = bind_budget(budget)
    trace_token = None
    trace_id: str | None = None
    session_id = f"fb_{uuid.uuid4().hex}"
    container: FeatureBenchContainer | None = None
    task = None
    patch_path: Path | None = None
    output_path: Path | None = None
    infer_log_path: Path | None = None

    try:
        task = load_task(task_id, dataset_name=dataset_name, split=split)
        container = FeatureBenchContainer(task.image_name, resolved_run_id, task.instance_id)
        await init_db()
        async with Database() as db:
            await db.create_session(session_id, "dev", "/testbed", task.problem_statement)
        trace_id = await create_trace(session_id, "featurebench", "/testbed", task.problem_statement)
        trace_token = bind_trace(trace_id)
        await record_event("featurebench_task_loaded", "input", {
            "instance_id": task.instance_id, "image_name": task.image_name, "level": task.level,
        })
        await container.start()
        development = await run_feature_development(task, container, budget)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        infer_log_path = artifact_dir / "infer_log.json"
        infer_log_path.write_text(
            json.dumps(development, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        patch_path = artifact_dir / "patch.diff"
        model_patch = await extract_patch(container, patch_path)
        output_path = write_output(
            root / "runs" / _safe_id(resolved_run_id) / "output.jsonl",
            task.instance_id, model_patch, attempt,
        )
        await record_event("featurebench_patch_created", "output", {
            "path": str(patch_path), "size": len(model_patch),
        })
        await container.stop()
        container = None
        evaluation = await evaluate(
            output_path, task.instance_id, artifact_dir / "evaluation",
            dataset_name, data_version, split,
        )
        result = FeatureBenchTrialResult(
            run_id=resolved_run_id, instance_id=task.instance_id, attempt=attempt,
            status="completed", image_name=task.image_name, level=task.level,
            patch_path=patch_path, output_path=output_path,
            report_path=evaluation.report_path, infer_log_path=infer_log_path,
            test_output_path=evaluation.test_output_path,
            passed=evaluation.passed, resolved=evaluation.resolved,
            duration_ms=_duration_ms(started), input_tokens=budget.total_input_tokens,
            output_tokens=budget.total_output_tokens,
        )
        await record_event("featurebench_harness_result", "harness", {
            "passed": evaluation.passed, "resolved": evaluation.resolved,
            "report_path": str(evaluation.report_path),
        })
        await finish_trace("done", budget.get_status(), trace_id=trace_id)
        await _set_session_status(session_id, "done")
    except Exception as exc:
        result = FeatureBenchTrialResult(
            run_id=resolved_run_id, instance_id=task_id, attempt=attempt,
            status="failed", image_name=task.image_name if task else "",
            level=task.level if task else None, patch_path=patch_path,
            output_path=output_path, infer_log_path=infer_log_path,
            error_message=str(exc), duration_ms=_duration_ms(started),
            input_tokens=budget.total_input_tokens, output_tokens=budget.total_output_tokens,
        )
        if trace_id:
            await finish_trace("failed", budget.get_status(), str(exc), trace_id=trace_id)
            await _set_session_status(session_id, "failed")
    finally:
        if container is not None:
            await container.stop()
        if trace_token is not None:
            unbind_trace(trace_token)
        unbind_budget(budget_token)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


async def _set_session_status(session_id: str, status: str) -> None:
    async with Database() as db:
        await db.update_session_status(session_id, status)


def _safe_id(value: str) -> str:
    return "".join(char if char.isalnum() or char in "_.-" else "_" for char in value)


def _duration_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
