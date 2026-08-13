"""LangGraph 工作流上下文：暂停/恢复 + 进度推送"""

from __future__ import annotations

import asyncio
import json
from typing import Any


# 暂停等待的 session 事件
_pause_events: dict[str, asyncio.Event] = {}
# 人类决策存储
_human_decisions: dict[str, dict[str, Any]] = {}
# WebSocket 连接池
_ws_connections: dict[str, list[Any]] = {}


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
    if data.get("event") == "progress" and data.get("stage"):
        from backend.trace import record_event
        await record_event("progress", data["stage"], {
            "message": data.get("stage_name") or data.get("message", ""),
        })
    if session_id in _ws_connections:
        msg = json.dumps(data, ensure_ascii=False)
        for ws in _ws_connections[session_id]:
            try:
                await ws.send_text(msg)
            except Exception:
                pass


async def wait_for_human(session_id: str, stage: str = "human_accept") -> dict[str, Any]:
    """等待人工操作"""
    event = asyncio.Event()
    _pause_events[session_id] = event

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

    await event.wait()
    _pause_events.pop(session_id, None)
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
        _pause_events[session_id].set()
        return True
    return False
