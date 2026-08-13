"""在工具调用期间传递当前研发槽位，不跨角色传递对话历史。"""

from __future__ import annotations

import contextvars
from typing import Optional

from backend.intent.models import CodingIntent


_active_intent: contextvars.ContextVar[Optional[CodingIntent]] = contextvars.ContextVar(
    "active_coding_intent", default=None
)


def bind_intent(intent: CodingIntent) -> contextvars.Token[Optional[CodingIntent]]:
    """绑定当前工作流的结构化意图槽位。"""
    return _active_intent.set(intent)


def current_intent() -> Optional[CodingIntent]:
    """返回当前工具调用可读取的研发槽位。"""
    return _active_intent.get()


def unbind_intent(token: contextvars.Token[Optional[CodingIntent]]) -> None:
    """恢复绑定前的槽位上下文。"""
    _active_intent.reset(token)
