"""加载 FeatureBench 任务，严格隔离参考答案字段。"""

from __future__ import annotations

import json
from typing import Any

from backend.config import FEATUREBENCH_DATASET, FEATUREBENCH_SPLIT
from backend.evaluation.featurebench.models import FeatureBenchTask


class FeatureBenchTaskNotFoundError(LookupError):
    """指定任务不存在或不唯一。"""


def load_task(
    task_id: str,
    dataset_name: str = FEATUREBENCH_DATASET,
    split: str = FEATUREBENCH_SPLIT,
) -> FeatureBenchTask:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError("缺少 datasets 依赖，请先安装 requirements.txt") from exc
    dataset = load_dataset(dataset_name, split=split)
    matches = dataset.filter(lambda row: _record_id(row) == task_id)
    if len(matches) != 1:
        raise FeatureBenchTaskNotFoundError(
            f"数据集 {dataset_name}/{split} 中未找到唯一任务: {task_id}"
        )
    return task_from_record(dict(matches[0]))


def task_from_record(record: dict[str, Any]) -> FeatureBenchTask:
    """仅复制运行所需公开字段，metadata 也使用白名单。"""
    task_id = _record_id(record)
    return FeatureBenchTask(
        instance_id=task_id,
        problem_statement=str(record.get("problem_statement") or record.get("prompt") or ""),
        image_name=str(record.get("image_name") or record.get("docker_image") or ""),
        level=_parse_level(record.get("level")),
        fail_to_pass=_parse_list(record.get("fail_to_pass")),
        metadata={
            key: record[key]
            for key in ("language", "repo", "version")
            if key in record and record[key] is not None
        },
    )


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("instance_id") or record.get("task_id") or "")


def _parse_level(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []
