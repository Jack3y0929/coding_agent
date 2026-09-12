"""DEV 增量开发工作流状态图"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import StateGraph, END

from backend.budget import BudgetExceededError, get_workflow_budget
from backend.config import MAX_FIX_ATTEMPTS, PROJECT_ROOT
from backend.db.models import Database
from backend.graph.context import push_progress, wait_for_human
from backend.graph.nodes import (
    BUILD_REACT_POLICY,
    PLAN_MODE_TOOLS,
    PLAN_REACT_POLICY,
    BUILD_MODE_TOOLS,
    build_node_messages,
    inject_rag_context,
    load_system_prompt,
    run_fc_loop,
)
from backend.graph.review import aggregate_review_reports, run_review_perspective
from backend.rag.retriever import init_spec_library

logger = logging.getLogger("graph.dev")


# ---- State 定义 ----

class DevWorkflowState(TypedDict):
    stage: str
    coding_intent: dict[str, Any]
    slot_validation: dict[str, Any]
    manual_workflow_type: str | None
    clarification_doc: str | None
    requirement_doc: str | None
    plan: str | None
    code_changes: dict[str, str] | None
    build_execution: dict[str, Any] | None
    fix_execution: dict[str, Any] | None
    tool_failures: list[dict[str, Any]]
    last_tool_error: dict[str, Any] | None
    tool_failure_exhausted: bool
    build_attempt: int
    build_blocked: bool
    implementation_note: str | None
    review_report: str | None
    review_logic_report: str | None
    review_security_report: str | None
    review_quality_report: str | None
    review_logic_result: dict[str, Any] | None
    review_security_result: dict[str, Any] | None
    review_quality_result: dict[str, Any] | None
    review_logic_tokens: int
    review_security_tokens: int
    review_quality_tokens: int
    review_aggregate_tokens: int
    review_findings: list[dict[str, Any]]
    review_passed: bool
    review_round: int
    fix_attempt: int
    token_used: int
    status: str
    human_decision: str | None
    intervention_decision: str | None
    validation_passed: bool | None
    validation_result: dict[str, Any] | None
    session_id: str
    project_path: str
    description: str
    workflow_type: str
    document_id: str


# ---- 节点函数 ----

async def node_input_gate(state: DevWorkflowState) -> dict[str, Any]:
    """澄清节点：模拟澄清流程，产出澄清文档"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "input_gate",
                              "message": "需求澄清中..."})

    intent, validation = await _resolve_intent_slots(state, sid)

    clarification = (
        f"# 澄清文档\n\n"
        f"## 用户原始描述\n{state['description']}\n\n"
        f"## 目标项目\n项目路径: {state['project_path']}\n\n"
        f"## 已确认信息\n"
        f"- 任务类型: {intent.task_type.upper()}\n"
        f"- 目标模块: {', '.join(intent.target_modules or intent.change_scope) or '未确认'}\n"
        f"- 验收标准: {'；'.join(intent.acceptance_criteria) or '未确认'}\n"
        f"- 技术约束: {'；'.join(intent.tech_constraints) or '无'}\n"
    )

    async with Database() as db:
        await db.add_artifact(sid, "clarification", clarification)

    logger.info(f"[{sid}] 澄清文档已生成")
    return {
        "clarification_doc": clarification,
        "coding_intent": intent.model_dump(),
        "slot_validation": validation.model_dump(),
        "stage": "analyze",
    }


async def node_analyze(state: DevWorkflowState) -> dict[str, Any]:
    """需求分析师节点：独立上下文，产出需求概括文档"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "analyze",
                              "message": "需求分析中..."})

    system_prompt = load_system_prompt("analyst")
    rag_ctx = await inject_rag_context("analyst", state["clarification_doc"] or "", state["project_path"])

    user_content = (
        f"## 已确认研发槽位\n```json\n{json.dumps(state['coding_intent'], ensure_ascii=False, indent=2)}\n```\n\n"
        f"## 澄清文档\n{state['clarification_doc']}\n\n"
        f"## 项目路径\n{state['project_path']}\n\n"
        f"{rag_ctx}\n\n"
        f"请直接根据已确认槽位输出一份简短、可执行的需求概括文档。"
        f"不要重复提问或展开需求访谈，只保留项目定义、功能范围、约束、验收标准和风险。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = get_workflow_budget()

    try:
        result = await run_fc_loop(messages, PLAN_MODE_TOOLS, budget, "plan",
                                   state["project_path"], skill_id="requirements-analysis")
    except BudgetExceededError:
        result = "# 需求概括文档\n\n（预算超限，分析未完成）\n"

    from backend.artifacts import write_requirement
    requirement_path = write_requirement(state["document_id"], state["project_path"], result)
    structured_content = open(requirement_path, encoding="utf-8").read()
    async with Database() as db:
        await db.add_artifact(sid, "requirement", structured_content)

    logger.info(f"[{sid}] 需求概括文档已生成 ({len(result)} 字符)")
    return {"requirement_doc": result, "token_used": budget.total_input_tokens + budget.total_output_tokens}


async def node_develop_plan(state: DevWorkflowState) -> dict[str, Any]:
    """开发者 Plan 模式：只读分析，制定方案"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "develop_plan",
                              "message": "开发规划中 (Plan模式)..."})

    system_prompt = load_system_prompt("developer") + (
        "\n\n当前处于 **Plan 模式**。你只能使用 read_file 和 search_code 工具分析代码。"
        "严禁写文件或执行修改性命令。"
        "\n采用有限轮次的行动-观察循环：先读取或搜索证据，再根据工具结果决定下一步。"
        "不要重复完全相同的调用，不要输出隐藏思维链。"
        "\n制定完备的实现方案后，调用 save_checkpoint 工具请求切换到 Build 模式。"
    )
    rag_ctx = await inject_rag_context("developer", state["requirement_doc"] or "", state["project_path"])

    user_content = (
        f"## 已确认研发槽位\n```json\n{json.dumps(state['coding_intent'], ensure_ascii=False, indent=2)}\n```\n\n"
        f"## 需求概括文档\n{state['requirement_doc']}\n\n"
        f"## 项目路径\n{state['project_path']}\n\n"
        f"{rag_ctx}\n\n"
        f"请先阅读相关源码，然后制定详细的实现方案。方案应包含：涉及哪些文件、修改哪些函数、"
        f"数据流设计、异常处理策略。方案制定完成后，调用 save_checkpoint 请求 Build 模式。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = get_workflow_budget()

    try:
        plan = await run_fc_loop(
            messages,
            PLAN_MODE_TOOLS,
            budget,
            "plan",
            state["project_path"],
            skill_id="code-development",
            react_enabled=True,
            react_policy=PLAN_REACT_POLICY,
        )
    except BudgetExceededError:
        plan = "# 开发方案\n\n（预算超限，规划未完成）\n"

    async with Database() as db:
        await db.add_artifact(sid, "plan", plan)

    from backend.artifacts import write_prompt
    write_prompt(state["document_id"], state["project_path"], plan)

    logger.info(f"[{sid}] 开发方案已生成 ({len(plan)} 字符)")
    return {"plan": plan, "stage": "develop_build",
            "token_used": budget.total_input_tokens + budget.total_output_tokens}


async def node_develop_build(state: DevWorkflowState) -> dict[str, Any]:
    """开发者 Build 模式：按 Plan 方案写代码"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "develop_build",
                              "message": "代码开发中 (Build模式)..."})

    system_prompt = load_system_prompt("developer") + (
        f"\n\n当前处于 **Build 模式**。Plan 方案：\n{state.get('plan', '(无Plan)')}\n"
        "你可以使用全部工具（write_file、execute_shell等），但必须遵循安全校验。"
        "\n采用有限轮次的行动-观察循环：写入后继续执行验证，根据验证结果决定是否修复。"
        "不要重复完全相同的调用，不要输出隐藏思维链。"
        "\n完成后在回复末尾用清晰标记注明代码变更和实现说明。"
    )

    user_content = (
        f"## 已确认研发槽位\n```json\n{json.dumps(state['coding_intent'], ensure_ascii=False, indent=2)}\n```\n\n"
        f"## 需求概括文档\n{state['requirement_doc']}\n\n"
        f"## 项目路径\n{state['project_path']}\n\n"
        f"请按照 Plan 方案实施代码变更。完成后请用 ## 代码变更 和 ## 实现说明 "
        f"两部分总结所做的工作。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = get_workflow_budget()

    execution_report: dict[str, Any] = {
        "successful_writes": [], "failed_writes": [], "tool_calls": [], "tool_failures": [],
    }
    try:
        result_text = await run_fc_loop(
            messages,
            BUILD_MODE_TOOLS,
            budget,
            "build",
            state["project_path"],
            execution_report,
            skill_id="code-development",
            react_enabled=True,
            react_policy=BUILD_REACT_POLICY,
        )
    except BudgetExceededError:
        result_text = "## 代码变更\n\n（预算超限，开发未完成）\n\n## 实现说明\n\n开发被中断。\n"

    # 代码变更只来自真实成功的 write_file，不再把模型说明伪装成 output.txt。
    code_changes = dict(state.get("code_changes") or {})
    code_changes.update(_changes_from_execution(execution_report, state["project_path"]))
    impl_note = _parse_implementation_note(result_text)
    build_attempt = state.get("build_attempt", 0) + 1
    build_blocked = not execution_report.get("successful_writes")
    from backend.trace import record_event
    from backend.fallback.engine import after_build
    from backend.trace import record_fallback_decision
    await record_event("build_execution", "develop_build", {
        "successful_writes": [item["path"] for item in execution_report.get("successful_writes", [])],
        "failed_writes": [item["path"] for item in execution_report.get("failed_writes", [])],
        "tool_call_count": len(execution_report.get("tool_calls", [])),
        "build_attempt": build_attempt,
        "passed": not build_blocked,
    })
    await record_fallback_decision(after_build(
        len(execution_report.get("successful_writes") or []), build_attempt, MAX_FIX_ATTEMPTS,
    ), "develop_build")
    if build_blocked:
        await push_progress(sid, {
            "event": "progress", "stage": "develop_build",
            "message": f"Build 未产生实际文件，第{build_attempt}次重试...",
        })

    async with Database() as db:
        await db.add_artifact(sid, "code_changes",
                              json.dumps(code_changes, ensure_ascii=False))

    from backend.artifacts import write_example_code
    write_example_code(
        state["document_id"],
        state["project_path"],
        json.dumps(code_changes, ensure_ascii=False, indent=2),
    )

    logger.info(f"[{sid}] 代码开发完成 ({len(result_text)} 字符)")
    return {
        "code_changes": code_changes,
        "implementation_note": impl_note,
        "build_execution": execution_report,
        "tool_failures": execution_report.get("tool_failures", []),
        "last_tool_error": execution_report.get("last_tool_error"),
        "tool_failure_exhausted": bool(execution_report.get("tool_failure_exhausted")),
        "build_attempt": build_attempt,
        "build_blocked": build_blocked,
        "token_used": budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_review_start(state: DevWorkflowState) -> dict[str, Any]:
    """启动同一轮的三路独立审查。"""
    sid = state["session_id"]
    review_round = state.get("review_round", 0) + 1
    await push_progress(sid, {"event": "progress", "stage": "review",
                              "message": f"启动多视角代码审查（第{review_round}轮）..."})
    return {}



async def node_review_logic(state: DevWorkflowState) -> dict[str, Any]:
    """逻辑与需求符合性审查。"""
    return await run_review_perspective(state, "logic", "dev")


async def node_review_security(state: DevWorkflowState) -> dict[str, Any]:
    """安全与边界防护审查。"""
    return await run_review_perspective(state, "security", "dev")


async def node_review_quality(state: DevWorkflowState) -> dict[str, Any]:
    """工程质量与可维护性审查。"""
    return await run_review_perspective(state, "quality", "dev")


async def node_review_aggregate(state: DevWorkflowState) -> dict[str, Any]:
    """等待三路审查完成，确定性汇总并产生唯一的路由结论。"""
    sid = state["session_id"]
    aggregate = aggregate_review_reports(state, "dev", required_rounds=1)
    review_round = aggregate["review_round"]
    review_passed = aggregate["review_passed"]
    aggregate["token_used"] = sum(
        int(state.get(key, 0) or 0)
        for key in ("review_logic_tokens", "review_security_tokens", "review_quality_tokens")
    ) + state.get("token_used", 0)

    async with Database() as db:
        await db.add_artifact(sid, "review", aggregate["review_report"])

    from backend.fallback.engine import after_review
    from backend.trace import record_event, record_fallback_decision
    await record_event("review_aggregated", "review_aggregate", {
        "review_round": review_round,
        "passed": review_passed,
        "finding_count": len(aggregate["review_findings"]),
        "perspectives": ["logic", "security", "quality"],
    })
    await record_fallback_decision(after_review(
        review_passed, review_round, 1, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    ), "review_aggregate")
    logger.info(f"[{sid}] 多视角审查完成 (第{review_round}轮, {'通过' if review_passed else '不通过'})")
    return aggregate


async def node_validate(state: DevWorkflowState) -> dict[str, Any]:
    """在审查前执行确定性产物门禁和用户确认的验证命令。"""
    sid = state["session_id"]
    from backend.intent.models import CodingIntent
    from backend.tools.shell_tools import execute_shell
    from backend.trace import record_event

    intent = CodingIntent.model_validate(state["coding_intent"])
    checks = _validate_dev_artifacts(state, intent)
    commands = intent.validation_commands
    if not commands and checks["passed"]:
        await record_event("validation_result", "validation", checks)
        from backend.fallback.engine import after_validation
        from backend.trace import record_fallback_decision
        await record_fallback_decision(after_validation(True, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS), "validate")
        return {"validation_passed": True, "validation_result": checks}
    await push_progress(sid, {"event": "progress", "stage": "validate", "message": "执行验证命令中..."})
    results = [await execute_shell(command, cwd=state["project_path"]) for command in commands]
    passed = checks["passed"] and all("[exit_code: 0]" in result for result in results)
    await record_event("validation_result", "validation", {
        "passed": passed, "artifact_checks": checks, "commands": commands,
        "results": [result[:1000] for result in results],
    })
    from backend.fallback.engine import after_validation
    from backend.trace import record_fallback_decision
    await record_fallback_decision(after_validation(passed, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS), "validate")
    validation_result = {
        "passed": passed,
        "artifact_checks": checks,
        "commands": commands,
        "results": [result[:1000] for result in results],
    }
    return {"validation_passed": passed, "validation_result": validation_result}


def _validate_dev_artifacts(state: DevWorkflowState, intent: Any) -> dict[str, Any]:
    """验证实际文件和关键内容，防止仅输出说明文本就进入审查。"""
    root = Path(state["project_path"])
    execution = state.get("fix_execution") if state.get("fix_execution") is not None else state.get("build_execution")
    execution = execution or {}
    writes = execution.get("successful_writes") or []
    changes = state.get("code_changes") or {}
    paths = [str(path) for path in changes.keys()]
    required = []
    description = f"{intent.task_description} {' '.join(intent.acceptance_criteria)}".lower()
    if "hello-dev" in description or "index.html" in description:
        required = ["hello-dev/index.html", "hello-dev/style.css", "hello-dev/script.js", "hello-dev/README.md"]
    errors: list[str] = []
    if not writes:
        errors.append("代码变更为空，Build 未产生 write_file 结果")
    for relative in required:
        path = root / relative
        if not path.is_file():
            errors.append(f"缺少文件: {relative}")
            continue
        if not path.read_text(encoding="utf-8", errors="replace").strip():
            errors.append(f"文件为空: {relative}")
    existing = {p.replace("\\", "/") for p in paths}
    if required and not all(item in existing for item in required):
        errors.append("代码变更清单未覆盖全部需求文件")
    html = root / "hello-dev/index.html"
    js = root / "hello-dev/script.js"
    if html.is_file():
        content = html.read_text(encoding="utf-8", errors="replace").lower()
        if "<html" not in content or "<script" not in content:
            errors.append("index.html 缺少基本 HTML 或 script 结构")
        if "hello dev" not in content:
            errors.append("index.html 未包含 Hello DEV")
    if js.is_file():
        content = js.read_text(encoding="utf-8", errors="replace")
        if "addEventListener" not in content and "onclick" not in content:
            errors.append("script.js 未发现按钮交互绑定")
        if "DEV" not in content:
            errors.append("script.js 未包含成功状态文案")
    return {"passed": not errors, "required_files": required, "errors": errors}


async def node_fix(state: DevWorkflowState) -> dict[str, Any]:
    """开发者修复节点：根据审查报告修改代码"""
    sid = state["session_id"]
    fix_attempt = state.get("fix_attempt", 0) + 1

    await push_progress(sid, {"event": "progress", "stage": "fix",
                              "message": f"修复问题中 (第{fix_attempt}次)..."})

    system_prompt = load_system_prompt("developer") + (
        "\n\n你处于修复模式。严格只修复审查报告中列出的问题，不要改动无关代码。"
    )
    code_changes_str = json.dumps(state.get("code_changes", {}), ensure_ascii=False, indent=2)

    user_content = (
        f"## 审查报告\n{state['review_report']}\n\n"
        f"## 当前代码变更\n```json\n{code_changes_str}\n```\n\n"
        f"## 项目路径\n{state['project_path']}\n\n"
        f"请逐一修复审查报告中列出的问题。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = get_workflow_budget()

    execution_report: dict[str, Any] = {
        "successful_writes": [], "failed_writes": [], "tool_calls": [], "tool_failures": [],
    }
    try:
        result_text = await run_fc_loop(messages, BUILD_MODE_TOOLS, budget, "build",
                                        state["project_path"], execution_report, skill_id="code-development")
    except BudgetExceededError:
        result_text = "## 代码变更\n\n（预算超限，修复未完成）\n\n## 实现说明\n\n修复被中断。\n"

    code_changes = dict(state.get("code_changes") or {})
    code_changes.update(_changes_from_execution(execution_report, state["project_path"]))
    impl_note = _parse_implementation_note(result_text)
    from backend.trace import record_event
    from backend.fallback.engine import after_build
    from backend.trace import record_fallback_decision
    await record_event("build_execution", "fix", {
        "successful_writes": [item["path"] for item in execution_report.get("successful_writes", [])],
        "failed_writes": [item["path"] for item in execution_report.get("failed_writes", [])],
        "tool_call_count": len(execution_report.get("tool_calls", [])),
        "passed": bool(execution_report.get("successful_writes")),
    })
    await record_fallback_decision(after_build(
        len(execution_report.get("successful_writes") or []), fix_attempt, MAX_FIX_ATTEMPTS,
    ), "fix")
    if not execution_report.get("successful_writes"):
        await push_progress(sid, {
            "event": "progress", "stage": "fix",
            "message": f"修复未产生实际文件，第{fix_attempt}次重试...",
        })

    logger.info(f"[{sid}] 修复完成 (第{fix_attempt}次)")
    return {
        "code_changes": code_changes,
        "implementation_note": impl_note,
        "fix_execution": execution_report,
        "tool_failures": execution_report.get("tool_failures", []),
        "last_tool_error": execution_report.get("last_tool_error"),
        "tool_failure_exhausted": bool(execution_report.get("tool_failure_exhausted")),
        "fix_attempt": fix_attempt,
        "token_used": budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_human_accept(state: DevWorkflowState) -> dict[str, Any]:
    """人工验收节点：暂停等待人类在 Web UI 确认"""
    sid = state["session_id"]
    logger.info(f"[{sid}] 进入人工验收节点")

    decision_result = await wait_for_human(sid, "human_accept")

    human_decision = decision_result.get("decision", "rejected")
    feedback = decision_result.get("feedback", {})
    from backend.trace import add_trace_label, current_trace_id, record_event
    trace_id = current_trace_id()
    if trace_id and feedback.get("outcome"):
        feedback["outcome"] = feedback["outcome"]
        feedback["preference_scope_value"] = (
            feedback.get("preference_scope_value") or state.get("project_path", "")
        )
        await add_trace_label(trace_id, feedback)
    await record_event("human_decision", "human_accept", {
        "decision": human_decision,
        "outcome": feedback.get("outcome"),
        "adoption": feedback.get("adoption"),
    })
    from backend.fallback.engine import after_human_acceptance
    from backend.trace import record_fallback_decision
    await record_fallback_decision(after_human_acceptance(
        human_decision, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    ), "human_accept")
    logger.info(f"[{sid}] 人工验收决定: {human_decision}")

    return {"human_decision": human_decision, "status": "running"}


async def node_human_intervene(state: DevWorkflowState) -> dict[str, Any]:
    """自动修复或验证上限耗尽时，停止自动化并等待人工决策。"""
    sid = state["session_id"]
    from backend.trace import record_event
    await push_progress(sid, {"event": "progress", "stage": "human_intervene", "message": "自动修复已停止，等待人工介入"})
    response = await wait_for_human(sid, "human_intervene")
    decision = response.get("decision", "stop")
    await record_event("human_intervention", "human_intervene", {"decision": decision})
    from backend.fallback.engine import after_intervention
    from backend.trace import record_fallback_decision
    await record_fallback_decision(after_intervention(decision), "human_intervene")
    # 人工授权后从新的尝试窗口开始，避免刚达到上限就再次立即触发介入。
    reset_attempts = decision == "retry"
    return {
        "intervention_decision": decision,
        "build_attempt": 0 if reset_attempts and state.get("build_blocked") else state.get("build_attempt", 0),
        "fix_attempt": 0 if reset_attempts and not state.get("build_blocked") else state.get("fix_attempt", 0),
        "status": "running",
    }


async def node_output_summary(state: DevWorkflowState) -> dict[str, Any]:
    """生成总结：写入长期记忆 + 触发RAG增量更新"""
    sid = state["session_id"]

    code_changes = state.get("code_changes", {}) or {}
    file_count = len(code_changes)
    review_rounds = state.get("review_round", 0)
    total_tokens = state.get("token_used", 0)
    terminal_status = "failed" if state.get("intervention_decision") == "stop" else "done"
    summary_title = "# 任务失败" if terminal_status == "failed" else "# 开发完成"
    final_state = (
        "人工终止，任务失败" if terminal_status == "failed"
        else "人工验收通过" if state.get("human_decision") == "approved"
        else "审查通过"
    )

    summary = (
        f"{summary_title}\n\n"
        f"- 变更文件数: {file_count}\n"
        f"- 审查轮次: {review_rounds}\n"
        f"- 总Token消耗: {total_tokens}\n"
        f"- 最终状态: {final_state}\n\n"
        f"## 变更文件\n"
        + "\n".join(f"- {fp}" for fp in code_changes.keys())
    )

    async with Database() as db:
        summary_artifact_id = await db.add_artifact(sid, "output_summary", summary)

    from backend.artifacts import write_feedback
    write_feedback(state["document_id"], state["project_path"], summary)

    # 长期记忆只保存提炼后的结果和经验，完整原文保留在 artifacts/docs。
    from backend.rag.indexer import CodeIndexer
    from backend.rag.memory_service import build_dev_memory_entries
    indexer = CodeIndexer()
    memory_state = {**state, "summary": summary, "source_artifact_id": summary_artifact_id}
    # 长期记忆属于可选收尾步骤；索引故障不能否定已完成的人工验收。
    try:
        await indexer.index_long_term_memory_entries(
            build_dev_memory_entries(memory_state),
            source_session_id=sid,
            project_path=state.get("project_path", ""),
        )
    except Exception as exc:
        logger.warning("[%s] 长期记忆索引失败，保留核心工作流结果: %s", sid, exc)
        try:
            from backend.config import DB_PATH
            from backend.trace import record_event
            await record_event("memory_index_failed", "output", {
                "error": str(exc),
                "db_path": indexer._db_path or DB_PATH,
            })
        except Exception as event_exc:
            logger.warning("[%s] 无法记录长期记忆索引失败事件: %s", sid, event_exc)

    # 增量更新RAG索引
    if code_changes:
        changed_files = [
                f"{state['project_path']}/{fp}" if state['project_path'] else fp
                for fp in code_changes.keys()
        ]
        try:
            await indexer.incremental_update(changed_files, project_path=state.get("project_path", ""))
        except Exception as exc:
            logger.warning(f"增量RAG更新失败: {exc}")

    await push_progress(sid, {
        "event": "complete" if terminal_status == "done" else "error",
        "stage": "output",
        "message": "工作流完成" if terminal_status == "done" else "人工终止自动化任务",
        "summary": summary,
    })

    logger.info(f"[{sid}] 工作流完成总结: {file_count}个文件变更")
    return {"status": terminal_status}


# ---- 条件路由 ----

def route_after_validation(state: DevWorkflowState) -> Literal["review", "fix", "human_intervene"]:
    from backend.fallback.engine import after_validation
    decision = after_validation(bool(state.get("validation_passed")), state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS)
    return decision.next_stage or "human_intervene"


def route_after_build(state: DevWorkflowState) -> Literal["validate", "develop_build", "human_intervene"]:
    """Build 未产生真实写入时，禁止进入验证节点。"""
    from backend.fallback.engine import after_build
    execution = state.get("build_execution") or {}
    decision = after_build(
        len(execution.get("successful_writes") or []),
        state.get("build_attempt", 0),
        MAX_FIX_ATTEMPTS,
    )
    return decision.next_stage or "human_intervene"


def route_after_fix(state: DevWorkflowState) -> Literal["validate", "fix", "human_intervene"]:
    """修复节点同样必须有真实写入才允许进入验证。"""
    from backend.fallback.engine import after_build
    execution = state.get("fix_execution") or {}
    decision = after_build(
        len(execution.get("successful_writes") or []),
        state.get("fix_attempt", 0),
        MAX_FIX_ATTEMPTS,
    )
    if decision.next_stage == "develop_build":
        return "fix"
    return decision.next_stage or "human_intervene"


def route_after_review(state: DevWorkflowState) -> Literal["fix", "human_accept", "review", "human_intervene"]:
    from backend.fallback.engine import after_review
    decision = after_review(
        state.get("review_passed", False), state.get("review_round", 0), 1,
        state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    )
    return decision.next_stage or "human_intervene"


def route_after_human(state: DevWorkflowState) -> Literal["fix", "output", "human_intervene"]:
    from backend.fallback.engine import after_human_acceptance
    decision = after_human_acceptance(state.get("human_decision"), state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS)
    return decision.next_stage or "human_intervene"


def route_after_intervention(state: DevWorkflowState) -> Literal["fix", "develop_build", "output"]:
    from backend.fallback.engine import after_intervention
    next_stage = after_intervention(state.get("intervention_decision")).next_stage or "output"
    if next_stage == "fix" and state.get("build_blocked"):
        return "develop_build"
    return next_stage


# ---- 构建状态图 ----

async def build_dev_workflow() -> StateGraph:
    """构建并编译 DEV 工作流状态图"""
    from langgraph.checkpoint.memory import MemorySaver

    # 初始化规范库
    try:
        await init_spec_library()
    except Exception as exc:
        logger.warning(f"规范库初始化失败: {exc}")

    workflow = StateGraph(DevWorkflowState)

    # 添加节点
    workflow.add_node("input_gate", node_input_gate)
    workflow.add_node("analyze", node_analyze)
    workflow.add_node("develop_plan", node_develop_plan)
    workflow.add_node("develop_build", node_develop_build)
    workflow.add_node("validate", node_validate)
    workflow.add_node("review_start", node_review_start)
    workflow.add_node("review_logic", node_review_logic)
    workflow.add_node("review_security", node_review_security)
    workflow.add_node("review_quality", node_review_quality)
    workflow.add_node("review_aggregate", node_review_aggregate)
    workflow.add_node("fix", node_fix)
    workflow.add_node("human_accept", node_human_accept)
    workflow.add_node("human_intervene", node_human_intervene)
    workflow.add_node("output", node_output_summary)

    # 设置入口
    workflow.set_entry_point("input_gate")

    # 添加边
    workflow.add_edge("input_gate", "analyze")
    workflow.add_edge("analyze", "develop_plan")
    workflow.add_edge("develop_plan", "develop_build")
    workflow.add_conditional_edges("develop_build", route_after_build, {
        "validate": "validate", "develop_build": "develop_build", "human_intervene": "human_intervene",
    })
    workflow.add_conditional_edges("validate", route_after_validation, {
        "review": "review_start", "fix": "fix", "human_intervene": "human_intervene",
    })

    # 同一轮的三个审查视角并行执行，汇总节点等待全部结果。
    workflow.add_edge("review_start", "review_logic")
    workflow.add_edge("review_start", "review_security")
    workflow.add_edge("review_start", "review_quality")
    workflow.add_edge(["review_logic", "review_security", "review_quality"], "review_aggregate")

    # 汇总审查后的条件分支
    workflow.add_conditional_edges("review_aggregate", route_after_review, {
        "fix": "fix",
        "human_accept": "human_accept",
        "review": "review_start",
        "human_intervene": "human_intervene",
    })
    workflow.add_conditional_edges("fix", route_after_fix, {
        "validate": "validate", "fix": "fix", "human_intervene": "human_intervene",
    })

    # 人工验收后的条件分支
    workflow.add_conditional_edges("human_accept", route_after_human, {
        "fix": "fix",
        "output": "output",
        "human_intervene": "human_intervene",
    })
    workflow.add_conditional_edges("human_intervene", route_after_intervention, {
        "fix": "fix", "develop_build": "develop_build", "output": "output",
    })
    workflow.add_edge("output", END)

    return workflow.compile(checkpointer=MemorySaver())


# ---- 辅助函数 ----

def _parse_implementation_note(text: str) -> str:
    """只提取实现说明；文本本身永远不视为文件变更。"""
    if "## 实现说明" not in text:
        return text.strip()
    return text.split("## 实现说明", 1)[1].strip()


def _changes_from_execution(execution_report: dict[str, Any], project_path: str | None = None) -> dict[str, str]:
    """将成功 write_file 调用转换为正式代码变更，重复路径以后一次为准。"""
    changes: dict[str, str] = {}
    for item in execution_report.get("successful_writes", []):
        path = _normalize_change_path(str(item.get("path", "")), project_path)
        if path:
            changes[path] = str(item.get("content", ""))
    return changes


def _normalize_change_path(path: str, project_path: str | None = None) -> str:
    """将写入回执统一成项目根相对 POSIX 路径，避免绝对/相对键重复。"""
    raw = path.replace("\\", "/").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    if project_path and candidate.is_absolute():
        try:
            raw = candidate.resolve().relative_to(Path(project_path).resolve()).as_posix()
        except ValueError:
            return raw
    raw = raw.lstrip("./")
    normalized = Path(raw).as_posix()
    return "" if normalized in {"", "."} or normalized.startswith("../") else normalized


async def _resolve_intent_slots(
    state: DevWorkflowState,
    session_id: str,
) -> tuple[Any, Any]:
    """槽位不足或流程冲突时，阻止开发并等待用户补充。"""
    from backend.intent.context import bind_intent
    from backend.intent.models import CodingIntent
    from backend.intent.rules import validate_slots
    from backend.intent.service import apply_manual_override
    from backend.trace import record_event

    intent = CodingIntent.model_validate(state["coding_intent"])
    validation = validate_slots(intent, state.get("manual_workflow_type"))
    while validation.decision == "clarify":
        await record_event("clarification_requested", "intent", validation.model_dump())
        await push_progress(session_id, {
            "event": "progress", "stage": "intent_clarify",
            "message": "研发槽位不足或流程冲突，等待补充",
            "intent": intent.model_dump(), "validation": validation.model_dump(),
        })
        response = await wait_for_human(session_id, "intent_clarify")
        updates = response.get("intent_update", {})
        selected = updates.pop("workflow_type", None)
        if selected in {"dev", "debug"}:
            intent = apply_manual_override(intent, selected)
        intent = intent.model_copy(update={
            key: value for key, value in updates.items()
            if key in CodingIntent.model_fields and value not in ("", [])
        })
        bind_intent(intent)
        validation = validate_slots(intent, selected or state.get("manual_workflow_type"))
        await record_event("slots_updated", "intent", {"intent": intent.model_dump()})
        await record_event("slot_validation", "intent", validation.model_dump())
    return intent, validation
