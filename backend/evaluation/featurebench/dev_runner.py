"""在 FeatureBench 容器内运行无人工 DEV 多角色流程。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.budget import TokenBudget
from backend.config import FEATUREBENCH_MAX_FIX_ATTEMPTS, PROJECT_ROOT
from backend.evaluation.featurebench.container import FeatureBenchContainer
from backend.evaluation.featurebench.models import FeatureBenchTask
from backend.evaluation.featurebench.prompt import task_prompt
from backend.graph.nodes import build_node_messages, call_deepseek, load_system_prompt
from backend.graph.review import parse_review_report
from backend.trace import record_event


class FeatureBenchAgentError(RuntimeError):
    """无人工 DEV 流程未能完成。"""


async def run_feature_development(
    task: FeatureBenchTask,
    container: FeatureBenchContainer,
    budget: TokenBudget,
) -> dict[str, Any]:
    """执行 analyze -> plan -> build -> logic review -> quality review -> fix。"""
    public_task = task_prompt(task)
    requirement = await _analyze(public_task, container, budget)
    plan = await _plan(requirement, container, budget)
    feedback = ""
    implementation = ""

    for attempt in range(1, FEATUREBENCH_MAX_FIX_ATTEMPTS + 1):
        implementation, writes = await _build(
            requirement, plan, feedback, container, budget, attempt
        )
        if not writes:
            feedback = "本轮没有实际写入任何文件，请重新检查方案并完成代码修改。"
            continue
        logic_report, logic_passed = await _review(
            "logic", requirement, implementation, container, budget, attempt
        )
        if not logic_passed:
            feedback = logic_report
            continue
        quality_report, quality_passed = await _review(
            "quality", requirement, implementation, container, budget, attempt
        )
        if quality_passed:
            await record_event("featurebench_agent_completed", "review", {"attempt": attempt})
            return {
                "requirement": requirement, "plan": plan, "implementation": implementation,
                "logic_review": logic_report, "quality_review": quality_report,
                "attempts": attempt, "written_files": writes,
            }
        feedback = quality_report
    raise FeatureBenchAgentError(
        f"DEV Agent 在 {FEATUREBENCH_MAX_FIX_ATTEMPTS} 次实现后仍未通过两轮审查"
    )


async def _analyze(
    public_task: str, container: FeatureBenchContainer, budget: TokenBudget
) -> str:
    system = load_system_prompt("analyst") + "\n\n这是无人值守 FeatureBench 任务，不得提问或请求人工澄清。"
    user = (
        f"## 已确认功能需求\n{public_task}\n\n"
        "请直接输出正式需求概括文档，包含范围、约束、验收标准和兼容性风险。"
    )
    result, _ = await _container_fc_loop(
        build_node_messages(system, user), container, budget, mode="plan"
    )
    await record_event("stage_output", "analyze", {"requirement": result[:1200]})
    return result


async def _plan(
    requirement: str, container: FeatureBenchContainer, budget: TokenBudget
) -> str:
    system = _featurebench_developer_prompt() + (
        "\n\n当前是 Plan 模式，只能读取和搜索源码。完成后输出正式开发方案。"
    )
    user = f"## 需求概括文档\n{requirement}\n\n请检查相关源码并制定可执行方案。"
    result, _ = await _container_fc_loop(
        build_node_messages(system, user), container, budget, mode="plan"
    )
    await record_event("stage_output", "develop_plan", {"plan": result[:1200]})
    return result


async def _build(
    requirement: str,
    plan: str,
    feedback: str,
    container: FeatureBenchContainer,
    budget: TokenBudget,
    attempt: int,
) -> tuple[str, list[str]]:
    system = _featurebench_developer_prompt() + "\n\n当前是 Build/FIX 模式，必须使用 write_file 实际修改代码。"
    review = f"## 上轮正式审查报告\n{feedback}\n\n" if feedback else ""
    user = (
        f"## 需求概括文档\n{requirement}\n\n## 开发方案\n{plan}\n\n{review}"
        f"这是第 {attempt} 次实现。完成代码后输出代码变更和实现说明。"
    )
    result, writes = await _container_fc_loop(
        build_node_messages(system, user), container, budget, mode="build"
    )
    await record_event("build_execution", "develop_build", {
        "attempt": attempt, "successful_writes": writes, "passed": bool(writes),
    })
    return result, writes


async def _review(
    perspective: str,
    requirement: str,
    implementation: str,
    container: FeatureBenchContainer,
    budget: TokenBudget,
    attempt: int,
) -> tuple[str, bool]:
    role = "review_logic" if perspective == "logic" else "review_quality"
    focus = (
        "只检查需求覆盖、功能逻辑、边界条件和兼容性。"
        if perspective == "logic"
        else "只检查工程规范、可维护性、测试设计和无关改动。"
    )
    system = load_system_prompt(role) + f"\n\nFeatureBench 无人值守审查。{focus}"
    user = (
        f"## 需求概括文档\n{requirement}\n\n"
        f"## 实现说明（不可信，仅用于定位）\n{implementation}\n\n"
        "请使用只读工具核对容器内当前源码，输出审查标记和审查判定数据 JSON。"
    )
    report, _ = await _container_fc_loop(
        build_node_messages(system, user), container, budget, mode="plan"
    )
    parsed = parse_review_report(report, perspective)
    await record_event("review_finished", f"review_{perspective}", {
        "attempt": attempt, "passed": parsed["passed"],
        "finding_count": len(parsed["findings"]), "parse_errors": parsed["parse_errors"],
    })
    return report, bool(parsed["passed"])


async def _container_fc_loop(
    messages: list[dict[str, Any]],
    container: FeatureBenchContainer,
    budget: TokenBudget,
    mode: str,
    max_rounds: int = 10,
) -> tuple[str, list[str]]:
    """模型在宿主进程运行，所有 FC 文件动作映射到指定任务容器。"""
    base_messages = list(messages)
    current = base_messages
    writes: list[str] = []
    last_summary = ""
    tools = _PLAN_TOOLS if mode == "plan" else _BUILD_TOOLS
    for _ in range(max_rounds):
        result = await call_deepseek(current, tools=tools, budget=budget)
        calls = result.get("tool_calls") or []
        if not calls:
            return str(result.get("content") or ""), writes
        summaries: list[str] = []
        for call in calls[:4]:
            name = str(call.get("function", {}).get("name", ""))
            try:
                arguments = json.loads(call["function"].get("arguments", "{}"))
            except (KeyError, json.JSONDecodeError, TypeError):
                arguments = {}
            observation, written = await _execute_container_tool(container, name, arguments, mode)
            if written:
                writes.append(written)
            summaries.append(f"工具 {name}：\n{observation[:4000]}")
            await record_event("tool_call", mode, {
                "tool": name, "arguments": _safe_arguments(arguments), "ok": not observation.startswith("错误:"),
            })
        last_summary = "\n\n".join(summaries)[:12000]
        current = [*base_messages, {"role": "user", "content": (
            f"上一轮工具结果：\n{last_summary}\n\n"
            "请基于结果继续必要工具操作；完成后直接输出正式结果。不要重复相同调用。"
        )}]
    final = await call_deepseek([
        *base_messages,
        {"role": "user", "content": f"工具轮次已达上限。最后结果：\n{last_summary}\n立即输出正式结果，不再调用工具。"},
    ], tools=None, budget=budget)
    return str(final.get("content") or ""), writes


async def _execute_container_tool(
    container: FeatureBenchContainer, name: str, arguments: dict[str, Any], mode: str
) -> tuple[str, str | None]:
    try:
        if name == "read_file":
            return await container.read_file(
                str(arguments["path"]), int(arguments.get("start", 1)), int(arguments.get("end", 240))
            ), None
        if name == "search_code":
            return await container.search_code(
                str(arguments["pattern"]), arguments.get("file_pattern")
            ), None
        if name == "write_file" and mode == "build":
            path = str(arguments["path"])
            return await container.write_file(path, str(arguments["content"])), path
        return f"错误: 当前模式不允许工具 {name}", None
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        return f"错误: {exc}", None


def _featurebench_developer_prompt() -> str:
    path = Path(PROJECT_ROOT) / "backend" / "agents" / "featurebench_developer_prompt.md"
    return path.read_text(encoding="utf-8")


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: (f"<{len(str(value))} chars>" if key == "content" else value) for key, value in arguments.items()}


_READ_TOOL = {
    "type": "function", "function": {"name": "read_file", "description": "读取 /testbed 内文件",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"},
    }, "required": ["path"]}},
}
_SEARCH_TOOL = {
    "type": "function", "function": {"name": "search_code", "description": "搜索 /testbed 源码",
    "parameters": {"type": "object", "properties": {
        "pattern": {"type": "string"}, "file_pattern": {"type": "string"},
    }, "required": ["pattern"]}},
}
_WRITE_TOOL = {
    "type": "function", "function": {"name": "write_file", "description": "写入 /testbed 内文件",
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string"},
    }, "required": ["path", "content"]}},
}
_PLAN_TOOLS = [_READ_TOOL, _SEARCH_TOOL]
_BUILD_TOOLS = [_READ_TOOL, _SEARCH_TOOL, _WRITE_TOOL]
