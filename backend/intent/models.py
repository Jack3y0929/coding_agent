"""Coding Agent 的研发意图与槽位模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


TaskType = Literal["dev", "debug", "refactor", "clarify"]
RiskLevel = Literal["low", "medium", "high"]
SlotDecision = Literal["proceed", "clarify"]


class CodingIntent(BaseModel):
    """由意图 Agent 提取的研发任务槽位，不包含角色推理过程。"""

    task_type: TaskType = "clarify"
    confidence: float = 0.0
    task_description: str = ""
    target_modules: list[str] = Field(default_factory=list)
    change_scope: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    tech_constraints: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    observed_behavior: str = ""
    expected_behavior: str = ""
    risk_level: RiskLevel = "medium"
    missing_slots: list[str] = Field(default_factory=list)
    source: Literal["llm", "fallback", "manual_override"] = "llm"

    @property
    def workflow_type(self) -> Literal["dev", "debug"]:
        """将重构任务复用 DEV 工作流，保留其独立语义槽位。"""
        return "debug" if self.task_type == "debug" else "dev"


class SlotValidationResult(BaseModel):
    """规则层输出的确定性路由结论。"""

    decision: SlotDecision
    missing_slots: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    manual_selection_conflict: bool = False
    selected_workflow_type: Literal["dev", "debug"]
