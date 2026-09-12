"""工作流 Trace、人工标注、规则评测与偏好记忆服务。"""

from __future__ import annotations

import contextvars
import json
import time
import uuid
from typing import Any, Optional

from backend.db.models import Database, _now


_active_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "active_trace_id", default=None
)


def current_trace_id() -> Optional[str]:
    """返回当前异步任务关联的 Trace 标识。"""
    return _active_trace_id.get()


def bind_trace(trace_id: str) -> contextvars.Token[Optional[str]]:
    """将 Trace 绑定到当前异步上下文。"""
    return _active_trace_id.set(trace_id)


def unbind_trace(token: contextvars.Token[Optional[str]]) -> None:
    """恢复绑定前的异步上下文。"""
    _active_trace_id.reset(token)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _summary(value: Any, limit: int = 1200) -> str:
    text = value if isinstance(value, str) else _json(value)
    return text if len(text) <= limit else f"{text[:limit]}...（已截断）"


async def create_trace(
    session_id: str,
    workflow_type: str,
    project_path: str,
    description: str,
) -> str:
    """创建一次工作流执行 Trace。"""
    trace_id = f"trace_{uuid.uuid4().hex}"
    async with Database() as db:
        await db._conn.execute(
            "INSERT INTO workflow_traces "
            "(trace_id, session_id, workflow_type, project_path, description, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (trace_id, session_id, workflow_type, project_path, description, _now()),
        )
        await db._conn.commit()
    await record_event("workflow_started", "init", {
        "workflow_type": workflow_type,
        "project_path": project_path,
        "description": _summary(description, 500),
    }, trace_id=trace_id)
    return trace_id


async def record_event(
    event_type: str,
    stage: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    duration_ms: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> None:
    """记录不含角色推理过程的结构化运行事件。"""
    resolved_trace_id = trace_id or current_trace_id()
    if not resolved_trace_id:
        return
    session_id: Optional[str] = None
    event_id: Optional[int] = None
    async with Database() as db:
        trace_row = await (await db._conn.execute(
            "SELECT session_id FROM workflow_traces WHERE trace_id = ?", (resolved_trace_id,)
        )).fetchone()
        if trace_row:
            session_id = str(trace_row["session_id"])
        created_at = _now()
        cursor = await db._conn.execute(
            "INSERT INTO trace_events (trace_id, event_type, stage, payload, duration_ms, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (resolved_trace_id, event_type, stage, _json(payload or {}), duration_ms, created_at),
        )
        event_id = cursor.lastrowid
        await db._conn.commit()
    if session_id:
        from backend.graph.context import push_trace_event
        await push_trace_event(session_id, {
            "id": event_id,
            "trace_id": resolved_trace_id,
            "event_type": event_type,
            "stage": stage,
            "payload": payload or {},
            "duration_ms": duration_ms,
            "created_at": created_at,
        })


async def finish_trace(
    status: str,
    token_status: Optional[dict[str, Any]] = None,
    error_message: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> None:
    """结束 Trace 并固化时长、Token 和错误摘要。"""
    resolved_trace_id = trace_id or current_trace_id()
    if not resolved_trace_id:
        return
    async with Database() as db:
        row = await (await db._conn.execute(
            "SELECT started_at FROM workflow_traces WHERE trace_id = ?", (resolved_trace_id,)
        )).fetchone()
        duration_ms = None
        if row:
            from datetime import datetime
            started = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            duration_ms = max(0, int((datetime.now(started.tzinfo) - started).total_seconds() * 1000))
        token_status = token_status or {}
        token_rows = await (await db._conn.execute(
            "SELECT payload FROM trace_events WHERE trace_id = ? AND event_type = 'llm_call'",
            (resolved_trace_id,),
        )).fetchall()
        measured_input = 0
        measured_output = 0
        for token_row in token_rows:
            payload = json.loads(token_row["payload"])
            measured_input += int(payload.get("input_tokens") or 0)
            measured_output += int(payload.get("output_tokens") or 0)
        await db._conn.execute(
            "UPDATE workflow_traces SET status = ?, finished_at = ?, duration_ms = ?, "
            "input_tokens = ?, output_tokens = ?, error_message = ? WHERE trace_id = ?",
            (status, _now(), duration_ms, measured_input or token_status.get("total_input_tokens", 0),
             measured_output or token_status.get("total_output_tokens", 0), error_message, resolved_trace_id),
        )
        await db._conn.commit()
    await record_event(
        "workflow_finished" if status == "done" else "workflow_failed",
        "output",
        {"status": status, "error": error_message, "token_status": token_status},
        trace_id=resolved_trace_id,
    )


async def recover_stale_traces(max_age_minutes: int = 30) -> int:
    """将服务重启或异常退出遗留的 running Trace 标记为失败。"""
    async with Database() as db:
        cursor = await db._conn.execute(
            "UPDATE workflow_traces SET status = 'failed', finished_at = ?, "
            "error_message = ? WHERE status = 'running' "
            "AND started_at < datetime('now', ?)",
            (_now(), "工作流未正常收尾，已在服务启动时自动回收", f"-{max_age_minutes} minutes"),
        )
        await db._conn.commit()
        return int(cursor.rowcount or 0)


async def recover_stale_sessions(max_age_minutes: int = 30) -> int:
    """将服务重启遗留的 running 会话标记为失败。"""
    async with Database() as db:
        cursor = await db._conn.execute(
            "UPDATE sessions SET status = 'failed' WHERE status = 'running' "
            "AND created_at < datetime('now', ?)",
            (f"-{max_age_minutes} minutes",),
        )
        await db._conn.commit()
        return int(cursor.rowcount or 0)


async def repair_legacy_terminal_statuses() -> int:
    """校正旧版本将人工 stop 错记为 done 的 Trace，保留原事件并追加失败事件。"""
    async with Database() as db:
        rows = await (await db._conn.execute(
            "SELECT t.trace_id, t.session_id, e.payload "
            "FROM workflow_traces t "
            "JOIN trace_events e ON e.trace_id = t.trace_id "
            "WHERE t.status = 'done' AND e.event_type = 'human_intervention'"
        )).fetchall()
        repaired: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("decision") != "stop" or row["trace_id"] in repaired:
                continue
            error_message = "人工终止自动化任务（历史终态已校正）"
            await db._conn.execute(
                "UPDATE workflow_traces SET status = 'failed', error_message = ? "
                "WHERE trace_id = ?",
                (error_message, row["trace_id"]),
            )
            await db._conn.execute(
                "UPDATE sessions SET status = 'failed' WHERE id = ? AND status = 'done'",
                (row["session_id"],),
            )
            await db._conn.execute(
                "INSERT INTO trace_events "
                "(trace_id, event_type, stage, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    row["trace_id"],
                    "workflow_failed",
                    "output",
                    _json({
                        "status": "failed",
                        "error": error_message,
                        "reason_code": "legacy_terminal_status_repaired",
                    }),
                    _now(),
                ),
            )
            repaired.add(row["trace_id"])
        await db._conn.commit()
    return len(repaired)


async def update_trace_workflow_type(trace_id: str, workflow_type: str) -> None:
    """在自动识别或澄清后记录最终实际执行的工作流类型。"""
    async with Database() as db:
        await db._conn.execute(
            "UPDATE workflow_traces SET workflow_type = ? WHERE trace_id = ?",
            (workflow_type, trace_id),
        )
        await db._conn.commit()


async def record_fallback_decision(decision: Any, stage: str | None = None) -> None:
    """将统一 Fallback 决策持久化为可回溯的 Trace 事件。"""
    payload = decision.model_dump() if hasattr(decision, "model_dump") else dict(decision)
    await record_event("fallback_decision", stage, payload)


class TraceStage:
    """自动记录节点进入、退出与异常的异步上下文管理器。"""

    def __init__(self, stage: str, input_summary: Any) -> None:
        self.stage = stage
        self.input_summary = input_summary
        self.started_at = 0.0

    async def __aenter__(self) -> "TraceStage":
        self.started_at = time.perf_counter()
        await record_event("stage_started", self.stage, {"input": _summary(self.input_summary)})
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        duration_ms = int((time.perf_counter() - self.started_at) * 1000)
        if exc:
            await record_event("stage_failed", self.stage, {"error": str(exc)}, duration_ms)
        else:
            await record_event("stage_finished", self.stage, {}, duration_ms)


async def record_stage_output(stage: str, output: Any) -> None:
    """记录节点正式产出摘要。"""
    await record_event("stage_output", stage, {"output": _summary(output)})


async def list_traces(
    limit: int = 50,
    workflow_type: Optional[str] = None,
    project_path: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """按条件查询 Trace 列表，附带最新人工标签。"""
    clauses: list[str] = []
    params: list[Any] = []
    if workflow_type:
        clauses.append("t.workflow_type = ?")
        params.append(workflow_type)
    if project_path:
        clauses.append("t.project_path LIKE ?")
        params.append(f"%{project_path}%")
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 200)))
    sql = (
        "SELECT t.*, l.outcome AS label_outcome, l.adoption AS label_adoption "
        "FROM workflow_traces t "
        "LEFT JOIN trace_labels l ON l.id = (SELECT id FROM trace_labels "
        "WHERE trace_id = t.trace_id ORDER BY id DESC LIMIT 1) "
        f"{where} ORDER BY t.started_at DESC LIMIT ?"
    )
    async with Database() as db:
        rows = await (await db._conn.execute(sql, tuple(params))).fetchall()
    return [dict(row) for row in rows]


async def get_trace(trace_id: str) -> Optional[dict[str, Any]]:
    """返回 Trace 主记录、事件、产物和标注。"""
    async with Database() as db:
        trace = await (await db._conn.execute(
            "SELECT * FROM workflow_traces WHERE trace_id = ?", (trace_id,)
        )).fetchone()
        if not trace:
            return None
        events = await (await db._conn.execute(
            "SELECT * FROM trace_events WHERE trace_id = ? ORDER BY id", (trace_id,)
        )).fetchall()
        labels = await (await db._conn.execute(
            "SELECT * FROM trace_labels WHERE trace_id = ? ORDER BY id DESC", (trace_id,)
        )).fetchall()
        evaluations = await (await db._conn.execute(
            "SELECT metric_name, score, source, evidence_json, created_at FROM trace_evaluations "
            "WHERE trace_id = ? ORDER BY id", (trace_id,)
        )).fetchall()
        artifacts = await (await db._conn.execute(
            "SELECT type, content, created_at FROM artifacts WHERE session_id = ? ORDER BY id",
            (trace["session_id"],),
        )).fetchall()
    item = dict(trace)
    item["events"] = [{**dict(row), "payload": json.loads(row["payload"])} for row in events]
    item["labels"] = [{**dict(row), "issue_types": json.loads(row["issue_types"])} for row in labels]
    item["evaluations"] = [{
        **dict(row), "evidence": json.loads(row["evidence_json"]),
    } for row in evaluations]
    item["artifacts"] = [dict(row) for row in artifacts]
    return item


async def get_trace_by_session(session_id: str) -> Optional[dict[str, Any]]:
    """按会话获取最近一次 Trace，供工作流状态 API 使用。"""
    async with Database() as db:
        row = await (await db._conn.execute(
            "SELECT trace_id FROM workflow_traces WHERE session_id = "
            "? ORDER BY started_at DESC LIMIT 1", (session_id,)
        )).fetchone()
    return await get_trace(row["trace_id"]) if row else None


async def add_trace_label(trace_id: str, label: dict[str, Any]) -> dict[str, Any]:
    """写入人工验收/运营标注，并将明确偏好沉淀进长期记忆。"""
    issue_types = label.get("issue_types") or []
    async with Database() as db:
        cursor = await db._conn.execute(
            "INSERT INTO trace_labels (trace_id, outcome, requirement_fit, code_quality, "
            "review_effectiveness, diagnosis_effectiveness, adoption, fix_effectiveness, "
            "issue_types, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trace_id, label["outcome"], label.get("requirement_fit"), label.get("code_quality"),
             label.get("review_effectiveness"), label.get("diagnosis_effectiveness"),
             label.get("adoption"), label.get("fix_effectiveness"), _json(issue_types),
             label.get("note", ""), _now()),
        )
        await db._conn.commit()
        label_id = cursor.lastrowid
    await record_event("human_label_added", "human_accept", {
        "label_id": label_id, "outcome": label["outcome"], "adoption": label.get("adoption"),
        "issue_types": issue_types,
    }, trace_id=trace_id)
    preference = label.get("preference_content", "").strip()
    if preference:
        await save_preference(
            title=label.get("preference_title", "人工确认的开发偏好"),
            content=preference,
            scope_type=label.get("preference_scope_type", "project"),
            scope_value=label.get("preference_scope_value", ""),
            source_trace_id=trace_id,
        )
    return {"id": label_id, "trace_id": trace_id}


async def save_preference(
    title: str,
    content: str,
    scope_type: str,
    scope_value: str,
    source_trace_id: Optional[str] = None,
) -> int:
    """持久化人类明确偏好；同一范围同标题会覆盖旧偏好。"""
    async with Database() as db:
        old_rows = await (await db._conn.execute(
            "SELECT id FROM long_term_memory WHERE category = 'preference' AND title = ? "
            "AND scope_type = ? AND scope_value = ? AND status = 'active'",
            (title, scope_type, scope_value),
        )).fetchall()
        now = _now()
        cursor = await db._conn.execute(
            "INSERT INTO long_term_memory (category, title, content, source_session_id, "
            "scope_type, scope_value, source_type, source_trace_id, confidence, occurrence_count, "
            "status, last_verified_at, created_at) VALUES ('preference', ?, ?, NULL, ?, ?, "
            "'human_feedback', ?, 1.0, 1, 'active', ?, ?)",
            (title, content, scope_type, scope_value, source_trace_id, now, now),
        )
        memory_id = cursor.lastrowid
        for old in old_rows:
            await db._conn.execute(
                "UPDATE long_term_memory SET status = 'superseded', supersedes_memory_id = ? WHERE id = ?",
                (memory_id, old["id"]),
            )
        await db._conn.commit()
    return memory_id


async def list_preferences(limit: int = 100) -> list[dict[str, Any]]:
    """列出可管理的人类偏好。"""
    async with Database() as db:
        rows = await (await db._conn.execute(
            "SELECT id, title, content, scope_type, scope_value, confidence, occurrence_count, "
            "status, source_trace_id, created_at, last_verified_at FROM long_term_memory "
            "WHERE category = 'preference' ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
        )).fetchall()
    return [dict(row) for row in rows]


async def get_active_preferences(project_path: str, limit: int = 8) -> list[dict[str, Any]]:
    """读取与当前项目路径匹配的有效人工偏好。"""
    async with Database() as db:
        rows = await (await db._conn.execute(
            "SELECT title, content, scope_type, scope_value, confidence FROM long_term_memory "
            "WHERE category = 'preference' AND status = 'active' AND ("
            "scope_type = 'global' OR (scope_type IN ('project', 'directory') AND ? LIKE scope_value || '%')"
            ") ORDER BY confidence DESC, last_verified_at DESC, id DESC LIMIT ?",
            (project_path, max(1, min(limit, 30))),
        )).fetchall()
    return [dict(row) for row in rows]


async def update_preference(memory_id: int, content: Optional[str], status: Optional[str]) -> bool:
    """修改偏好文本或停用状态。"""
    fields: list[str] = []
    params: list[Any] = []
    if content is not None:
        fields.append("content = ?")
        params.append(content)
    if status is not None:
        if status not in {"active", "disabled", "superseded"}:
            raise ValueError("无效的偏好状态")
        fields.append("status = ?")
        params.append(status)
    if not fields:
        return False
    params.append(memory_id)
    async with Database() as db:
        cursor = await db._conn.execute(
            f"UPDATE long_term_memory SET {', '.join(fields)} WHERE id = ? AND category = 'preference'",
            tuple(params),
        )
        await db._conn.commit()
    return cursor.rowcount > 0


async def create_evaluation_report(
    workflow_type: Optional[str] = None,
    project_path: Optional[str] = None,
) -> dict[str, Any]:
    """生成基于 Trace 与人工标注的确定性评测报告。"""
    traces = await list_traces(limit=200, workflow_type=workflow_type, project_path=project_path)
    trace_ids = [trace["trace_id"] for trace in traces]
    labels: dict[str, dict[str, Any]] = {}
    if trace_ids:
        placeholders = ",".join("?" for _ in trace_ids)
        async with Database() as db:
            rows = await (await db._conn.execute(
                f"SELECT * FROM trace_labels WHERE id IN (SELECT MAX(id) FROM trace_labels "
                f"WHERE trace_id IN ({placeholders}) GROUP BY trace_id)", tuple(trace_ids)
            )).fetchall()
        labels = {row["trace_id"]: dict(row) for row in rows}

    completed = [item for item in traces if item["status"] == "done"]
    durations = [item["duration_ms"] for item in completed if item["duration_ms"] is not None]
    labeled = list(labels.values())
    accepted = [item for item in labeled if item["outcome"] in {"approved", "conditional"}]
    metrics = {
        "total_traces": len(traces),
        "completed_traces": len(completed),
        "labeled_traces": len(labeled),
        "completion_rate": _ratio(len(completed), len(traces)),
        "human_acceptance_rate": _ratio(len(accepted), len(labeled)),
        "average_duration_ms": round(sum(durations) / len(durations)) if durations else None,
        "total_input_tokens": sum(item["input_tokens"] for item in traces),
        "total_output_tokens": sum(item["output_tokens"] for item in traces),
        "average_fix_attempts": await _average_event_value(trace_ids, "fix", "attempt"),
        "review_pass_rate": await _review_pass_rate(trace_ids),
    }
    async with Database() as db:
        cursor = await db._conn.execute(
            "INSERT INTO evaluation_reports (project_path, workflow_type, metrics, created_at) "
            "VALUES (?, ?, ?, ?)", (project_path, workflow_type, _json(metrics), _now())
        )
        await db._conn.commit()
    return {"report_id": cursor.lastrowid, "metrics": metrics, "traces": traces}


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    return round(numerator / denominator, 4) if denominator else None


async def _average_event_value(trace_ids: list[str], stage: str, key: str) -> Optional[float]:
    del stage, key
    if not trace_ids:
        return None
    placeholders = ",".join("?" for _ in trace_ids)
    async with Database() as db:
        rows = await (await db._conn.execute(
            f"SELECT payload FROM trace_events WHERE trace_id IN ({placeholders}) AND event_type = 'stage_output' "
            "AND stage = 'fix'", tuple(trace_ids)
        )).fetchall()
    return round(len(rows) / len(trace_ids), 2) if rows else 0.0


async def _review_pass_rate(trace_ids: list[str]) -> Optional[float]:
    if not trace_ids:
        return None
    placeholders = ",".join("?" for _ in trace_ids)
    async with Database() as db:
        rows = await (await db._conn.execute(
            f"SELECT trace_id, payload FROM trace_events WHERE trace_id IN ({placeholders}) "
            "AND event_type = 'review_aggregated' ORDER BY id", tuple(trace_ids)
        )).fetchall()
    if not rows:
        return None
    latest_by_trace: dict[str, bool] = {}
    for row in rows:
        latest_by_trace[row["trace_id"]] = bool(json.loads(row["payload"]).get("passed"))
    return _ratio(sum(latest_by_trace.values()), len(latest_by_trace))
