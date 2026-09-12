"""工具失败分类与执行报告记录。"""

from __future__ import annotations

from typing import Any

from backend.config import MAX_TOOL_RETRIES
from backend.tools.models import ToolResult


def record_tool_failure(
    report: dict[str, Any] | None,
    result: ToolResult,
    arguments: dict[str, Any],
    retry_count: int,
) -> bool:
    """记录失败并返回本次是否允许纠错重试。"""
    if report is None:
        return result.retryable and retry_count <= MAX_TOOL_RETRIES
    failure = {
        "tool": result.tool_name,
        "code": result.code,
        "message": result.message[:1000],
        "arguments": arguments,
        "retryable": result.retryable,
        "retry_count": retry_count,
    }
    report.setdefault("tool_failures", []).append(failure)
    report["last_tool_error"] = failure
    report["tool_retry_count"] = retry_count
    allowed = result.retryable and retry_count <= MAX_TOOL_RETRIES
    if not allowed:
        report["tool_failure_exhausted"] = True
    return allowed
