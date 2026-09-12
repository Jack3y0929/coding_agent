"""LangGraph 工作流上下文：暂停/恢复 + 进度推送"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any


# 暂停等待的 session 事件
_pause_events: dict[str, asyncio.Event] = {}
# 人类决策存储
_human_decisions: dict[str, dict[str, Any]] = {}
# WebSocket 连接池
_ws_connections: dict[str, list[Any]] = {}
# 最近一次进度快照，供页面切换或重连后恢复展示。
_progress_snapshots: dict[str, dict[str, Any]] = {}


def register_ws(session_id: str, ws: Any) -> None:
    """注册 WebSocket 连接"""
    if session_id not in _ws_connections:
        _ws_connections[session_id] = []
    _ws_connections[session_id].append(ws)


def unregister_ws(session_id: str, ws: Any) -> None:
    """注销 WebSocket 连接"""
    if session_id in _ws_connections:
        _ws_connections[session_id].remove(ws)


async def push_progress(session_id: str, data: dict[str, Any]) -> None:
    """向所有连接的 WebSocket 推送进度更新"""
    if data.get("event") in {"progress", "paused", "complete", "error"}:
        previous = _progress_snapshots.get(session_id, {})
        _progress_snapshots[session_id] = {
            **previous,
            "event": data.get("event"),
            "stage": data.get("stage") or previous.get("stage"),
            "stage_name": data.get("stage_name") or previous.get("stage_name"),
            "message": data.get("message") or previous.get("message", ""),
            "intent": data.get("intent") or previous.get("intent"),
            "validation": data.get("validation") or previous.get("validation"),
            "token_status": data.get("token_status") or previous.get("token_status"),
        }
    if data.get("event") == "progress" and data.get("stage"):
        from backend.trace import record_event
        await record_event("progress", data["stage"], {
            "message": data.get("stage_name") or data.get("message", ""),
            "intent": data.get("intent"),
            "validation": data.get("validation"),
            "token_status": data.get("token_status"),
        })
    if session_id in _ws_connections:
        msg = json.dumps(data, ensure_ascii=False)
        for ws in _ws_connections[session_id]:
            try:
                await ws.send_text(msg)
            except Exception:
                pass


async def push_trace_event(session_id: str, trace_event: dict[str, Any]) -> None:
    """通过任务 WebSocket 推送已落库的单条 Trace 事件。"""
    if session_id not in _ws_connections:
        return
    message = json.dumps({"event": "trace", "trace": trace_event}, ensure_ascii=False)
    for ws in list(_ws_connections[session_id]):
        try:
            await ws.send_text(message)
        except Exception:
            pass


def get_progress_snapshot(session_id: str) -> dict[str, Any] | None:
    """读取当前进度快照；不会暴露 WebSocket 连接或工作流上下文。"""
    snapshot = _progress_snapshots.get(session_id)
    return dict(snapshot) if snapshot else None


async def wait_for_human(session_id: str, stage: str = "human_accept") -> dict[str, Any]:
    """等待人工操作，并通过 SQLite 控制队列支持跨进程恢复。"""
    event = asyncio.Event()
    _pause_events[session_id] = event
    from backend.db.models import update_session_status
    await update_session_status(session_id, "paused")

    await push_progress(session_id, {
        "event": "paused",
        "stage": stage,
        "message": (
            "等待人工操作" if stage == "human_accept" else
            "等待补充研发任务槽位" if stage == "intent_clarify" else
            "等待复现BUG后提供日志"
        ),
    })

    from backend.trace import record_event
    await record_event("human_waiting", stage, {})

    # Worker 与 FastAPI 不共享内存，不能只等待本进程 asyncio.Event。
    # Event 用于同进程快速唤醒，数据库轮询用于跨进程恢复。
    while True:
        if event.is_set():
            # 清理 queue_resume_session 写入的共享信号，避免下一次暂停误消费旧决策。
            await _consume_control(session_id)
            break
        control = await _consume_control(session_id)
        if control is not None:
            _human_decisions[session_id] = control
            break
        try:
            await asyncio.wait_for(event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
    _pause_events.pop(session_id, None)
    await update_session_status(session_id, "running")
    return _human_decisions.pop(session_id, {})


def resume_session(session_id: str, decision: str | None = None,
                   new_logs: str | None = None,
                   feedback: dict[str, Any] | None = None,
                   intent_update: dict[str, Any] | None = None) -> bool:
    """恢复暂停的 session"""
    if session_id in _pause_events:
        _human_decisions[session_id] = {
            "decision": decision,
            "new_logs": new_logs,
            "feedback": feedback or {},
            "intent_update": intent_update or {},
        }
        # 同步函数不能阻塞等待数据库；创建任务即可让状态尽快恢复为 running。
        asyncio.create_task(_mark_session_running(session_id))
        _pause_events[session_id].set()
        return True
    return False


async def queue_resume_session(session_id: str, decision: str | None = None,
                               new_logs: str | None = None,
                               feedback: dict[str, Any] | None = None,
                               intent_update: dict[str, Any] | None = None) -> bool:
    """将人工决策写入共享队列，并唤醒同进程等待者（若存在）。"""
    from backend.db.models import Database

    async with Database() as db:
        row = await (await db._conn.execute(
            "SELECT status FROM sessions WHERE id = ?", (session_id,)
        )).fetchone()
        if not row or row["status"] != "paused":
            return False
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db._conn.execute(
            "INSERT INTO workflow_controls "
            "(session_id, decision, new_logs, feedback, intent_update, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET decision=excluded.decision, "
            "new_logs=excluded.new_logs, feedback=excluded.feedback, "
            "intent_update=excluded.intent_update, created_at=excluded.created_at",
            (session_id, decision, new_logs, json.dumps(feedback or {}, ensure_ascii=False),
             json.dumps(intent_update or {}, ensure_ascii=False), created_at),
        )
        await db._conn.commit()

    # 同进程时立即唤醒；跨进程 Worker 会在轮询中消费队列。
    if session_id in _pause_events:
        _human_decisions[session_id] = {
            "decision": decision,
            "new_logs": new_logs,
            "feedback": feedback or {},
            "intent_update": intent_update or {},
        }
        _pause_events[session_id].set()
    return True


async def _consume_control(session_id: str) -> dict[str, Any] | None:
    """原子读取并删除一条人工决策，避免重复恢复。"""
    from backend.db.models import Database

    async with Database() as db:
        row = await (await db._conn.execute(
            "SELECT decision, new_logs, feedback, intent_update FROM workflow_controls "
            "WHERE session_id = ?", (session_id,)
        )).fetchone()
        if not row:
            return None
        await db._conn.execute("DELETE FROM workflow_controls WHERE session_id = ?", (session_id,))
        await db._conn.commit()
    return {
        "decision": row["decision"],
        "new_logs": row["new_logs"],
        "feedback": json.loads(row["feedback"] or "{}"),
        "intent_update": json.loads(row["intent_update"] or "{}"),
    }


async def _mark_session_running(session_id: str) -> None:
    from backend.db.models import update_session_status
    await update_session_status(session_id, "running")
