"""多视角代码审查的执行与确定性汇总。"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.budget import BudgetExceededError, TokenBudget
from backend.graph.nodes import PLAN_MODE_TOOLS, build_node_messages, inject_rag_context, load_system_prompt, run_fc_loop


_PERSPECTIVES = {
    "logic": {
        "role": "review_logic",
        "title": "逻辑与需求符合性",
        "focus": "需求符合性、业务逻辑、状态流转、边界条件、并发与回归风险",
    },
    "security": {
        "role": "review_security",
        "title": "安全与边界防护",
        "focus": "输入校验、权限边界、路径与命令安全、敏感信息、资源释放与异常处理",
    },
    "quality": {
        "role": "review_quality",
        "title": "工程质量与可维护性",
        "focus": "代码规范、类型与异常处理、测试覆盖、性能、依赖和可维护性",
    },
}

_REVIEW_DATA_PATTERN = re.compile(
    r"##\s*审查判定数据\s*\n(?P<fence>[`~]{3})json\s*(?P<body>.*?)\s*(?P=fence)",
    re.DOTALL | re.IGNORECASE,
)
_SEVERITIES = {"critical", "high", "medium", "low"}


def perspective_state_keys(perspective: str) -> tuple[str, str, str]:
    """返回某一审查视角对应的报告、判定和 token State 字段。"""
    return (
        f"review_{perspective}_report",
        f"review_{perspective}_result",
        f"review_{perspective}_tokens",
    )


async def run_review_perspective(
    state: dict[str, Any],
    perspective: str,
    workflow_type: str,
) -> dict[str, Any]:
    """以独立 Prompt、独立上下文执行一个审查视角。"""
    if perspective not in _PERSPECTIVES:
        raise ValueError(f"未知审查视角: {perspective}")

    from backend.graph.context import push_progress
    from backend.trace import record_event

    config = _PERSPECTIVES[perspective]
    sid = state["session_id"]
    review_round = state.get("review_round", 0) + 1
    report_key, result_key, token_key = perspective_state_keys(perspective)
    await push_progress(sid, {
        "event": "progress",
        "stage": f"review_{perspective}",
        "message": f"{config['title']}审查中（第{review_round}轮）...",
    })
    await record_event("review_started", f"review_{perspective}", {
        "perspective": perspective,
        "review_round": review_round,
        "workflow_type": workflow_type,
    })

    extra_context = ""
    if workflow_type == "debug":
        extra_context = f"## 问题分析报告\n{state.get('diagnosis_report', '(无)')}\n\n"
    requirement_doc = state.get("requirement_doc") or state.get("clarification_doc") or ""
    implementation_note = state.get("implementation_note") or ""
    rag_query = f"{config['focus']} {requirement_doc} {implementation_note}"
    rag_context = await inject_rag_context(config["role"], rag_query, state["project_path"])
    code_changes = json.dumps(state.get("code_changes", {}), ensure_ascii=False, indent=2)
    system_prompt = load_system_prompt(config["role"]) + (
        f"\n\n当前为 {workflow_type.upper()} 工作流第 {review_round} 轮审查。"
        f"本视角唯一重点：{config['focus']}。"
    )
    user_content = (
        f"## 需求概括文档\n{requirement_doc}\n\n"
        f"{extra_context}"
        f"## 代码变更\n```json\n{code_changes}\n```\n\n"
        f"## 实现说明\n{implementation_note or '(无)'}\n\n"
        f"{rag_context}\n\n"
        "只按本视角审查。可使用只读工具核实变更。"
        "报告开头必须标注 [审查通过] 或 [审查不通过]，并在末尾按 Prompt 输出审查判定数据 JSON。"
    )

    budget = TokenBudget()
    try:
        report = await run_fc_loop(
            build_node_messages(system_prompt, user_content),
            PLAN_MODE_TOOLS,
            budget,
            "plan",
            state["project_path"],
        )
    except BudgetExceededError:
        report = _failed_review_report(perspective, "预算超限，审查未完成")
    except Exception as exc:
        report = _failed_review_report(perspective, f"审查执行失败: {exc}")

    result = parse_review_report(report, perspective)
    await record_event("review_finished", f"review_{perspective}", {
        "perspective": perspective,
        "review_round": review_round,
        "passed": result["passed"],
        "finding_count": len(result["findings"]),
        "parse_errors": result["parse_errors"],
        "tokens": budget.total_input_tokens + budget.total_output_tokens,
    })
    return {report_key: report, result_key: result, token_key: budget.total_input_tokens + budget.total_output_tokens}


def parse_review_report(report: str, perspective: str) -> dict[str, Any]:
    """校验审查输出；格式错误按未通过处理，避免审查静默放行。"""
    errors: list[str] = []
    match = _REVIEW_DATA_PATTERN.search(report)
    payload: Any = None
    if not match:
        errors.append("missing_review_data_json")
    else:
        try:
            payload = json.loads(match.group("body"))
        except json.JSONDecodeError:
            errors.append("invalid_review_data_json")
    if not isinstance(payload, dict):
        payload = {}
        if not errors:
            errors.append("review_data_not_object")

    findings = _normalize_findings(payload.get("findings"), perspective, errors)
    passed_value = payload.get("passed")
    if not isinstance(passed_value, bool):
        errors.append("invalid_passed")
    has_pass_marker = "[审查通过]" in report and "[审查不通过]" not in report
    has_reject_marker = "[审查不通过]" in report
    if not has_pass_marker and not has_reject_marker:
        errors.append("missing_review_marker")
    if isinstance(passed_value, bool) and passed_value != has_pass_marker:
        errors.append("marker_and_json_disagree")
    if any(item["severity"] in {"critical", "high"} for item in findings) and passed_value is True:
        errors.append("high_severity_finding_marked_passed")

    return {
        "perspective": perspective,
        "passed": bool(passed_value) and has_pass_marker and not errors,
        "findings": findings,
        "parse_errors": errors,
    }


def aggregate_review_reports(state: dict[str, Any], workflow_type: str, required_rounds: int) -> dict[str, Any]:
    """确定性合并三份审查报告，不由汇总节点重新推测新的问题。"""
    perspective_results: list[dict[str, Any]] = []
    perspective_reports: list[tuple[str, str]] = []
    total_tokens = 0
    for perspective, config in _PERSPECTIVES.items():
        report_key, result_key, token_key = perspective_state_keys(perspective)
        report = state.get(report_key) or _failed_review_report(perspective, "未获得审查报告")
        result = state.get(result_key)
        if not isinstance(result, dict):
            result = parse_review_report(report, perspective)
        perspective_results.append(result)
        perspective_reports.append((config["title"], report))
        total_tokens += int(state.get(token_key, 0) or 0)

    findings = _deduplicate_findings(perspective_results)
    review_passed = all(item.get("passed") is True for item in perspective_results)
    review_round = state.get("review_round", 0) + 1
    report = _format_aggregate_report(
        perspective_results,
        perspective_reports,
        findings,
        review_passed,
        review_round,
        workflow_type,
    )
    return {
        "review_report": report,
        "review_findings": findings,
        "review_passed": review_passed,
        "review_round": review_round,
        "review_aggregate_tokens": total_tokens,
    }


def _normalize_findings(raw_findings: Any, perspective: str, errors: list[str]) -> list[dict[str, Any]]:
    if not isinstance(raw_findings, list):
        errors.append("invalid_findings")
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            errors.append(f"invalid_finding_{index}")
            continue
        severity = str(item.get("severity", "")).lower()
        category = item.get("category")
        evidence = item.get("evidence")
        recommendation = item.get("recommendation")
        file_path = item.get("file", "")
        line = item.get("line")
        if severity not in _SEVERITIES or not isinstance(category, str) or not category.strip() or not isinstance(evidence, str) or not evidence.strip() or not isinstance(recommendation, str) or not recommendation.strip():
            errors.append(f"invalid_finding_{index}")
            continue
        if not isinstance(file_path, str):
            file_path = ""
        if isinstance(line, bool) or not isinstance(line, int) or line < 0:
            line = 0
        normalized.append({
            "perspectives": [perspective],
            "severity": severity,
            "category": category.strip(),
            "file": file_path.strip(),
            "line": line,
            "evidence": evidence.strip(),
            "recommendation": recommendation.strip(),
        })
    return normalized


def _deduplicate_findings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, int, str, str], dict[str, Any]] = {}
    for result in results:
        for finding in result.get("findings", []):
            key = (finding["file"], finding["line"], finding["category"], finding["evidence"])
            existing = merged.get(key)
            if existing is None:
                merged[key] = dict(finding)
                continue
            existing["perspectives"] = sorted(set(existing["perspectives"] + finding["perspectives"]))
            if _severity_rank(finding["severity"]) < _severity_rank(existing["severity"]):
                existing["severity"] = finding["severity"]
                existing["recommendation"] = finding["recommendation"]
    return sorted(merged.values(), key=lambda item: (_severity_rank(item["severity"]), item["file"], item["line"]))


def _severity_rank(severity: str) -> int:
    return {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(severity, 4)


def _format_aggregate_report(
    results: list[dict[str, Any]],
    reports: list[tuple[str, str]],
    findings: list[dict[str, Any]],
    passed: bool,
    review_round: int,
    workflow_type: str,
) -> str:
    verdict = "[审查通过]" if passed else "[审查不通过]"
    lines = [
        "# 多视角审查汇总报告",
        "",
        verdict,
        "",
        f"- 工作流: {workflow_type.upper()}",
        f"- 审查轮次: {review_round}",
        f"- 审查视角: {len(results)}",
        f"- 去重后问题数: {len(findings)}",
        "",
        "## 汇总结论",
        "所有视角均通过，允许进入下一流程。" if passed else "至少一个审查视角未通过或输出格式无效，禁止静默放行。",
        "",
        "## 去重问题清单",
    ]
    if findings:
        for index, finding in enumerate(findings, 1):
            location = finding["file"] or "未定位文件"
            if finding["line"]:
                location = f"{location}:{finding['line']}"
            perspectives = ", ".join(finding["perspectives"])
            lines.extend([
                f"{index}. [{finding['severity'].upper()}] {finding['category']} ({location}; 视角: {perspectives})",
                f"   - 证据: {finding['evidence']}",
                f"   - 建议: {finding['recommendation']}",
            ])
    else:
        lines.append("无有效问题条目。")
    lines.append("")
    lines.append("## 各视角原始报告")
    for title, report in reports:
        lines.extend(["", f"### {title}", report])
    return "\n".join(lines)


def _failed_review_report(perspective: str, reason: str) -> str:
    return (
        "# 审查报告\n\n[审查不通过]\n\n"
        f"审查视角 {perspective} 未完成：{reason}\n\n"
        "## 审查判定数据\n```json\n"
        "{\"passed\": false, \"findings\": [{\"severity\": \"high\", "
        "\"category\": \"review_execution\", \"file\": \"\", \"line\": 0, "
        f"\"evidence\": {json.dumps(reason, ensure_ascii=False)}, "
        "\"recommendation\": \"修复审查执行问题后重新审查\"}]}\n```"
    )
