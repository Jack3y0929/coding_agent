"""安全加载 SWE-bench 数据集元数据。"""

from __future__ import annotations

import json
from typing import Any

from backend.config import SWEBENCH_DATASET_NAME, SWEBENCH_SPLIT
from backend.evaluation.swebench.models import SWEbenchInstance


class DatasetInstanceNotFoundError(LookupError):
    """请求的 SWE-bench 实例不存在。"""


def load_instance(
    instance_id: str,
    dataset_name: str = SWEBENCH_DATASET_NAME,
    split: str = SWEBENCH_SPLIT,
) -> SWEbenchInstance:
    """按 instance_id 加载单个任务，禁止返回参考补丁字段。"""
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise RuntimeError("缺少 datasets 依赖，请先安装 requirements.txt") from exc

    dataset = load_dataset(dataset_name, split=split)
    matches = dataset.filter(lambda row: row["instance_id"] == instance_id)
    if len(matches) != 1:
        raise DatasetInstanceNotFoundError(
            f"数据集 {dataset_name}/{split} 中未找到唯一实例: {instance_id}"
        )
    return instance_from_record(dict(matches[0]))


def instance_from_record(record: dict[str, Any]) -> SWEbenchInstance:
    """白名单映射数据集记录，避免 patch/test_patch 意外进入上下文。"""
    allowed = {
        key: record.get(key)
        for key in (
            "instance_id", "repo", "base_commit", "problem_statement", "version",
            "environment_setup_commit", "fail_to_pass", "pass_to_pass",
        )
    }
    allowed["fail_to_pass"] = _parse_test_list(allowed.get("fail_to_pass"))
    allowed["pass_to_pass"] = _parse_test_list(allowed.get("pass_to_pass"))
    return SWEbenchInstance.model_validate(allowed)


def _parse_test_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []
