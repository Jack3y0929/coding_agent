"""SWE-bench 数据集和单次试验的数据契约。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class SWEbenchInstance(BaseModel):
    """仅包含允许暴露给评测编排器和 Agent 的任务字段。"""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    version: str | None = None
    environment_setup_commit: str | None = None
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)


class HarnessResult(BaseModel):
    """官方 Harness 的归一化判定。"""

    resolved: bool
    fail_to_pass: bool | None = None
    pass_to_pass: bool | None = None
    report_path: Path | None = None
    raw_report: dict[str, Any] = Field(default_factory=dict)


class SWEbenchTrialResult(BaseModel):
    """一次 trial 的可持久化结果。"""

    run_id: str
    instance_id: str
    trial_index: int
    status: Literal["completed", "failed"]
    workspace_path: Path
    model_patch_path: Path | None = None
    harness_report_path: Path | None = None
    resolved: bool | None = None
    fail_to_pass: bool | None = None
    pass_to_pass: bool | None = None
    error_message: str | None = None
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
