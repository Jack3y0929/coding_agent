"""编排单个 SWE-bench trial 的完整生命周期。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from backend.budget import TokenBudget, bind_budget, unbind_budget
from backend.config import MAX_COST_YUAN, SWEBENCH_DATASET_NAME, SWEBENCH_WORK_ROOT
from backend.db.models import Database, init_db
from backend.evaluation.swebench.dataset import load_instance
from backend.evaluation.swebench.harness import run_harness
from backend.evaluation.swebench.models import SWEbenchTrialResult
from backend.evaluation.swebench.workflow import run_headless_repair
from backend.evaluation.swebench.workspace import extract_patch, prepare_workspace, trial_path
from backend.tools.file_tools import set_allowed_dirs
from backend.trace import bind_trace, create_trace, finish_trace, record_event, unbind_trace


async def run_trial(
    instance_id: str,
    trial_index: int = 0,
    run_id: str | None = None,
    dataset_name: str = SWEBENCH_DATASET_NAME,
    work_root: Path | None = None,
    max_workers: int = 1,
) -> SWEbenchTrialResult:
    """运行一个真实 trial，任何失败都会写入 result.json 后再返回。"""
    started_at = time.perf_counter()
    resolved_run_id = run_id or f"swebench_{uuid.uuid4().hex[:12]}"
    root = (work_root or Path(SWEBENCH_WORK_ROOT)).resolve()
    workspace = trial_path(root, resolved_run_id, instance_id, trial_index)
    artifacts_dir = workspace.parent / f"{workspace.name}-artifacts"
    result_path = artifacts_dir / "result.json"
    budget = TokenBudget(max_cost=MAX_COST_YUAN)
    trace_token = None
    budget_token = bind_budget(budget)
    trace_id: str | None = None
    session_id = f"sb_{uuid.uuid4().hex}"

    try:
        instance = load_instance(instance_id, dataset_name=dataset_name)
        await prepare_workspace(workspace, instance.repo, instance.base_commit)
        set_allowed_dirs([str(workspace)])
        await init_db()
        async with Database() as db:
            await db.create_session(session_id, "debug", str(workspace), instance.problem_statement)
        trace_id = await create_trace(
            session_id, "swebench", str(workspace), f"{instance.instance_id}: {instance.problem_statement}"
        )
        trace_token = bind_trace(trace_id)
        await record_event("swebench_instance_loaded", "input", {
            "instance_id": instance.instance_id,
            "repo": instance.repo,
            "base_commit": instance.base_commit,
        })
        await run_headless_repair(instance, workspace, budget)
        patch_path = artifacts_dir / "model.patch"
        model_patch = await extract_patch(workspace, patch_path)
        await record_event("swebench_patch_created", "output", {
            "path": str(patch_path), "size": len(model_patch),
        })
        harness = await run_harness(
            instance, model_patch, resolved_run_id, artifacts_dir,
            dataset_name=dataset_name, max_workers=max_workers,
        )
        result = SWEbenchTrialResult(
            run_id=resolved_run_id,
            instance_id=instance_id,
            trial_index=trial_index,
            status="completed",
            workspace_path=workspace,
            model_patch_path=patch_path,
            harness_report_path=harness.report_path,
            resolved=harness.resolved,
            fail_to_pass=harness.fail_to_pass,
            pass_to_pass=harness.pass_to_pass,
            duration_ms=_duration_ms(started_at),
            input_tokens=budget.total_input_tokens,
            output_tokens=budget.total_output_tokens,
        )
        await record_event("swebench_harness_result", "harness", {
            "resolved": harness.resolved,
            "fail_to_pass": harness.fail_to_pass,
            "pass_to_pass": harness.pass_to_pass,
            "report_path": str(harness.report_path),
        })
        await finish_trace("done", budget.get_status(), trace_id=trace_id)
        await _set_session_status(session_id, "done")
    except Exception as exc:
        result = SWEbenchTrialResult(
            run_id=resolved_run_id,
            instance_id=instance_id,
            trial_index=trial_index,
            status="failed",
            workspace_path=workspace,
            error_message=str(exc),
            duration_ms=_duration_ms(started_at),
            input_tokens=budget.total_input_tokens,
            output_tokens=budget.total_output_tokens,
        )
        if trace_id:
            await finish_trace("failed", budget.get_status(), str(exc), trace_id=trace_id)
            await _set_session_status(session_id, "failed")
    finally:
        if trace_token is not None:
            unbind_trace(trace_token)
        unbind_budget(budget_token)
        set_allowed_dirs([])

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


async def _set_session_status(session_id: str, status: str) -> None:
    async with Database() as db:
        await db.update_session_status(session_id, status)


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))
