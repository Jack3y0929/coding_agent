"""跨工作流共享的正式产出模型；不包含任何 Agent 对话历史。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ClarificationDocument(BaseModel):
    user_description: str = ""
    project_path: str = ""
    confirmed_slots: dict[str, Any] = Field(default_factory=dict)


class IntentDecision(BaseModel):
    workflow_type: Literal["dev", "debug", "unknown"] = "unknown"
    confidence: float = 0.0
    missing_fields: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "medium"
    route: Literal["clarify", "develop", "diagnose", "terminate", "human_accept"] = "clarify"


class RequirementSummary(BaseModel):
    content: str = ""


class DiagnosisReport(BaseModel):
    content: str = ""
    sufficient: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class CodeChange(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)
    implementation_note: str = ""


class ReviewReport(BaseModel):
    content: str = ""
    passed: bool = False
    findings: list[dict[str, Any]] = Field(default_factory=list)
    round: int = 0


class QualityScore(BaseModel):
    workflow_success: float | None = None
    review_pass_rate: float | None = None
    test_pass_rate: float | None = None
    repair_count: int = 0
    human_acceptance: float | None = None
    llm_cost: float = 0.0
    latency_ms: int | None = None
    error_count: int = 0
    rag_hit_count: int = 0


class WorkflowStatus(BaseModel):
    session_id: str
    status: Literal["running", "paused", "done", "failed"]
    stage: str = ""
    workflow_type: Literal["dev", "debug", "unknown"] = "unknown"
