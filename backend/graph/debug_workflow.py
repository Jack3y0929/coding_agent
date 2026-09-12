"""DEBUG BUG修复工作流状态图"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, TypedDict

from langgraph.graph import StateGraph, END

from backend.budget import BudgetExceededError, get_workflow_budget
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
from backend.graph.review import aggregate_review_reports, run_review_perspective
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
    diagnosis_evidence: dict[str, Any]
    new_logs: str | None
    code_changes: dict[str, str] | None
    fix_execution: dict[str, Any] | None
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
    session_id: str
    project_path: str
    description: str
    workflow_type: str
    document_id: str


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
        f"\n\n报告末尾必须包含 debugger Prompt 规定的诊断判定数据 JSON。"
        f"只有数据校验条件满足时才标注 [论据充足]，否则标注 [论据不足]。"
    )

    messages = build_node_messages(system_prompt, user_content)
    budget = get_workflow_budget()

    try:
        result = await run_fc_loop(messages, PLAN_MODE_TOOLS, budget, "plan",
                                   state["project_path"])
    except BudgetExceededError:
        result = "# 问题分析报告\n\n（预算超限，诊断未完成）\n\n[论据不足]\n"

    diagnosis_sufficient, diagnosis_evidence = _evaluate_diagnosis_evidence(result)

    from backend.artifacts import write_diagnosis
    diagnosis_path = write_diagnosis(state["document_id"], state["project_path"], result)
    structured_content = open(diagnosis_path, encoding="utf-8").read()
    async with Database() as db:
        await db.add_artifact(sid, "diagnosis", structured_content)

    from backend.fallback.engine import after_diagnosis
    from backend.trace import record_event, record_fallback_decision
    await record_event("diagnosis_evidence_check", "diagnose", diagnosis_evidence)
    await record_fallback_decision(after_diagnosis(diagnosis_sufficient), "diagnose")

    logger.info(
        "[%s] BUG诊断完成 (数据校验%s，证据%d条)",
        sid,
        "通过" if diagnosis_sufficient else "未通过",
        diagnosis_evidence["evidence_count"],
    )
    return {
        "diagnosis_report": result,
        "diagnosis_sufficient": diagnosis_sufficient,
        "diagnosis_evidence": diagnosis_evidence,
        "token_used": budget.total_input_tokens + budget.total_output_tokens,
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
    budget = get_workflow_budget()

    try:
        await run_fc_loop(messages, BUILD_MODE_TOOLS, budget, "build",
                          state["project_path"])
    except BudgetExceededError:
        pass

    logger.info(f"[{sid}] 日志打点已添加")
    return {
        "token_used": budget.total_input_tokens + budget.total_output_tokens,
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
    budget = get_workflow_budget()

    execution_report: dict[str, Any] = {"successful_writes": [], "failed_writes": [], "tool_calls": []}
    try:
        result_text = await run_fc_loop(messages, BUILD_MODE_TOOLS, budget, "build",
                                        state["project_path"], execution_report)
    except BudgetExceededError:
        result_text = "## 代码变更\n\n（预算超限，修复未完成）\n\n## 实现说明\n\n修复被中断。\n"

    code_changes = dict(state.get("code_changes") or {})
    code_changes.update(_changes_from_execution(execution_report))
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

    async with Database() as db:
        await db.add_artifact(sid, "code_changes",
                              json.dumps(code_changes, ensure_ascii=False))

    logger.info(f"[{sid}] BUG修复完成 (第{fix_attempt}次)")
    return {
        "code_changes": code_changes,
        "fix_execution": execution_report,
        "implementation_note": impl_note,
        "fix_attempt": fix_attempt,
        "token_used": budget.total_input_tokens + budget.total_output_tokens,
    }


async def node_review_start(state: DebugWorkflowState) -> dict[str, Any]:
    """启动同一轮的三路独立审查。"""
    sid = state["session_id"]
    review_round = state.get("review_round", 0) + 1
    await push_progress(sid, {"event": "progress", "stage": "review",
                              "message": f"启动多视角代码审查（第{review_round}轮）..."})
    return {}



async def node_review_logic(state: DebugWorkflowState) -> dict[str, Any]:
    """核对修复是否覆盖根因、逻辑和回归风险。"""
    return await run_review_perspective(state, "logic", "debug")


async def node_review_security(state: DebugWorkflowState) -> dict[str, Any]:
    """核对修复是否引入安全和边界问题。"""
    return await run_review_perspective(state, "security", "debug")


async def node_review_quality(state: DebugWorkflowState) -> dict[str, Any]:
    """核对修复后的工程质量、测试和可维护性。"""
    return await run_review_perspective(state, "quality", "debug")


async def node_review_aggregate(state: DebugWorkflowState) -> dict[str, Any]:
    """等待三路审查完成，确定性汇总并产生唯一的路由结论。"""
    sid = state["session_id"]
    aggregate = aggregate_review_reports(state, "debug", required_rounds=REVIEW_ROUNDS_DEBUG)
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
        review_passed, review_round, REVIEW_ROUNDS_DEBUG,
        state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS,
    ), "review_aggregate")
    logger.info(f"[{sid}] 多视角审查完成 (第{review_round}轮, {'通过' if review_passed else '不通过'})")
    return aggregate


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
    execution = state.get("fix_execution") or {}
    if not execution.get("successful_writes"):
        checks = {
            "passed": False,
            "errors": ["代码变更为空，FIX 未产生 write_file 结果"],
            "successful_writes": [],
        }
        await record_event("validation_result", "validation", checks)
        await record_fallback_decision(after_validation(False, state.get("fix_attempt", 0), MAX_FIX_ATTEMPTS), "validate")
        return {"validation_passed": False}
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
    return {
        "intervention_decision": decision,
        "fix_attempt": 0 if decision == "retry" else state.get("fix_attempt", 0),
        "status": "running",
    }


async def node_output_summary(state: DebugWorkflowState) -> dict[str, Any]:
    """生成总结"""
    sid = state["session_id"]

    code_changes = state.get("code_changes", {}) or {}
    file_count = len(code_changes)
    review_rounds = state.get("review_round", 0)
    total_tokens = state.get("token_used", 0)
    summary_title = "# BUG修复完成" if code_changes else "# 自动化任务已停止"

    diagnosis_sufficient = state.get("diagnosis_sufficient", False)

    summary = (
        f"{summary_title}\n\n"
        f"- 变更文件数: {file_count}\n"
        f"- 审查轮次: {review_rounds}\n"
        f"- 总Token消耗: {total_tokens}\n"
        f"- 诊断状态: {'数据校验通过' if diagnosis_sufficient else '需人工补充日志'}\n"
        f"- 可追溯证据数: {state.get('diagnosis_evidence', {}).get('evidence_count', 0)}\n"
        f"- 最终状态: {'审查通过' if state.get('review_passed') else '人工通过'}\n\n"
        f"## 变更文件\n"
        + "\n".join(f"- {fp}" for fp in code_changes.keys())
    )

    async with Database() as db:
        await db.add_artifact(sid, "output_summary", summary)

    from backend.artifacts import write_feedback
    write_feedback(state["document_id"], state["project_path"], summary)

    # 写入长期记忆
    from backend.rag.indexer import CodeIndexer
    indexer = CodeIndexer()
    await indexer.index_long_term_memory_entries([
            {
                "category": "error",
                "title": f"BUG修复原因 - {state.get('description', '')[:50]}",
                "content": state.get("diagnosis_report") or "",
            },
            {
                "category": "error",
                "title": f"BUG修复总结 - {state.get('description', '')[:50]}",
                "content": summary,
            },
            {
                "category": "error",
                "title": f"BUG实现说明 - {state.get('description', '')[:50]}",
                "content": state.get("implementation_note") or "",
            },
    ], source_session_id=sid, project_path=state.get("project_path", ""))

    if code_changes:
        changed_files = [
                f"{state['project_path']}/{fp}" if state['project_path'] else fp
                for fp in code_changes.keys()
        ]
        try:
            await indexer.incremental_update(changed_files, project_path=state.get("project_path", ""))
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


def route_after_fix(state: DebugWorkflowState) -> Literal["validate", "fix", "human_intervene"]:
    """FIX 未产生真实写入时，禁止进入验证节点。"""
    from backend.fallback.engine import after_build
    execution = state.get("fix_execution") or {}
    decision = after_build(
        len(execution.get("successful_writes") or []),
        state.get("fix_attempt", 0),
        MAX_FIX_ATTEMPTS,
    )
    return "fix" if decision.next_stage == "develop_build" else (decision.next_stage or "human_intervene")


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
    workflow.add_node("review_start", node_review_start)
    workflow.add_node("review_logic", node_review_logic)
    workflow.add_node("review_security", node_review_security)
    workflow.add_node("review_quality", node_review_quality)
    workflow.add_node("review_aggregate", node_review_aggregate)
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
    workflow.add_conditional_edges("fix", route_after_fix, {
        "validate": "validate", "fix": "fix", "human_intervene": "human_intervene",
    })
    workflow.add_conditional_edges("validate", route_after_validation, {
        "review": "review_start", "fix": "fix", "human_intervene": "human_intervene",
    })

    # 同一轮的三个审查视角并行执行，汇总节点等待全部结果。
    workflow.add_edge("review_start", "review_logic")
    workflow.add_edge("review_start", "review_security")
    workflow.add_edge("review_start", "review_quality")
    workflow.add_edge(["review_logic", "review_security", "review_quality"], "review_aggregate")

    # 汇总审查后条件分支：继续审查/修复/验收
    workflow.add_conditional_edges("review_aggregate", route_after_review, {
        "review": "review_start",
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

_DIAGNOSIS_EVIDENCE_PATTERN = re.compile(
    r"##\s*诊断判定数据\s*\n(?P<fence>[\x60~]{3})json\s*(?P<body>.*?)\s*(?P=fence)",
    re.DOTALL,
)
_EVIDENCE_SOURCES = {"code", "log", "runtime", "test", "history"}
_MIN_DIAGNOSIS_CONFIDENCE = 0.75
_MIN_DIAGNOSIS_EVIDENCE_COUNT = 2


def _evaluate_diagnosis_evidence(report: str) -> tuple[bool, dict[str, Any]]:
    """解析并校验诊断数据，避免仅凭模型文本标签进入修复。"""
    checks_failed: list[str] = []
    match = _DIAGNOSIS_EVIDENCE_PATTERN.search(report)
    if not match:
        return False, {
            "passed": False,
            "evidence_count": 0,
            "evidence_sources": [],
            "failed_checks": ["missing_diagnosis_evidence_json"],
        }

    try:
        payload = json.loads(match.group("body"))
    except json.JSONDecodeError:
        return False, {
            "passed": False,
            "evidence_count": 0,
            "evidence_sources": [],
            "failed_checks": ["invalid_diagnosis_evidence_json"],
        }

    if not isinstance(payload, dict):
        return False, {
            "passed": False,
            "evidence_count": 0,
            "evidence_sources": [],
            "failed_checks": ["diagnosis_evidence_not_object"],
        }

    root_cause = payload.get("root_cause")
    repair_direction = payload.get("repair_direction")
    confidence = payload.get("confidence")
    missing_information = payload.get("missing_information")
    evidence = payload.get("evidence")
    if not isinstance(root_cause, str) or not root_cause.strip():
        checks_failed.append("missing_root_cause")
    if not isinstance(repair_direction, str) or not repair_direction.strip():
        checks_failed.append("missing_repair_direction")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        checks_failed.append("invalid_confidence")
    elif not 0 <= float(confidence) <= 1:
        checks_failed.append("confidence_out_of_range")
    elif float(confidence) < _MIN_DIAGNOSIS_CONFIDENCE:
        checks_failed.append("confidence_below_threshold")
    if not isinstance(missing_information, list) or any(
        not isinstance(item, str) for item in missing_information
    ):
        checks_failed.append("invalid_missing_information")
    elif missing_information:
        checks_failed.append("missing_information_present")
    if not isinstance(evidence, list):
        evidence = []
        checks_failed.append("invalid_evidence")

    evidence_sources: list[str] = []
    valid_evidence_count = 0
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        reference = item.get("reference")
        detail = item.get("detail")
        if (
            isinstance(source, str)
            and source in _EVIDENCE_SOURCES
            and isinstance(reference, str)
            and reference.strip()
            and isinstance(detail, str)
            and detail.strip()
        ):
            valid_evidence_count += 1
            evidence_sources.append(source)
    if valid_evidence_count < _MIN_DIAGNOSIS_EVIDENCE_COUNT:
        checks_failed.append("insufficient_traceable_evidence")
    if "[论据充足]" not in report or "[论据不足]" in report:
        checks_failed.append("inconsistent_evidence_label")

    evidence_data = {
        "passed": not checks_failed,
        "root_cause": root_cause if isinstance(root_cause, str) else "",
        "repair_direction": repair_direction if isinstance(repair_direction, str) else "",
        "confidence": float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else None,
        "evidence_count": valid_evidence_count,
        "evidence_sources": evidence_sources,
        "missing_information": missing_information if isinstance(missing_information, list) else [],
        "failed_checks": checks_failed,
    }
    return not checks_failed, evidence_data

def _parse_implementation_note(text: str) -> str:
    """只提取实现说明；文本本身永远不视为文件变更。"""
    if "## 实现说明" not in text:
        return text.strip()
    return text.split("## 实现说明", 1)[1].strip()


def _changes_from_execution(execution_report: dict[str, Any]) -> dict[str, str]:
    """将成功 write_file 调用转换为正式代码变更，重复路径以后一次为准。"""
    changes: dict[str, str] = {}
    for item in execution_report.get("successful_writes", []):
        path = str(item.get("path", "")).replace("\\", "/").strip()
        if path:
            changes[path] = str(item.get("content", ""))
    return changes


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
