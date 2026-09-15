"""FeatureBench 任务、容器和 trial 数据模型。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class FeatureBenchTask(BaseModel):
    instance_id: str
    problem_statement: str
    image_name: str
    level: int | None = None
    fail_to_pass: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeatureBenchEvalResult(BaseModel):
    passed: bool
    resolved: bool
    report_path: Path
    test_output_path: Path | None = None
    raw_report: dict[str, Any] = Field(default_factory=dict)


class FeatureBenchTrialResult(BaseModel):
    run_id: str
    instance_id: str
    attempt: int
    status: Literal["completed", "failed"]
    image_name: str = ""
    level: int | None = None
    patch_path: Path | None = None
    output_path: Path | None = None
    report_path: Path | None = None
    infer_log_path: Path | None = None
    test_output_path: Path | None = None
    passed: bool | None = None
    resolved: bool | None = None
    error_message: str | None = None
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
