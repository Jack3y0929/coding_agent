"""DEV 增量开发工作流状态图"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, TypedDict

from langgraph.graph import StateGraph, END

from backend.budget import BudgetExceededError, TokenBudget
from backend.config import MAX_FIX_ATTEMPTS, PROJECT_ROOT
from backend.db.models import Database
from backend.graph.context import push_progress, wait_for_human
from backend.graph.nodes import (
    PLAN_MODE_TOOLS,
    BUILD_MODE_TOOLS,
    build_node_messages,
    inject_rag_context,
    load_system_prompt,
    run_fc_loop,
)
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
    implementation_note: str | None
    review_report: str | None
    review_passed: bool
    review_round: int
    fix_attempt: int
    token_used: int
    status: str
    human_decision: str | None
    intervention_decision: str | None
    validation_passed: bool | None
    session_id: str
    project_path: str
    description: str
    workflow_type: str


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
        f"请根据以上信息，输出需求概括文档。按照你定义的四阶段流程进行分析后，"
        f"输出包含项目定义、核心功能范围、技术栈约束、关键成功标准、风险清单的结构化文档。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = TokenBudget()

    try:
        result = await run_fc_loop(messages, PLAN_MODE_TOOLS, budget, "plan",
                                   state["project_path"])
    except BudgetExceededError:
        result = "# 需求概括文档\n\n（预算超限，分析未完成）\n"

    async with Database() as db:
        await db.add_artifact(sid, "requirement", result)

    logger.info(f"[{sid}] 需求概括文档已生成 ({len(result)} 字符)")
    return {"requirement_doc": result, "token_used": state.get("token_used", 0) +
            budget.total_input_tokens + budget.total_output_tokens}


async def node_develop_plan(state: DevWorkflowState) -> dict[str, Any]:
    """开发者 Plan 模式：只读分析，制定方案"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "develop_plan",
                              "message": "开发规划中 (Plan模式)..."})

    system_prompt = load_system_prompt("developer") + (
        "\n\n当前处于 **Plan 模式**。你只能使用 read_file 和 search_code 工具分析代码。"
        "严禁写文件或执行修改性命令。"
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
    budget = TokenBudget()

    try:
        plan = await run_fc_loop(messages, PLAN_MODE_TOOLS, budget, "plan",
                                 state["project_path"])
    except BudgetExceededError:
        plan = "# 开发方案\n\n（预算超限，规划未完成）\n"

    async with Database() as db:
        await db.add_artifact(sid, "plan", plan)

    logger.info(f"[{sid}] 开发方案已生成 ({len(plan)} 字符)")
    return {"plan": plan, "stage": "develop_build",
            "token_used": state.get("token_used", 0) +
            budget.total_input_tokens + budget.total_output_tokens}


async def node_develop_build(state: DevWorkflowState) -> dict[str, Any]:
    """开发者 Build 模式：按 Plan 方案写代码"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "develop_build",
                              "message": "代码开发中 (Build模式)..."})

    system_prompt = load_system_prompt("developer") + (
        f"\n\n当前处于 **Build 模式**。Plan 方案：\n{state.get('plan', '(无Plan)')}\n"
        "你可以使用全部工具（write_file、execute_shell等），但必须遵循安全校验。"
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
    budget = TokenBudget()

    try:
        result_text = await run_fc_loop(messages, BUILD_MODE_TOOLS, budget, "build",
                                        state["project_path"])
    except BudgetExceededError:
        result_text = "## 代码变更\n\n（预算超限，开发未完成）\n\n## 实现说明\n\n开发被中断。\n"

    # 从结果中提取代码变更和实现说明
    code_changes, impl_note = _parse_developer_output(result_text)

    async with Database() as db:
        await db.add_artifact(sid, "code_changes",
                              json.dumps(code_changes, ensure_ascii=False))

    logger.info(f"[{sid}] 代码开发完成 ({len(result_text)} 字符)")
    return {
        "code_changes": code_changes,
        "implementation_note": impl_note,
        "token_used": state.get("token_used", 0) +
        budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_review(state: DevWorkflowState) -> dict[str, Any]:
    """审查者节点：独立上下文，四维审查"""
    sid = state["session_id"]
    review_round = state.get("review_round", 0) + 1

    angle = "逻辑正确性 + 边界条件与安全" if review_round == 1 else "代码规范 + 整体合理性 + 安全复查"
    await push_progress(sid, {"event": "progress", "stage": "review",
                              "message": f"代码审查中 (第{review_round}轮, 侧重{angle})..."})

    system_prompt = load_system_prompt("reviewer") + (
        f"\n\n当前是第 {review_round} 轮审查。本轮的审查侧重: {angle}。"
    )
    rag_ctx = await inject_rag_context("reviewer",
                                       (state["requirement_doc"] or "") + " " +
                                       (state["implementation_note"] or ""), state["project_path"])

    code_changes_str = json.dumps(state.get("code_changes", {}), ensure_ascii=False, indent=2)

    user_content = (
        f"## 需求概括文档\n{state['requirement_doc']}\n\n"
        f"## 代码变更\n```json\n{code_changes_str}\n```\n\n"
        f"## 实现说明\n{state.get('implementation_note', '(无)')}\n\n"
        f"{rag_ctx}\n\n"
        f"请进行第{review_round}轮审查。关注维度: {angle}。"
        f"请在审查报告开头明确标注结论: [审查通过] 或 [审查不通过]。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = TokenBudget()

    try:
        result = await run_fc_loop(messages, PLAN_MODE_TOOLS, budget, "plan",
                                   state["project_path"])
    except BudgetExceededError:
        result = "# 审查报告\n\n[审查不通过]\n\n（预算超限，审查未完成）\n"

    review_passed = "[审查通过]" in result and "[审查不通过]" not in result

    async with Database() as db:
        await db.add_artifact(sid, "review", result)

    from backend.fallback.engine import after_review
    from backend.trace import record_fallback_decision
    await record_fallback_decision(after_review(
        review_passed, review_round, 1, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    ), "review")

    logger.info(f"[{sid}] 审查完成 (第{review_round}轮, {'通过' if review_passed else '不通过'})")
    return {
        "review_report": result,
        "review_passed": review_passed,
        "review_round": review_round,
        "token_used": state.get("token_used", 0) +
        budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_validate(state: DevWorkflowState) -> dict[str, Any]:
    """在审查前执行用户确认的白名单验证命令。"""
    sid = state["session_id"]
    from backend.intent.models import CodingIntent
    from backend.tools.shell_tools import execute_shell
    from backend.trace import record_event

    intent = CodingIntent.model_validate(state["coding_intent"])
    commands = intent.validation_commands
    if not commands:
        await record_event("validation_result", "validation", {"passed": True, "skipped": True})
        from backend.fallback.engine import after_validation
        from backend.trace import record_fallback_decision
        await record_fallback_decision(after_validation(True, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS), "validate")
        return {"validation_passed": True}
    await push_progress(sid, {"event": "progress", "stage": "validate", "message": "执行验证命令中..."})
    results = [await execute_shell(command, cwd=state["project_path"]) for command in commands]
    passed = all("[exit_code: 0]" in result for result in results)
    await record_event("validation_result", "validation", {
        "passed": passed, "commands": commands, "results": [result[:1000] for result in results],
    })
    from backend.fallback.engine import after_validation
    from backend.trace import record_fallback_decision
    await record_fallback_decision(after_validation(passed, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS), "validate")
    return {"validation_passed": passed}


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
    budget = TokenBudget()

    try:
        result_text = await run_fc_loop(messages, BUILD_MODE_TOOLS, budget, "build",
                                        state["project_path"])
    except BudgetExceededError:
        result_text = "## 代码变更\n\n（预算超限，修复未完成）\n\n## 实现说明\n\n修复被中断。\n"

    code_changes, impl_note = _parse_developer_output(result_text)

    logger.info(f"[{sid}] 修复完成 (第{fix_attempt}次)")
    return {
        "code_changes": code_changes,
        "implementation_note": impl_note,
        "fix_attempt": fix_attempt,
        "token_used": state.get("token_used", 0) +
        budget.total_input_tokens + budget.total_output_tokens,
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
    return {"intervention_decision": decision, "status": "running"}


async def node_output_summary(state: DevWorkflowState) -> dict[str, Any]:
    """生成总结：写入长期记忆 + 触发RAG增量更新"""
    sid = state["session_id"]

    code_changes = state.get("code_changes", {}) or {}
    file_count = len(code_changes)
    review_rounds = state.get("review_round", 0)
    total_tokens = state.get("token_used", 0)

    summary = (
        f"# 开发完成\n\n"
        f"- 变更文件数: {file_count}\n"
        f"- 审查轮次: {review_rounds}\n"
        f"- 总Token消耗: {total_tokens}\n"
        f"- 最终状态: {'审查通过' if state.get('review_passed') else '人工通过'}\n\n"
        f"## 变更文件\n"
        + "\n".join(f"- {fp}" for fp in code_changes.keys())
    )

    async with Database() as db:
        await db.add_artifact(sid, "output_summary", summary)

        # 写入长期记忆
        from backend.rag.indexer import CodeIndexer
        indexer = CodeIndexer()
        await indexer.index_long_term_memory(
            category="task",
            title=f"DEV任务 - {state.get('description', '')[:50]}",
            content=summary,
            source_session_id=sid,
        )

        # 增量更新RAG索引
        if code_changes:
            changed_files = [
                f"{state['project_path']}/{fp}" if state['project_path'] else fp
                for fp in code_changes.keys()
            ]
            try:
                await indexer.incremental_update(changed_files)
            except Exception as exc:
                logger.warning(f"增量RAG更新失败: {exc}")

    await push_progress(sid, {"event": "complete", "stage": "output",
                              "message": "工作流完成", "summary": summary})

    logger.info(f"[{sid}] 工作流完成总结: {file_count}个文件变更")
    return {"status": "done"}


# ---- 条件路由 ----

def route_after_validation(state: DevWorkflowState) -> Literal["review", "fix", "human_intervene"]:
    from backend.fallback.engine import after_validation
    decision = after_validation(bool(state.get("validation_passed")), state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS)
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


def route_after_intervention(state: DevWorkflowState) -> Literal["fix", "output"]:
    from backend.fallback.engine import after_intervention
    return after_intervention(state.get("intervention_decision")).next_stage or "output"


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
    workflow.add_node("review", node_review)
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
    workflow.add_edge("develop_build", "validate")
    workflow.add_conditional_edges("validate", route_after_validation, {
        "review": "review", "fix": "fix", "human_intervene": "human_intervene",
    })

    # 审查后的条件分支
    workflow.add_conditional_edges("review", route_after_review, {
        "fix": "fix",
        "human_accept": "human_accept",
        "review": "review",
        "human_intervene": "human_intervene",
    })
    workflow.add_edge("fix", "validate")

    # 人工验收后的条件分支
    workflow.add_conditional_edges("human_accept", route_after_human, {
        "fix": "fix",
        "output": "output",
        "human_intervene": "human_intervene",
    })
    workflow.add_conditional_edges("human_intervene", route_after_intervention, {
        "fix": "fix", "output": "output",
    })
    workflow.add_edge("output", END)

    return workflow.compile(checkpointer=MemorySaver())


# ---- 辅助函数 ----

def _parse_developer_output(text: str) -> tuple[dict[str, str], str]:
    """从开发者输出文本中解析代码变更和实现说明"""
    code_changes: dict[str, str] = {}
    impl_note = text

    if "## 代码变更" in text:
        parts = text.split("## 代码变更", 1)
        if len(parts) > 1:
            changes_section = parts[1]
            if "## 实现说明" in changes_section:
                changes_text, impl_section = changes_section.split("## 实现说明", 1)
                impl_note = impl_section.strip()
            else:
                changes_text = changes_section
            code_changes = {"output.txt": changes_text.strip()}
    elif "## 实现说明" in text:
        code_changes = {"output.txt": text}
    else:
        code_changes = {"output.txt": text}

    return code_changes, impl_note


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
