"""DEBUG BUG修复工作流状态图"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, TypedDict

from langgraph.graph import StateGraph, END

from backend.budget import BudgetExceededError, TokenBudget
from backend.config import MAX_FIX_ATTEMPTS, REVIEW_ROUNDS_DEBUG
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

logger = logging.getLogger("graph.debug")


# ---- State 定义 ----

class DebugWorkflowState(TypedDict):
    stage: str
    coding_intent: dict[str, Any]
    slot_validation: dict[str, Any]
    manual_workflow_type: str | None
    clarification_doc: str | None
    diagnosis_report: str | None
    diagnosis_sufficient: bool
    new_logs: str | None
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

async def node_input_gate(state: DebugWorkflowState) -> dict[str, Any]:
    """澄清节点：产出包含BUG描述的澄清文档"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "input_gate",
                              "message": "需求澄清中..."})

    intent, validation = await _resolve_intent_slots(state, sid)

    clarification = (
        f"# BUG澄清文档\n\n"
        f"## 用户原始描述\n{state['description']}\n\n"
        f"## 目标项目\n项目路径: {state['project_path']}\n\n"
        f"## 已确认信息\n"
        f"- 工作流类型: BUG修复 (DEBUG)\n"
        f"- 目标模块: {', '.join(intent.target_modules or intent.change_scope) or '未确认'}\n"
        f"- 实际表现: {intent.observed_behavior or '未确认'}\n"
        f"- 复现信息: {'；'.join(intent.reproduction_steps) or '未确认'}\n"
    )

    async with Database() as db:
        await db.add_artifact(sid, "clarification", clarification)

    logger.info(f"[{sid}] 澄清文档已生成")
    return {
        "clarification_doc": clarification,
        "coding_intent": intent.model_dump(),
        "slot_validation": validation.model_dump(),
        "stage": "diagnose",
    }


async def node_diagnose(state: DebugWorkflowState) -> dict[str, Any]:
    """侦探型调试节点：系统性分析，定位BUG根因"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "diagnose",
                              "message": "BUG诊断分析中..."})

    system_prompt = load_system_prompt("debugger")
    rag_ctx = await inject_rag_context("debugger", state["clarification_doc"] or "", state["project_path"])

    new_logs_info = ""
    if state.get("new_logs"):
        new_logs_info = f"\n## 用户复现后提供的新日志\n```\n{state['new_logs']}\n```\n"

    user_content = (
        f"## 已确认研发槽位\n```json\n{json.dumps(state['coding_intent'], ensure_ascii=False, indent=2)}\n```\n\n"
        f"## BUG澄清文档\n{state['clarification_doc']}\n\n"
        f"## 项目路径\n{state['project_path']}\n\n"
        f"{new_logs_info}"
        f"{rag_ctx}\n\n"
        f"请进行系统性BUG诊断。如果能定位根因，输出包含五个部分的结构化问题分析报告。"
        f"如果信息不够，请在报告中明确指出缺少哪些信息，并请求添加日志打点。"
        f"\n\n在报告末尾请用单独一行标注: [论据充足] 或 [论据不足]。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = TokenBudget()

    try:
        result = await run_fc_loop(messages, PLAN_MODE_TOOLS, budget, "plan",
                                   state["project_path"])
    except BudgetExceededError:
        result = "# 问题分析报告\n\n（预算超限，诊断未完成）\n\n[论据不足]\n"

    diagnosis_sufficient = "[论据充足]" in result and "[论据不足]" not in result

    async with Database() as db:
        await db.add_artifact(sid, "diagnosis", result)

    from backend.fallback.engine import after_diagnosis
    from backend.trace import record_fallback_decision
    await record_fallback_decision(after_diagnosis(diagnosis_sufficient), "diagnose")

    logger.info(f"[{sid}] BUG诊断完成 (论据{'充足' if diagnosis_sufficient else '不足'})")
    return {
        "diagnosis_report": result,
        "diagnosis_sufficient": diagnosis_sufficient,
        "token_used": state.get("token_used", 0) +
        budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_add_logging(state: DebugWorkflowState) -> dict[str, Any]:
    """添加日志打点节点：在可疑位置加日志"""
    sid = state["session_id"]
    await push_progress(sid, {"event": "progress", "stage": "add_logging",
                              "message": "添加日志打点代码..."})

    system_prompt = load_system_prompt("debugger") + (
        "\n\n你需要使用 add_logging 工具在关键的代码位置添加日志打点。"
        "只添加诊断日志，不修改任何业务逻辑代码。"
    )
    user_content = (
        f"## 已确认研发槽位\n```json\n{json.dumps(state['coding_intent'], ensure_ascii=False, indent=2)}\n```\n\n"
        f"## 问题分析报告\n{state['diagnosis_report']}\n\n"
        f"## 项目路径\n{state['project_path']}\n\n"
        f"论据不足，请在关键代码位置添加 print 或 logging 打点，"
        f"以帮助下次复现时定位根因。使用 add_logging 工具。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = TokenBudget()

    try:
        await run_fc_loop(messages, BUILD_MODE_TOOLS, budget, "build",
                          state["project_path"])
    except BudgetExceededError:
        pass

    logger.info(f"[{sid}] 日志打点已添加")
    return {
        "token_used": state.get("token_used", 0) +
        budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_human_wait(state: DebugWorkflowState) -> dict[str, Any]:
    """等待用户复现BUG并提供新日志"""
    sid = state["session_id"]
    logger.info(f"[{sid}] 等待用户复现BUG并粘贴新日志")

    decision_result = await wait_for_human(sid, "human_wait")
    new_logs = decision_result.get("new_logs", "")

    logger.info(f"[{sid}] 收到新日志 ({len(new_logs)} 字符)")
    return {"new_logs": new_logs, "status": "running"}


async def node_fix(state: DebugWorkflowState) -> dict[str, Any]:
    """开发者修复节点：根据诊断报告修复BUG"""
    sid = state["session_id"]
    fix_attempt = state.get("fix_attempt", 0) + 1

    await push_progress(sid, {"event": "progress", "stage": "fix",
                              "message": f"修复BUG中 (第{fix_attempt}次)..."})

    system_prompt = load_system_prompt("developer") + (
        "\n\n你是BUG修复开发者。严格根据问题分析报告中的根因定位和修复方向进行修复。"
        "不要改动无关代码，不要引入新的问题。"
        "\n完成后在回复末尾用清晰标记注明代码变更和实现说明。"
    )
    rag_ctx = await inject_rag_context("fix", (state.get("diagnosis_report") or ""), state["project_path"])

    code_changes_str = ""
    if state.get("code_changes"):
        code_changes_str = (
            "## 上次修复的代码变更（参考）\n```json\n" +
            json.dumps(state["code_changes"], ensure_ascii=False, indent=2) + "\n```\n"
        )

    user_content = (
        f"## 问题分析报告\n{state['diagnosis_report']}\n\n"
        f"{code_changes_str}"
        f"## 项目路径\n{state['project_path']}\n\n"
        f"{rag_ctx}\n\n"
        f"请根据问题分析报告，修复上述BUG。完成后用 ## 代码变更 和 ## 实现说明 两部分总结。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = TokenBudget()

    try:
        result_text = await run_fc_loop(messages, BUILD_MODE_TOOLS, budget, "build",
                                        state["project_path"])
    except BudgetExceededError:
        result_text = "## 代码变更\n\n（预算超限，修复未完成）\n\n## 实现说明\n\n修复被中断。\n"

    code_changes, impl_note = _parse_developer_output(result_text)

    async with Database() as db:
        await db.add_artifact(sid, "code_changes",
                              json.dumps(code_changes, ensure_ascii=False))

    logger.info(f"[{sid}] BUG修复完成 (第{fix_attempt}次)")
    return {
        "code_changes": code_changes,
        "implementation_note": impl_note,
        "fix_attempt": fix_attempt,
        "token_used": state.get("token_used", 0) +
        budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_review(state: DebugWorkflowState) -> dict[str, Any]:
    """审查者节点：DEBUG工作流需要至少2轮审查"""
    sid = state["session_id"]
    review_round = state.get("review_round", 0) + 1
    required_rounds = REVIEW_ROUNDS_DEBUG

    if review_round == 1:
        angle = "逻辑正确性 + 边界条件与安全（第1轮）"
    else:
        angle = f"代码规范 + 整体合理性（第{review_round}轮）"

    await push_progress(sid, {"event": "progress", "stage": "review",
                              "message": f"代码审查中 (第{review_round}轮, 侧重{angle})..."})

    system_prompt = load_system_prompt("reviewer") + (
        f"\n\n这是DEBUG工作流的第 {review_round} 轮审查（共需至少{required_rounds}轮）。"
        f"本轮的审查侧重: {angle}。"
    )
    rag_ctx = await inject_rag_context("reviewer",
                                       (state.get("diagnosis_report") or "") + " " +
                                       (state.get("implementation_note") or ""), state["project_path"])

    code_changes_str = json.dumps(state.get("code_changes", {}), ensure_ascii=False, indent=2)

    user_content = (
        f"## 问题分析报告（原始BUG）\n{state['diagnosis_report']}\n\n"
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
        result = f"# 审查报告\n\n[审查不通过]\n\n（预算超限，第{review_round}轮审查未完成）\n"

    review_passed = "[审查通过]" in result and "[审查不通过]" not in result

    async with Database() as db:
        await db.add_artifact(sid, "review", result)

    from backend.fallback.engine import after_review
    from backend.trace import record_fallback_decision
    await record_fallback_decision(after_review(
        review_passed, review_round, required_rounds, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    ), "review")

    logger.info(f"[{sid}] 审查完成 (第{review_round}轮, {'通过' if review_passed else '不通过'})")
    return {
        "review_report": result,
        "review_passed": review_passed,
        "review_round": review_round,
        "token_used": state.get("token_used", 0) +
        budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_human_accept(state: DebugWorkflowState) -> dict[str, Any]:
    """人工验收节点"""
    sid = state["session_id"]
    logger.info(f"[{sid}] 进入人工验收节点")

    decision_result = await wait_for_human(sid, "human_accept")
    human_decision = decision_result.get("decision", "rejected")
    feedback = decision_result.get("feedback", {})
    from backend.trace import add_trace_label, current_trace_id, record_event
    trace_id = current_trace_id()
    if trace_id and feedback.get("outcome"):
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


async def node_validate(state: DebugWorkflowState) -> dict[str, Any]:
    """修复完成后执行用户确认的验证命令。"""
    sid = state["session_id"]
    from backend.intent.models import CodingIntent
    from backend.tools.shell_tools import execute_shell
    from backend.trace import record_event, record_fallback_decision
    from backend.fallback.engine import after_validation

    intent = CodingIntent.model_validate(state["coding_intent"])
    commands = intent.validation_commands
    if not commands:
        await record_event("validation_result", "validation", {"passed": True, "skipped": True})
        await record_fallback_decision(after_validation(True, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS), "validate")
        return {"validation_passed": True}
    await push_progress(sid, {"event": "progress", "stage": "validate", "message": "执行验证命令中..."})
    results = [await execute_shell(command, cwd=state["project_path"]) for command in commands]
    passed = all("[exit_code: 0]" in result for result in results)
    await record_event("validation_result", "validation", {
        "passed": passed, "commands": commands, "results": [result[:1000] for result in results],
    })
    await record_fallback_decision(after_validation(passed, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS), "validate")
    return {"validation_passed": passed}


async def node_human_intervene(state: DebugWorkflowState) -> dict[str, Any]:
    """自动修复上限耗尽时转由人工决定重试或停止。"""
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


async def node_output_summary(state: DebugWorkflowState) -> dict[str, Any]:
    """生成总结"""
    sid = state["session_id"]

    code_changes = state.get("code_changes", {}) or {}
    file_count = len(code_changes)
    review_rounds = state.get("review_round", 0)
    total_tokens = state.get("token_used", 0)

    diagnosis_sufficient = state.get("diagnosis_sufficient", False)

    summary = (
        f"# BUG修复完成\n\n"
        f"- 变更文件数: {file_count}\n"
        f"- 审查轮次: {review_rounds}\n"
        f"- 总Token消耗: {total_tokens}\n"
        f"- 诊断状态: {'论据充足' if diagnosis_sufficient else '需人工补充日志'}\n"
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
            category="error",
            title=f"BUG修复 - {state.get('description', '')[:50]}",
            content=f"{state.get('diagnosis_report', '')}\n\n{summary}",
            source_session_id=sid,
        )

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
                              "message": "BUG修复完成", "summary": summary})

    logger.info(f"[{sid}] 工作流完成: {file_count}个文件变更")
    return {"status": "done"}


# ---- 条件路由 ----

def route_after_diagnose(state: DebugWorkflowState) -> Literal["fix", "add_logging"]:
    """诊断后路由：论据充足→修复，不足→加日志"""
    from backend.fallback.engine import after_diagnosis
    return after_diagnosis(state.get("diagnosis_sufficient", False)).next_stage or "add_logging"


def route_after_validation(state: DebugWorkflowState) -> Literal["review", "fix", "human_intervene"]:
    from backend.fallback.engine import after_validation
    return after_validation(
        bool(state.get("validation_passed")), state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    ).next_stage or "human_intervene"


def route_after_review(state: DebugWorkflowState) -> Literal["fix", "human_accept", "review", "human_intervene"]:
    """审查后路由：DEBUG需要至少2轮审查"""
    from backend.fallback.engine import after_review
    return after_review(
        state.get("review_passed", False), state.get("review_round", 0), REVIEW_ROUNDS_DEBUG,
        state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    ).next_stage or "human_intervene"


def route_after_human(state: DebugWorkflowState) -> Literal["fix", "output", "human_intervene"]:
    from backend.fallback.engine import after_human_acceptance
    return after_human_acceptance(
        state.get("human_decision"), state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    ).next_stage or "human_intervene"


def route_after_intervention(state: DebugWorkflowState) -> Literal["fix", "output"]:
    from backend.fallback.engine import after_intervention
    return after_intervention(state.get("intervention_decision")).next_stage or "output"


# ---- 构建状态图 ----

async def build_debug_workflow() -> StateGraph:
    """构建并编译 DEBUG 工作流状态图"""
    from langgraph.checkpoint.memory import MemorySaver

    try:
        await init_spec_library()
    except Exception as exc:
        logger.warning(f"规范库初始化失败: {exc}")

    workflow = StateGraph(DebugWorkflowState)

    # 添加节点
    workflow.add_node("input_gate", node_input_gate)
    workflow.add_node("diagnose", node_diagnose)
    workflow.add_node("add_logging", node_add_logging)
    workflow.add_node("human_wait", node_human_wait)
    workflow.add_node("fix", node_fix)
    workflow.add_node("validate", node_validate)
    workflow.add_node("review", node_review)
    workflow.add_node("human_accept", node_human_accept)
    workflow.add_node("human_intervene", node_human_intervene)
    workflow.add_node("output", node_output_summary)

    # 设置入口
    workflow.set_entry_point("input_gate")
    workflow.add_edge("input_gate", "diagnose")

    # 诊断后条件分支
    workflow.add_conditional_edges("diagnose", route_after_diagnose, {
        "fix": "fix",
        "add_logging": "add_logging",
    })
    workflow.add_edge("add_logging", "human_wait")
    workflow.add_edge("human_wait", "diagnose")  # 用户提供新日志后重新诊断

    # 修复→审查
    workflow.add_edge("fix", "validate")
    workflow.add_conditional_edges("validate", route_after_validation, {
        "review": "review", "fix": "fix", "human_intervene": "human_intervene",
    })

    # 审查后条件分支：继续审查/修复/验收
    workflow.add_conditional_edges("review", route_after_review, {
        "review": "review",
        "fix": "fix",
        "human_accept": "human_accept",
        "human_intervene": "human_intervene",
    })

    # 人工验收后条件分支
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
    state: DebugWorkflowState,
    session_id: str,
) -> tuple[Any, Any]:
    """DEBUG 在缺少模块、异常表现或复现信息时，不进入诊断和修复。"""
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
