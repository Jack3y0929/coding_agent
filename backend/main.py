from __future__ import annotations

import asyncio
import json
import logging
import uuid
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.config import PROJECT_ROOT
from backend.project_scope import real_project_path
from backend.graph.context import (
    push_progress,
    register_ws,
    unregister_ws,
    resume_session,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("main")

app = FastAPI(title="Just_codding")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def startup_event() -> None:
    """初始化数据库并回收服务重启后遗留的工作流。"""
    from backend.db.models import init_db
    from backend.trace import recover_stale_traces, recover_stale_sessions

    await init_db()
    await recover_orphan_workflows()
    recovered = await recover_stale_traces()
    recovered_sessions = await recover_stale_sessions()
    if recovered:
        logger.warning("启动时回收 %s 个陈旧 Trace", recovered)
    if recovered_sessions:
        logger.warning("启动时回收 %s 个陈旧会话", recovered_sessions)


async def recover_orphan_workflows() -> int:
    """回收没有对应任务文件的遗留 running 会话。"""
    from backend.db.models import Database
    task_dir = Path(PROJECT_ROOT) / ".workflow_tasks"
    async with Database() as db:
        rows = await (await db._conn.execute(
            "SELECT id FROM sessions WHERE status = 'running'"
        )).fetchall()
        recovered = 0
        for row in rows:
            if not (task_dir / f"{row['id']}.json").exists():
                await db._conn.execute("UPDATE sessions SET status='failed' WHERE id=?", (row["id"],))
                await db._conn.execute(
                    "UPDATE workflow_traces SET status='failed', finished_at=datetime('now'), "
                    "error_message=? WHERE session_id=? AND status='running'",
                    ("Worker 进程已退出，任务状态自动回收", row["id"]),
                )
                recovered += 1
        await db._conn.commit()
    return recovered


# ---- 请求模型 ----
class StartWorkflowRequest(BaseModel):
    workflow_type: str | None = None  # 'dev' | 'debug'，为空则自动识别
    description: str
    project_path: str


class ResumeRequest(BaseModel):
    session_id: str
    human_decision: str | None = None  # 'approved' | 'rejected'
    new_logs: str | None = None
    outcome: str | None = None
    requirement_fit: str | None = None
    code_quality: str | None = None
    review_effectiveness: str | None = None
    diagnosis_effectiveness: str | None = None
    adoption: str | None = None
    fix_effectiveness: str | None = None
    issue_types: list[str] = Field(default_factory=list)
    note: str = ""
    preference_title: str = ""
    preference_content: str = ""
    preference_scope_type: str = "project"
    preference_scope_value: str = ""
    intent_workflow_type: str | None = None
    target_modules: list[str] = Field(default_factory=list)
    change_scope: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    tech_constraints: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    observed_behavior: str = ""
    expected_behavior: str = ""


class HumanDecisionRequest(BaseModel):
    """轻量人工验收接口请求体。"""
    note: str = ""


class TraceLabelRequest(BaseModel):
    outcome: str
    requirement_fit: str | None = None
    code_quality: str | None = None
    review_effectiveness: str | None = None
    diagnosis_effectiveness: str | None = None
    adoption: str | None = None
    fix_effectiveness: str | None = None
    issue_types: list[str] = Field(default_factory=list)
    note: str = ""
    preference_title: str = ""
    preference_content: str = ""
    preference_scope_type: str = "project"
    preference_scope_value: str = ""


# ---- 路由 ----
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/workflow/run")
async def start_workflow(req: StartWorkflowRequest) -> dict[str, Any]:
    """启动一个新工作流"""
    session_id = str(uuid.uuid4())[:8]
    if req.workflow_type not in (None, "dev", "debug"):
        raise HTTPException(status_code=400, detail=f"无效的工作流类型: {req.workflow_type}")
    try:
        normalized_project = real_project_path(req.project_path)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"项目路径无效: {exc}") from exc
    req = req.model_copy(update={"project_path": normalized_project})
    logger.info(f"提交工作流 session={session_id} type={req.workflow_type or 'auto'}")
    task_dir = Path(PROJECT_ROOT) / ".workflow_tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    task_file = task_dir / f"{session_id}.json"
    task_file.write_text(json.dumps({"session_id": session_id, "request": req.model_dump()}, ensure_ascii=False), encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [sys.executable, "-m", "backend.worker", str(task_file)],
        cwd=PROJECT_ROOT,
        creationflags=creationflags,
        close_fds=(creationflags == 0),
    )
    return {"session_id": session_id, "status": "started", "workflow_type": req.workflow_type or "auto"}


@app.get("/api/workflow/status/{session_id}")
async def get_status(session_id: str) -> dict[str, Any]:
    """查询工作流当前状态"""
    from backend.db.models import get_session, get_artifacts_by_session
    from backend.graph.context import get_progress_snapshot
    async with get_session() as db_session:
        session_data = await db_session.get_session(session_id)
        if not session_data:
            return {"error": "会话不存在", "session_id": session_id}
        artifacts = await db_session.get_artifacts_by_session(session_id)
        progress = get_progress_snapshot(session_id) or {}
        return {
            "session_id": session_data["id"],
            "type": session_data["type"],
            "status": session_data["status"],
            "created_at": session_data["created_at"],
            "artifacts": [
                {"type": a["type"], "created_at": a["created_at"]}
                for a in artifacts
            ],
            "progress": progress,
        }


@app.get("/api/workflow/events/{session_id}")
async def get_workflow_events(session_id: str) -> dict[str, Any]:
    """按会话返回完整 Trace 时间线和正式产物。"""
    from backend.db.models import init_db
    from backend.trace import get_trace_by_session
    await init_db()
    trace = await get_trace_by_session(session_id)
    if not trace:
        return {"error": "会话不存在", "session_id": session_id}
    return {"session_id": session_id, "trace_id": trace["trace_id"],
            "events": trace["events"], "artifacts": trace["artifacts"]}


@app.get("/api/workflow/quality/{session_id}")
async def get_workflow_quality(session_id: str) -> dict[str, Any]:
    """返回单次工作流的确定性质量指标。"""
    from backend.db.models import init_db
    from backend.trace import get_trace_by_session
    from backend.evaluation.service import evaluate_trace
    await init_db()
    trace = await get_trace_by_session(session_id)
    if not trace:
        return {"error": "会话不存在", "session_id": session_id}
    metrics = await evaluate_trace(trace["trace_id"])
    return {"session_id": session_id, "trace_id": trace["trace_id"], "metrics": metrics}


async def _resume_endpoint(session_id: str, decision: str, note: str = "") -> dict[str, str]:
    from backend.graph.context import resume_session
    ok = resume_session(session_id, decision, feedback={"outcome": "approved" if decision == "approved" else "rejected", "note": note})
    if not ok:
        return {"status": "error", "message": "无等待中的会话", "session_id": session_id}
    return {"status": "resumed", "session_id": session_id}


@app.post("/api/workflow/accept/{session_id}")
async def accept_workflow(session_id: str, req: HumanDecisionRequest | None = None) -> dict[str, str]:
    return await _resume_endpoint(session_id, "approved", req.note if req else "")


@app.post("/api/workflow/reject/{session_id}")
async def reject_workflow(session_id: str, req: HumanDecisionRequest | None = None) -> dict[str, str]:
    return await _resume_endpoint(session_id, "rejected", req.note if req else "")


@app.get("/api/traces")
async def get_traces(
    limit: int = 50,
    workflow_type: str | None = None,
    project_path: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """查询工作流 Trace 列表。"""
    from backend.db.models import init_db
    from backend.trace import list_traces
    await init_db()
    return await list_traces(limit, workflow_type, project_path, status)


@app.get("/api/rag/sources")
async def get_rag_sources(
    source_type: str | None = None,
    project_path: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """查看 RAG 已索引的来源清单。"""
    from backend.db.models import Database, init_db
    await init_db()
    if limit < 1 or limit > 1000 or offset < 0:
        return []
    async with Database() as db:
        return await db.list_rag_sources(source_type, project_path, limit, offset)


@app.get("/api/rag/chunks")
async def get_rag_chunks(
    source_path: str,
    source_type: str | None = None,
    project_path: str | None = None,
    include_embedding: bool = False,
) -> list[dict[str, Any]]:
    """查看指定来源的 RAG 分块、元数据与内容。"""
    from backend.db.models import Database, init_db
    await init_db()
    async with Database() as db:
        return await db.get_rag_chunks(source_path, source_type, project_path, include_embedding)


@app.get("/api/traces/{trace_id}")
async def get_trace_detail(trace_id: str) -> dict[str, Any]:
    """查询 Trace 时间线、产物与人工标注。"""
    from backend.db.models import init_db
    from backend.trace import get_trace
    await init_db()
    trace = await get_trace(trace_id)
    if not trace:
        return {"error": "Trace 不存在", "trace_id": trace_id}
    return trace


@app.post("/api/traces/{trace_id}/labels")
async def label_trace(trace_id: str, req: TraceLabelRequest) -> dict[str, Any]:
    """为历史 Trace 补充运营标注和明确的人类偏好。"""
    from backend.db.models import init_db
    from backend.trace import add_trace_label, get_trace
    await init_db()
    if not await get_trace(trace_id):
        return {"error": "Trace 不存在", "trace_id": trace_id}
    if not req.outcome:
        return {"error": "outcome 为必填项"}
    return await add_trace_label(trace_id, req.model_dump())


@app.post("/api/evaluations/run")
async def run_evaluation(
    workflow_type: str | None = None,
    project_path: str | None = None,
) -> dict[str, Any]:
    """生成基于 Trace 与人工标签的规则评测报告。"""
    from backend.db.models import init_db
    from backend.evaluation.service import build_evaluation_report
    await init_db()
    return await build_evaluation_report(workflow_type, project_path)


@app.get("/api/memories")
async def get_memories(limit: int = 100) -> list[dict[str, Any]]:
    """查看已沉淀的人类偏好记忆。"""
    from backend.db.models import init_db
    from backend.trace import list_preferences
    await init_db()
    return await list_preferences(limit)


class UpdateMemoryRequest(BaseModel):
    content: str | None = None
    status: str | None = None


@app.put("/api/memories/{memory_id}")
async def edit_memory(memory_id: int, req: UpdateMemoryRequest) -> dict[str, Any]:
    """修改、停用或恢复一条偏好记忆。"""
    from backend.db.models import init_db
    from backend.trace import update_preference
    await init_db()
    try:
        updated = await update_preference(memory_id, req.content, req.status)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"updated": updated, "memory_id": memory_id}


@app.post("/api/workflow/resume")
async def resume_workflow(req: ResumeRequest) -> dict[str, str]:
    """暂停节点恢复继续（人工验收/日志复现后）"""
    session_id = req.session_id
    logger.info(f"恢复工作流 session={session_id} decision={req.human_decision}")

    intent_fields = {
        "intent_workflow_type", "target_modules", "change_scope", "protected_paths",
        "acceptance_criteria", "tech_constraints", "validation_commands", "reproduction_steps",
        "observed_behavior", "expected_behavior",
    }
    payload = req.model_dump(exclude={"session_id", "human_decision", "new_logs"})
    intent_update = {
        key.removeprefix("intent_"): value
        for key, value in payload.items() if key in intent_fields
    }
    feedback = {key: value for key, value in payload.items() if key not in intent_fields}
    ok = resume_session(session_id, req.human_decision, req.new_logs, feedback, intent_update)
    if ok:
        return {"status": "resumed", "session_id": session_id}
    return {"status": "error", "message": "无等待中的会话", "session_id": session_id}


@app.websocket("/ws/progress/{session_id}")
async def websocket_progress(websocket: WebSocket, session_id: str) -> None:
    """WebSocket实时推送工作流进度"""
    await websocket.accept()
    register_ws(session_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        unregister_ws(session_id, websocket)


# ---- 工作流运行器 ----
async def run_workflow_async(session_id: str, req: StartWorkflowRequest) -> None:
    """后台异步运行工作流"""
    from backend.budget import BudgetExceededError, TokenBudget, bind_budget, unbind_budget
    from backend.db.models import Database, init_db, create_session, update_session_status
    from backend.intent.context import bind_intent, unbind_intent
    from backend.intent.rules import validate_slots
    from backend.intent.service import extract_coding_intent, merge_intent_updates
    from backend.graph.context import wait_for_human
    from backend.trace import bind_trace, create_trace, finish_trace, record_event, record_stage_output, unbind_trace, update_trace_workflow_type

    budget = TokenBudget()
    trace_token = None
    intent_token = None
    budget_token = bind_budget(budget)
    try:
        req = req.model_copy(update={"project_path": real_project_path(req.project_path)})
        await init_db()
        requested_workflow = req.workflow_type or "auto"
        await create_session(
            session_id,
            "debug" if req.workflow_type == "debug" else "dev",
            req.project_path,
            req.description,
        )
        trace_id = await create_trace(session_id, requested_workflow, req.project_path, req.description)
        trace_token = bind_trace(trace_id)

        manual_workflow_type = req.workflow_type
        intent = await extract_coding_intent(req.description, req.project_path, manual_workflow_type, budget)
        validation = validate_slots(intent, manual_workflow_type)
        await record_event("intent_extracted", "intent", {"intent": intent.model_dump()})
        await record_event("slot_validation", "intent", validation.model_dump())

        # 在选择具体状态图前完成槽位澄清，自动识别任务可在此稳定路由到 DEV 或 DEBUG。
        while validation.decision == "clarify":
            await record_event("clarification_requested", "intent", validation.model_dump())
            await push_progress(session_id, {
                "event": "progress", "stage": "intent_clarify",
                "message": "研发槽位不足或流程冲突，等待补充",
                "intent": intent.model_dump(), "validation": validation.model_dump(),
                "token_status": budget.get_status(),
            })
            response = await wait_for_human(session_id, "intent_clarify")
            updates = response.get("intent_update", {})
            intent = merge_intent_updates(intent, updates)
            if updates.get("workflow_type") in {"dev", "debug"}:
                manual_workflow_type = str(updates["workflow_type"])
            validation = validate_slots(intent, manual_workflow_type)
            await record_event("slots_updated", "intent", {"intent": intent.model_dump()})
            await record_event("slot_validation", "intent", validation.model_dump())

        async with Database() as db:
            await db.update_session_workflow_type(session_id, validation.selected_workflow_type)
        await update_trace_workflow_type(trace_id, validation.selected_workflow_type)
        intent_token = bind_intent(intent)

        await push_progress(session_id, {
            "event": "progress",
            "stage": "init",
            "message": "工作流初始化中...",
            "token_status": budget.get_status(),
        })

        if validation.selected_workflow_type == "dev":
            from backend.graph.dev_workflow import build_dev_workflow
            graph = await build_dev_workflow()
            from backend.artifacts import allocate_document_id
            document_id = allocate_document_id("T")
        else:
            from backend.graph.debug_workflow import build_debug_workflow
            graph = await build_debug_workflow()
            from backend.artifacts import allocate_document_id
            document_id = allocate_document_id("D")

        initial_state = {
            "stage": "input_gate",
            "coding_intent": intent.model_dump(),
            "slot_validation": validation.model_dump(),
            "manual_workflow_type": manual_workflow_type,
            "clarification_doc": None,
            "requirement_doc": None,
            "plan": None,
            "code_changes": None,
            "implementation_note": None,
            "review_report": None,
            "review_passed": False,
            "review_round": 0,
            "fix_attempt": 0,
            "diagnosis_report": None,
            "diagnosis_sufficient": False,
            "diagnosis_evidence": {},
            "new_logs": None,
            "token_used": 0,
            "status": "running",
            "human_decision": None,
            "intervention_decision": None,
            "validation_passed": None,
            "session_id": session_id,
            "project_path": req.project_path,
            "description": req.description,
            "workflow_type": validation.selected_workflow_type,
            "document_id": document_id,
        }

        config = {"configurable": {"thread_id": session_id}}

        async for event in graph.astream(initial_state, config):
            await _handle_graph_event(session_id, event, budget)
            for node_name, node_output in event.items():
                if node_output is not None:
                    await record_stage_output(node_name, node_output)

        await update_session_status(session_id, "done")
        await finish_trace("done", budget.get_status())
        from backend.evaluation.service import evaluate_trace
        await evaluate_trace(trace_id)
        await push_progress(session_id, {
            "event": "complete",
            "message": "工作流执行完成",
            "token_status": budget.get_status(),
        })

    except asyncio.CancelledError as exc:
        logger.warning("工作流 %s 被取消", session_id)
        await update_session_status(session_id, "failed")
        if trace_token is not None:
            await finish_trace("failed", budget.get_status(), "工作流被服务重启或任务取消")
        raise
    except Exception as exc:
        logger.error(f"工作流 {session_id} 异常: {exc}", exc_info=True)
        from backend.fallback.engine import budget_exceeded
        from backend.trace import record_fallback_decision
        if isinstance(exc, BudgetExceededError):
            await record_fallback_decision(budget_exceeded(), "runtime")
        await update_session_status(session_id, "failed")
        await finish_trace("failed", budget.get_status(), str(exc))
        if trace_token is not None:
            from backend.evaluation.service import evaluate_trace
            await evaluate_trace(trace_id)
        await push_progress(session_id, {
            "event": "error",
            "message": str(exc),
            "token_status": budget.get_status(),
        })
    finally:
        unbind_budget(budget_token)
        if intent_token is not None:
            unbind_intent(intent_token)
        if trace_token is not None:
            unbind_trace(trace_token)


async def _handle_graph_event(session_id: str, event: dict[str, Any], budget: Any) -> None:
    """处理 LangGraph 流的每个事件，推送进度"""
    for node_name, node_output in event.items():
        if node_output is None:
            continue

        stage_map = {
            "input_gate": "澄清需求",
            "analyze": "需求分析",
            "develop_plan": "开发规划",
            "develop_build": "开发编码",
            "review": "代码审查",
            "fix": "修复问题",
            "human_accept": "等待验收",
            "output": "生成总结",
            "diagnose": "BUG诊断",
            "add_logging": "添加日志",
            "validate": "验证变更",
            "human_wait": "等待复现",
            "human_intervene": "人工介入",
        }
        stage_name = stage_map.get(node_name, node_name)

        await push_progress(session_id, {
            "event": "progress",
            "stage": node_name,
            "stage_name": stage_name,
            "message": f"正在执行: {stage_name}",
            "token_status": budget.get_status(),
        })


if __name__ == "__main__":
    import uvicorn
    # 与 frontend/vite.config.js 的开发代理保持一致。
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=True)
