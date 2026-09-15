"""FeatureBench trial 指标汇总。"""

from collections import defaultdict

from backend.evaluation.featurebench.models import FeatureBenchTrialResult


def summarize(results: list[FeatureBenchTrialResult]) -> dict[str, float | int | None]:
    total = len(results)
    grouped: dict[str, list[FeatureBenchTrialResult]] = defaultdict(list)
    for item in results:
        grouped[item.instance_id].append(item)
    tasks = len(grouped)
    return {
        "total_trials": total,
        "total_tasks": tasks,
        "passed_rate": _ratio(sum(item.passed is True for item in results), total),
        "resolved_rate": _ratio(sum(item.resolved is True for item in results), total),
        "patch_valid_rate": _ratio(sum(item.patch_path is not None for item in results), total),
        "agent_completion_rate": _ratio(sum(item.status == "completed" for item in results), total),
        "pass_at_k": _ratio(sum(any(item.passed is True for item in values) for values in grouped.values()), tasks),
        "pass_power_k": _ratio(sum(all(item.passed is True for item in values) for values in grouped.values()), tasks),
        "average_duration_ms": round(sum(item.duration_ms for item in results) / total) if total else None,
        "average_input_tokens": round(sum(item.input_tokens for item in results) / total) if total else None,
        "average_output_tokens": round(sum(item.output_tokens for item in results) / total) if total else None,
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None
