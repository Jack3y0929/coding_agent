"""研发槽位完整性、冲突和写入范围规则。"""

from __future__ import annotations

from pathlib import Path

from backend.intent.models import CodingIntent, SlotValidationResult


def validate_slots(
    intent: CodingIntent,
    manual_workflow_type: str | None = None,
) -> SlotValidationResult:
    """根据意图类型检查最小可执行槽位并给出稳定路由。"""
    missing: list[str] = []
    warnings: list[str] = []
    questions: list[str] = []

    if intent.task_type == "clarify" or intent.confidence < 0.55:
        missing.append("任务类型")
        questions.append("这次任务是新增功能、修复 BUG，还是重构优化？")

    if not intent.task_description.strip():
        missing.append("任务描述")
        questions.append("请补充需要实现的功能，或需要修复的具体问题。")

    if not intent.target_modules and not intent.change_scope:
        missing.append("目标模块或修改范围")
        questions.append("请确认涉及的模块、目录或允许修改的文件范围。")

    if intent.task_type in {"dev", "refactor"} and not intent.acceptance_criteria:
        missing.append("验收标准")
        questions.append("完成后应满足哪些可验证的验收标准？")

    if intent.task_type == "debug":
        if not intent.observed_behavior.strip():
            missing.append("实际异常表现")
            questions.append("请说明当前实际出现的错误、异常现象或日志。")
        if not intent.reproduction_steps:
            missing.append("复现步骤或日志")
            questions.append("请提供稳定复现步骤，或补充关键错误日志。")

    if intent.risk_level == "high":
        if not intent.change_scope:
            missing.append("高风险任务修改范围")
            questions.append("该高风险任务允许修改哪些目录或文件？")
        if not intent.acceptance_criteria:
            missing.append("高风险任务验收标准")
            questions.append("请明确高风险改动的验收和回归标准。")

    conflict = bool(
        manual_workflow_type
        and manual_workflow_type in {"dev", "debug"}
        and manual_workflow_type != intent.workflow_type
        and intent.confidence >= 0.70
    )
    if conflict:
        questions.append(
            f"你选择了 {manual_workflow_type.upper()}，但系统识别为 "
            f"{intent.task_type.upper()}。请确认按哪种流程继续。"
        )

    if intent.validation_commands:
        warnings.append("验证命令将继续受 Shell 白名单校验，未通过的命令不会执行。")

    return SlotValidationResult(
        decision="clarify" if missing or conflict else "proceed",
        missing_slots=missing,
        warnings=warnings,
        clarification_questions=questions,
        manual_selection_conflict=conflict,
        # 手动选择是显式用户约束；发生冲突时先进入澄清确认，再按该选择编排图。
        selected_workflow_type=(manual_workflow_type if manual_workflow_type in {"dev", "debug"} else intent.workflow_type),
    )


def validate_write_scope(project_path: str, target_path: str, intent: CodingIntent) -> str | None:
    """校验写入目标是否违反受保护路径或明确的目录范围。"""
    if not project_path:
        return None
    project_root = Path(project_path).resolve()
    target = Path(target_path).resolve()
    try:
        relative_target = target.relative_to(project_root).as_posix()
    except ValueError:
        return "写入目标不在当前项目根目录内。"

    protected = [_normalize_relative_path(item) for item in intent.protected_paths]
    if any(_matches_path(relative_target, item) for item in protected if item):
        return f"写入目标位于受保护范围: {relative_target}"

    scoped = [_normalize_relative_path(item) for item in intent.change_scope]
    scoped = [item for item in scoped if item]
    if scoped and not any(_matches_path(relative_target, item) for item in scoped):
        return f"写入目标不在已确认的修改范围内: {relative_target}"
    return None


def _normalize_relative_path(value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value or value.startswith("/") or ":" in value:
        return ""
    return value.rstrip("/")


def _matches_path(relative_target: str, scope: str) -> bool:
    return relative_target == scope or relative_target.startswith(f"{scope}/")
