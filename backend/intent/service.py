"""独立上下文中的研发意图提取服务。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from backend.budget import TokenBudget
from backend.graph.nodes import build_node_messages, call_deepseek
from backend.intent.models import CodingIntent

logger = logging.getLogger("intent.service")


_INTENT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "agents" / "intent_prompt.md"


def _load_intent_prompt() -> str:
    """加载意图分析 Skill，保持提示词与运行时实现单一来源。"""
    try:
        return _INTENT_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("意图分析 Skill 加载失败，使用最小安全提示词: %s", exc)
        return (
            "你是 Coding Agent 的研发意图分析器。只根据输入提取事实，"
            "禁止编写代码、调用工具或输出推理过程。只输出完整 JSON。"
        )


INTENT_SYSTEM_PROMPT = _load_intent_prompt()


async def extract_coding_intent(
    description: str,
    project_path: str,
    manual_workflow_type: str | None,
    budget: TokenBudget,
    provided_slots: dict[str, object] | None = None,
) -> CodingIntent:
    """使用独立 Agent 上下文提取研发意图；服务不可用时降级到规则化初判。"""
    user_content = (
        f"项目路径: {project_path}\n"
        f"用户手动选择的流程: {manual_workflow_type or '未选择，自动识别'}\n"
        f"用户任务: {description}\n"
        "请提取研发意图槽位。"
    )
    try:
        result = await call_deepseek(build_node_messages(INTENT_SYSTEM_PROMPT, user_content), budget=budget)
        payload = _parse_json_object(result["content"])
        intent = CodingIntent.model_validate(payload)
        return _merge_provided_slots(intent.model_copy(update={"source": "llm"}), provided_slots)
    except Exception as exc:
        logger.warning("研发意图 LLM 提取失败，使用降级规则: %s", exc)
        return _merge_provided_slots(_fallback_intent(description, manual_workflow_type), provided_slots)


def apply_manual_override(intent: CodingIntent, workflow_type: str) -> CodingIntent:
    """记录人工确认的工作流选择，保留其他已提取槽位。"""
    task_type = "debug" if workflow_type == "debug" else "dev"
    return intent.model_copy(update={
        "task_type": task_type,
        "confidence": 1.0,
        "source": "manual_override",
    })


def merge_intent_updates(intent: CodingIntent, updates: dict[str, object]) -> CodingIntent:
    """合并澄清阶段提交的槽位；仅允许更新定义过的公开字段。"""
    merged = _merge_provided_slots(intent, updates)
    selected = updates.get("workflow_type")
    if isinstance(selected, str) and selected in {"dev", "debug"}:
        return apply_manual_override(merged, selected)
    return merged


def _parse_json_object(text: str) -> dict:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    else:
        object_match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if object_match:
            stripped = object_match.group(0)
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("意图输出不是 JSON 对象")
    return parsed


def _fallback_intent(description: str, manual_workflow_type: str | None) -> CodingIntent:
    """当模型调用不可用时保留安全的最小意图，交由槽位规则决定是否澄清。"""
    lower = description.lower()
    if manual_workflow_type in {"dev", "debug"}:
        task_type = manual_workflow_type
    elif any(keyword in lower for keyword in ("bug", "异常", "报错", "失败", "崩溃", "修复")):
        task_type = "debug"
    elif any(keyword in lower for keyword in ("重构", "优化", "整理", "迁移")):
        task_type = "refactor"
    elif description.strip():
        task_type = "dev"
    else:
        task_type = "clarify"
    return CodingIntent(
        task_type=task_type,
        confidence=0.45,
        task_description=description.strip(),
        observed_behavior=description.strip() if task_type == "debug" else "",
        source="fallback",
    )


def _merge_provided_slots(intent: CodingIntent, provided_slots: dict[str, object] | None) -> CodingIntent:
    """用户显式填写的槽位优先于模型提取，避免模型覆盖人工约束。"""
    if not provided_slots:
        return intent
    updates: dict[str, object] = {}
    for field in (
        "target_modules", "change_scope", "protected_paths", "acceptance_criteria",
        "tech_constraints", "validation_commands", "reproduction_steps",
    ):
        value = provided_slots.get(field)
        if isinstance(value, list) and value:
            updates[field] = value
    for field in ("observed_behavior", "expected_behavior"):
        value = provided_slots.get(field)
        if isinstance(value, str) and value.strip():
            updates[field] = value.strip()
    return intent.model_copy(update=updates)
