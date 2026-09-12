"""集中管理 DEV/DEBUG 的稳定路由与降级策略。"""

from __future__ import annotations

from backend.fallback.models import FallbackDecision


def after_validation(validation_passed: bool, fix_attempt: int, max_attempts: int) -> FallbackDecision:
    if validation_passed:
        return FallbackDecision(
            action="continue", reason_code="validation_passed", reason="验证命令均已通过",
            next_stage="review", input_snapshot={"fix_attempt": fix_attempt},
        )
    if fix_attempt < max_attempts:
        return FallbackDecision(
            action="fix", reason_code="validation_failed", reason="验证命令失败，进入受限修复",
            next_stage="fix", retryable=True, input_snapshot={"fix_attempt": fix_attempt},
        )
    return _human_intervene("validation_retry_exhausted", "验证持续失败，已达到自动修复上限", fix_attempt)


def after_build(successful_writes: int, build_attempt: int, max_attempts: int) -> FallbackDecision:
    """Build 产出门禁：没有真实写入时不得进入验证。"""
    if successful_writes > 0:
        return FallbackDecision(
            action="continue", reason_code="build_writes_present", reason="Build 已产生实际文件写入",
            next_stage="validate", input_snapshot={"successful_writes": successful_writes},
        )
    if build_attempt < max_attempts:
        return FallbackDecision(
            action="retry", reason_code="build_no_write", reason="Build 未产生任何成功的 write_file 结果",
            next_stage="develop_build", retryable=True,
            input_snapshot={"build_attempt": build_attempt},
        )
    return _human_intervene(
        "build_no_write_retry_exhausted", "Build 连续未产生实际文件写入，已达到重试上限", build_attempt,
        {"successful_writes": successful_writes},
    )


def after_review(
    review_passed: bool,
    review_round: int,
    required_rounds: int,
    fix_attempt: int,
    max_attempts: int,
) -> FallbackDecision:
    snapshot = {"review_round": review_round, "required_rounds": required_rounds, "fix_attempt": fix_attempt}
    if review_round < required_rounds:
        return FallbackDecision(
            action="continue", reason_code="review_round_required", reason="尚未完成最小审查轮次",
            next_stage="review", input_snapshot=snapshot,
        )
    if review_passed:
        return FallbackDecision(
            action="human_accept", reason_code="review_passed", reason="审查通过，等待人工验收",
            next_stage="human_accept", input_snapshot=snapshot,
        )
    if fix_attempt < max_attempts:
        return FallbackDecision(
            action="fix", reason_code="review_rejected", reason="审查未通过，进入修复循环",
            next_stage="fix", retryable=True, input_snapshot=snapshot,
        )
    return _human_intervene("review_retry_exhausted", "审查未通过且已达到自动修复上限", fix_attempt, snapshot)


def after_diagnosis(evidence_sufficient: bool) -> FallbackDecision:
    if evidence_sufficient:
        return FallbackDecision(
            action="fix", reason_code="diagnosis_sufficient", reason="诊断证据充足，进入修复",
            next_stage="fix",
        )
    return FallbackDecision(
        action="add_logging", reason_code="diagnosis_insufficient", reason="诊断证据不足，添加日志并等待复现",
        next_stage="add_logging", retryable=True,
    )


def after_human_acceptance(decision: str | None, fix_attempt: int, max_attempts: int) -> FallbackDecision:
    if decision == "approved":
        return FallbackDecision(
            action="continue", reason_code="human_approved", reason="人工验收通过，允许交付",
            next_stage="output", input_snapshot={"fix_attempt": fix_attempt},
        )
    if fix_attempt < max_attempts:
        return FallbackDecision(
            action="fix", reason_code="human_rejected", reason="人工验收驳回，进入修复",
            next_stage="fix", retryable=True, input_snapshot={"fix_attempt": fix_attempt},
        )
    return _human_intervene("human_rejected_retry_exhausted", "人工验收驳回且自动修复次数已用尽", fix_attempt)


def after_intervention(decision: str | None) -> FallbackDecision:
    if decision == "retry":
        return FallbackDecision(
            action="fix", reason_code="human_retry_authorized", reason="人工授权后重新进入修复",
            next_stage="fix", retryable=True,
        )
    return FallbackDecision(
        action="stop", reason_code="human_stopped", reason="人工终止自动化流程",
        next_stage="output",
    )


def budget_exceeded() -> FallbackDecision:
    return FallbackDecision(
        action="stop", reason_code="budget_exceeded", reason="Token 预算超限，停止自动化执行并保留产物",
        next_stage="output",
    )


def _human_intervene(
    reason_code: str,
    reason: str,
    fix_attempt: int,
    snapshot: dict[str, object] | None = None,
) -> FallbackDecision:
    details = {"fix_attempt": fix_attempt}
    if snapshot:
        details.update(snapshot)
    return FallbackDecision(
        action="human_intervene", reason_code=reason_code, reason=reason,
        next_stage="human_intervene", input_snapshot=details,
    )
