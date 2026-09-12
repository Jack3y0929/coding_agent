"""根据 Trace 事件、人工标签和 Fallback 决策生成可解释评测。"""

from __future__ import annotations

import json
from typing import Any

from backend.db.models import Database, _now


async def evaluate_trace(trace_id: str) -> list[dict[str, Any]]:
    """计算单次任务的确定性指标；不依赖模型主观判断。"""
    async with Database() as db:
        event_rows = await (await db._conn.execute(
            "SELECT event_type, stage, payload FROM trace_events WHERE trace_id = ? ORDER BY id", (trace_id,)
        )).fetchall()
        label = await (await db._conn.execute(
            "SELECT * FROM trace_labels WHERE trace_id = ? ORDER BY id DESC LIMIT 1", (trace_id,)
        )).fetchone()
        await db._conn.execute("DELETE FROM trace_evaluations WHERE trace_id = ?", (trace_id,))

        events = [{**dict(row), "payload": json.loads(row["payload"])} for row in event_rows]
        metrics = _build_metrics(events, dict(label) if label else None)
        for metric in metrics:
            await db._conn.execute(
                "INSERT INTO trace_evaluations (trace_id, metric_name, score, source, evidence_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (trace_id, metric["metric_name"], metric["score"], metric["source"],
                 json.dumps(metric["evidence"], ensure_ascii=False), _now()),
            )
        await db._conn.commit()
    return metrics


def _build_metrics(events: list[dict[str, Any]], label: dict[str, Any] | None) -> list[dict[str, Any]]:
    event_types = [item["event_type"] for item in events]
    fallbacks = [item for item in events if item["event_type"] == "fallback_decision"]
    tool_calls = [item for item in events if item["event_type"] == "tool_call"]
    tool_failures = [item for item in events if item["event_type"] in {"tool_call_failed", "write_scope_blocked"}]
    validations = [item for item in events if item["event_type"] == "validation_result"]
    review_outputs = [item for item in events if item["event_type"] == "review_aggregated"]
    slot_checks = [item for item in events if item["event_type"] == "slot_validation"]
    clarified = "clarification_requested" in event_types

    validation_passed = bool(validations) and all(
        bool(item["payload"].get("passed")) for item in validations
    )
    review_passed = bool(review_outputs) and bool(review_outputs[-1]["payload"].get("passed"))
    scope_blocks = sum(item["event_type"] == "write_scope_blocked" for item in events)
    scores = [
        _metric("intent_slot_completion", 0.0 if clarified else 1.0, "rule", {
            "clarification_requested": clarified, "checks": len(slot_checks),
        }),
        _metric("tool_success_rate", _ratio(len(tool_calls) - len(tool_failures), len(tool_calls)), "rule", {
            "tool_calls": len(tool_calls), "tool_failures": len(tool_failures),
        }),
        _metric("validation_passed", 1.0 if validation_passed else 0.0, "rule", {
            "validation_count": len(validations), "passed": validation_passed,
        }),
        _metric("review_passed", 1.0 if review_passed else 0.0, "rule", {
            "review_rounds": len(review_outputs), "passed": review_passed,
        }),
        _metric("scope_block_count", float(scope_blocks), "rule", {"count": scope_blocks}),
        _metric("fallback_count", float(len(fallbacks)), "rule", {
            "reason_codes": [item["payload"].get("reason_code") for item in fallbacks],
        }),
    ]
    if label:
        scores.extend([
            _metric("human_acceptance", 1.0 if label["outcome"] in {"approved", "conditional"} else 0.0, "human", {"outcome": label["outcome"]}),
            _metric("code_adoption", {"all": 1.0, "partial": 0.5, "none": 0.0}.get(label.get("adoption"), 0.0), "human", {"adoption": label.get("adoption")}),
            _metric("fix_effectiveness", {"effective": 1.0, "partial": 0.5, "ineffective": 0.0}.get(label.get("fix_effectiveness"), 0.0), "human", {"value": label.get("fix_effectiveness")}),
        ])
    return scores


def _metric(name: str, score: float | None, source: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"metric_name": name, "score": score, "source": source, "evidence": evidence}


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


async def build_evaluation_report(workflow_type: str | None = None, project_path: str | None = None) -> dict[str, Any]:
    """聚合单 Trace 评测，输出项目级质量报告。"""
    clauses: list[str] = []
    params: list[Any] = []
    if workflow_type:
        clauses.append("workflow_type = ?")
        params.append(workflow_type)
    if project_path:
        clauses.append("project_path LIKE ?")
        params.append(f"%{project_path}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with Database() as db:
        traces = await (await db._conn.execute(
            f"SELECT * FROM workflow_traces {where} ORDER BY started_at DESC LIMIT 200", tuple(params)
        )).fetchall()
        trace_ids = [row["trace_id"] for row in traces]
        for trace_id in trace_ids:
            existing = await (await db._conn.execute(
                "SELECT 1 FROM trace_evaluations WHERE trace_id = ? LIMIT 1", (trace_id,)
            )).fetchone()
            if not existing:
                await db._conn.commit()
                await evaluate_trace(trace_id)
        if trace_ids:
            placeholders = ",".join("?" for _ in trace_ids)
            metrics_rows = await (await db._conn.execute(
                f"SELECT metric_name, score FROM trace_evaluations WHERE trace_id IN ({placeholders})", tuple(trace_ids)
            )).fetchall()
        else:
            metrics_rows = []
        grouped: dict[str, list[float]] = {}
        for row in metrics_rows:
            if row["score"] is not None:
                grouped.setdefault(row["metric_name"], []).append(float(row["score"]))
        metrics = {
            "total_traces": len(traces),
            "completed_traces": sum(row["status"] == "done" for row in traces),
            "average_duration_ms": _average([row["duration_ms"] for row in traces if row["duration_ms"] is not None]),
            "total_input_tokens": sum(row["input_tokens"] for row in traces),
            "total_output_tokens": sum(row["output_tokens"] for row in traces),
            **{f"avg_{key}": _average(values) for key, values in grouped.items()},
        }
        cursor = await db._conn.execute(
            "INSERT INTO evaluation_reports (project_path, workflow_type, metrics, created_at) VALUES (?, ?, ?, ?)",
            (project_path, workflow_type, json.dumps(metrics, ensure_ascii=False), _now()),
        )
        await db._conn.commit()
    return {"report_id": cursor.lastrowid, "metrics": metrics, "traces": [dict(item) for item in traces]}


def _average(values: list[float | int]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None
