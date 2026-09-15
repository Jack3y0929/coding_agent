"""从任务容器提取并校验 FeatureBench patch。"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from backend.evaluation.featurebench.container import FeatureBenchContainer


class FeatureBenchPatchError(RuntimeError):
    """patch 为空、越界或篡改基线测试。"""


async def extract_patch(container: FeatureBenchContainer, output_path: Path) -> str:
    """在 /testbed 内生成完整 diff；允许新增测试但拒绝修改已有测试。"""
    changed_before_add = await container.status_paths()
    modified_baseline_tests = sorted(set(changed_before_add) & container.baseline_tests)
    if modified_baseline_tests:
        raise FeatureBenchPatchError(
            f"Agent 修改了基线测试文件: {', '.join(modified_baseline_tests)}"
        )
    for path in changed_before_add:
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise FeatureBenchPatchError(f"patch 路径越过 /testbed: {path}")

    await container.exec(["git", "add", "--intent-to-add", "--all"])
    patch = await container.exec(["git", "diff", "--binary", "--no-ext-diff"])
    if not patch.strip():
        raise FeatureBenchPatchError("Agent 未生成任何 git diff")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(patch, encoding="utf-8", newline="\n")
    return patch
