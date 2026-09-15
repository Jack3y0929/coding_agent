"""生成 FeatureBench 官方 Harness 所需的 JSONL。"""

from __future__ import annotations

import json
from pathlib import Path

from backend.config import DEEPSEEK_MODEL


def write_output(
    path: Path,
    instance_id: str,
    model_patch: str,
    attempt: int,
    success: bool = True,
) -> Path:
    record = {
        "instance_id": instance_id,
        "model_patch": model_patch,
        "success": success,
        "agent": "just_codding",
        "model": DEEPSEEK_MODEL,
        "n_attempt": attempt,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
