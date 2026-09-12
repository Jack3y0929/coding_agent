"""将工作流正式产物提炼为可复用的长期记忆。"""

from __future__ import annotations

from typing import Any


def _clip(value: str | None, limit: int = 1200) -> str:
    text = (value or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n...（长期记忆已截断）"


def _files(state: dict[str, Any]) -> str:
    changes = state.get("code_changes") or {}
    return ", ".join(changes.keys()) if changes else "无实际代码变更"


def build_dev_memory_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """只保留 DEV 的结果、决策和经验，不复制需求/计划全文。"""
    description = _clip(state.get("description"), 500)
    summary = _clip(state.get("summary"), 1000)
    implementation = _clip(state.get("implementation_note"), 1200)
    lesson = (
        f"任务目标：{description}\n"
        f"实际变更文件：{_files(state)}\n"
        f"实现要点：{implementation or '未提供'}\n"
        f"工作流结果：{summary or '未提供'}"
    )
    source_artifact_id = state.get("source_artifact_id")
    return [
        {
            "category": "task",
            "memory_type": "task_outcome",
            "title": f"DEV任务结果 - {description[:50]}",
            "content": summary or lesson,
            "source_artifact_id": source_artifact_id,
        },
        {
            "category": "task",
            "memory_type": "lesson",
            "title": f"DEV实现经验 - {description[:50]}",
            "content": lesson,
            "source_artifact_id": source_artifact_id,
        },
    ]


def build_debug_memory_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """只保留 DEBUG 的根因、修复和经验，不复制诊断报告全文。"""
    description = _clip(state.get("description"), 500)
    diagnosis = _clip(state.get("diagnosis_report"), 1600)
    implementation = _clip(state.get("implementation_note"), 1200)
    summary = _clip(state.get("summary"), 1000)
    lesson = (
        f"BUG描述：{description}\n"
        f"根因与证据：{diagnosis or '未提供'}\n"
        f"修复要点：{implementation or '未提供'}\n"
        f"实际变更文件：{_files(state)}\n"
        f"修复结果：{summary or '未提供'}"
    )
    source_artifact_id = state.get("source_artifact_id")
    return [
        {
            "category": "error",
            "memory_type": "error_pattern",
            "title": f"DEBUG修复经验 - {description[:50]}",
            "content": lesson,
            "source_artifact_id": source_artifact_id,
        },
    ]
