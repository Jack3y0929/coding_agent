"""SWE-bench 运行结果指标。"""

from collections import defaultdict

from backend.evaluation.swebench.models import SWEbenchTrialResult


def summarize(results: list[SWEbenchTrialResult]) -> dict[str, float | int | None]:
    """计算单次和多 trial 指标；缺失判定不按通过处理。"""
    total = len(results)
    completed = [item for item in results if item.status == "completed"]
    resolved = [item for item in results if item.resolved is True]
    grouped: dict[str, list[SWEbenchTrialResult]] = defaultdict(list)
    for item in results:
        grouped[item.instance_id].append(item)
    task_count = len(grouped)
    pass_at_k = sum(any(item.resolved is True for item in items) for items in grouped.values())
    pass_power_k = sum(all(item.resolved is True for item in items) for items in grouped.values())
    return {
        "total_trials": total,
        "total_tasks": task_count,
        "resolved_rate": _ratio(len(resolved), total),
        "pass_at_k": _ratio(pass_at_k, task_count),
        "pass_power_k": _ratio(pass_power_k, task_count),
        "agent_completion_rate": _ratio(len(completed), total),
        "average_duration_ms": (
            round(sum(item.duration_ms for item in results) / total) if total else None
        ),
        "average_input_tokens": (
            round(sum(item.input_tokens for item in results) / total) if total else None
        ),
        "average_output_tokens": (
            round(sum(item.output_tokens for item in results) / total) if total else None
        ),
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None
