"""Fallback 决策数据模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


FallbackAction = Literal[
    "continue", "clarify", "retry", "fix", "add_logging",
    "human_accept", "human_intervene", "stop",
]


class FallbackDecision(BaseModel):
    """规则层产生的下一步动作及其可审计依据。"""

    action: FallbackAction
    reason_code: str
    reason: str
    next_stage: str | None = None
    retryable: bool = False
    input_snapshot: dict[str, object] = {}
