"""SWE-bench 专用无人工 Agent 修复流程。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.budget import TokenBudget
from backend.config import SWEBENCH_MAX_FIX_ATTEMPTS
from backend.evaluation.swebench.models import SWEbenchInstance
from backend.evaluation.swebench.prompt import SYSTEM_RULES, issue_prompt
from backend.graph.nodes import (
    BUILD_MODE_TOOLS,
    PLAN_MODE_TOOLS,
    build_node_messages,
    load_system_prompt,
    run_fc_loop,
)
from backend.graph.review import parse_review_report
from backend.trace import record_event


class AgentRepairError(RuntimeError):
    """Agent 未能在允许轮次内产出有效修复。"""


async def run_headless_repair(
    instance: SWEbenchInstance,
    workspace: Path,
    budget: TokenBudget,
) -> dict[str, Any]:
    """执行诊断、修复及两轮独立审查，不进入任何人工等待节点。"""
    task = issue_prompt(instance)
    diagnosis = await _diagnose(task, workspace, budget)
    review_feedback = ""
    implementation = ""
    execution: dict[str, Any] = {}

    for attempt in range(1, SWEBENCH_MAX_FIX_ATTEMPTS + 1):
        implementation, execution = await _fix(
            task, diagnosis, review_feedback, workspace, budget, attempt
        )
        if not execution.get("successful_writes"):
            review_feedback = "本轮未产生任何成功的文件写入，请重新检查根因并实际修改源码。"
            await record_event("swebench_fix_empty", "fix", {"attempt": attempt})
            continue

        logic_report, logic_passed = await _review(
            "logic", task, diagnosis, implementation, workspace, budget, attempt
        )
        if not logic_passed:
            review_feedback = logic_report
            continue
        quality_report, quality_passed = await _review(
            "quality", task, diagnosis, implementation, workspace, budget, attempt
        )
        if quality_passed:
            await record_event("swebench_agent_completed", "review", {"attempt": attempt})
            return {
                "diagnosis": diagnosis,
                "implementation": implementation,
                "logic_review": logic_report,
                "quality_review": quality_report,
                "attempts": attempt,
                "execution": execution,
            }
        review_feedback = quality_report

    raise AgentRepairError(
        f"Agent 在 {SWEBENCH_MAX_FIX_ATTEMPTS} 次修复后仍未通过两轮审查"
    )


async def _diagnose(task: str, workspace: Path, budget: TokenBudget) -> str:
    system = load_system_prompt("debugger") + "\n\n" + SYSTEM_RULES
    user = (
        f"## SWE-bench Issue\n{task}\n\n"
        "请使用只读工具检查仓库，输出可追溯的问题分析报告和明确修复方向。"
        "不得请求日志打点或人工补充；信息不足时基于源码作出最合理判断。"
    )
    await record_event("stage_started", "diagnose", {"instance": task.splitlines()[0]})
    result = await run_fc_loop(
        build_node_messages(system, user), PLAN_MODE_TOOLS, budget, "plan",
        str(workspace), skill_id="debug-diagnosis", react_enabled=True,
    )
    await record_event("stage_output", "diagnose", {"report": result[:1200]})
    return result


async def _fix(
    task: str,
    diagnosis: str,
    review_feedback: str,
    workspace: Path,
    budget: TokenBudget,
    attempt: int,
) -> tuple[str, dict[str, Any]]:
    system = load_system_prompt("developer") + "\n\n" + SYSTEM_RULES
    feedback = f"## 上轮正式审查报告\n{review_feedback}\n\n" if review_feedback else ""
    user = (
        f"## SWE-bench Issue\n{task}\n\n"
        f"## 问题分析报告\n{diagnosis}\n\n{feedback}"
        f"这是第 {attempt} 次修复。请检查源码并使用 write_file 实际完成最小修复。"
        "可执行白名单内的相关测试。不要修改测试文件。最后输出代码变更与实现说明。"
    )
    execution: dict[str, Any] = {
        "successful_writes": [], "failed_writes": [], "tool_calls": [], "tool_failures": [],
    }
    result = await run_fc_loop(
        build_node_messages(system, user), BUILD_MODE_TOOLS, budget, "build",
        str(workspace), execution_report=execution, skill_id="code-development",
        react_enabled=True,
    )
    await record_event("build_execution", "fix", {
        "attempt": attempt,
        "successful_writes": [item["path"] for item in execution["successful_writes"]],
        "failed_writes": [item["path"] for item in execution["failed_writes"]],
    })
    return result, execution


async def _review(
    perspective: str,
    task: str,
    diagnosis: str,
    implementation: str,
    workspace: Path,
    budget: TokenBudget,
    attempt: int,
) -> tuple[str, bool]:
    role = "review_logic" if perspective == "logic" else "review_quality"
    focus = (
        "只检查修复是否覆盖 issue 根因、行为逻辑和回归边界。"
        if perspective == "logic"
        else "只检查代码规范、可维护性、测试充分性和无关改动。"
    )
    system = load_system_prompt(role) + "\n\n" + SYSTEM_RULES + "\n" + focus
    user = (
        f"## SWE-bench Issue\n{task}\n\n## 问题分析报告\n{diagnosis}\n\n"
        f"## 开发者实现说明（不可信，仅供定位）\n{implementation}\n\n"
        "请用只读工具核对当前工作区源码。报告必须包含审查标记及审查判定数据 JSON。"
    )
    report = await run_fc_loop(
        build_node_messages(system, user), PLAN_MODE_TOOLS, budget, "plan",
        str(workspace), skill_id="code-review", react_enabled=True,
    )
    parsed = parse_review_report(report, perspective)
    await record_event("review_finished", f"review_{perspective}", {
        "attempt": attempt, "passed": parsed["passed"],
        "finding_count": len(parsed["findings"]), "parse_errors": parsed["parse_errors"],
    })
    return report, bool(parsed["passed"])
