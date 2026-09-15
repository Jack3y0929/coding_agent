"""为每个 SWE-bench trial 创建、校验和读取隔离 Git 工作区。"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


class WorkspaceError(RuntimeError):
    """工作区准备或 Git 操作失败。"""


def trial_path(work_root: Path, run_id: str, instance_id: str, trial_index: int) -> Path:
    """构造并校验 trial 目录始终位于 work_root 内。"""
    safe_run = _SAFE_ID.sub("_", run_id)
    safe_instance = _SAFE_ID.sub("_", instance_id)
    root = work_root.resolve()
    target = (root / "runs" / safe_run / safe_instance / f"trial-{trial_index}").resolve()
    if os.path.commonpath([str(root), str(target)]) != str(root):
        raise WorkspaceError("trial 工作区越过 SWEBENCH_WORK_ROOT")
    return target


async def prepare_workspace(target: Path, repo: str, base_commit: str) -> Path:
    """克隆仓库并精确恢复 base_commit；已存在目录会被拒绝。"""
    if target.exists():
        raise WorkspaceError(f"trial 工作区已存在，拒绝覆盖: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    repo_url = repo if "://" in repo else f"https://github.com/{repo}.git"
    await _run(["git", "clone", "--no-checkout", repo_url, str(target)])
    await _run(["git", "checkout", "--detach", base_commit], cwd=target)
    actual = (await _run(["git", "rev-parse", "HEAD"], cwd=target)).strip()
    expected = (await _run(["git", "rev-parse", base_commit], cwd=target)).strip()
    if actual != expected:
        raise WorkspaceError(f"base_commit 校验失败: 期望 {expected}，实际 {actual}")
    status = await _run(["git", "status", "--porcelain"], cwd=target)
    if status.strip():
        raise WorkspaceError("checkout 后工作区不是干净状态")
    return target


async def extract_patch(workspace: Path, output_path: Path) -> str:
    """提取包含二进制内容的完整工作区差异。"""
    # intent-to-add 只更新隔离仓库索引，不写入对象；使新文件能够进入 git diff。
    await _run(["git", "add", "--intent-to-add", "--all"], cwd=workspace)
    changed_paths = (
        await _run(["git", "diff", "--name-only", "--no-ext-diff"], cwd=workspace)
    ).splitlines()
    test_changes = [path for path in changed_paths if _is_test_path(path)]
    if test_changes:
        raise WorkspaceError(f"Agent 修改了测试文件，拒绝生成 patch: {', '.join(test_changes)}")
    patch = await _run(["git", "diff", "--binary", "--no-ext-diff"], cwd=workspace)
    if not patch.strip():
        raise WorkspaceError("Agent 未生成任何 git diff")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(patch, encoding="utf-8", newline="\n")
    return patch


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    parts = normalized.split("/")
    filename = parts[-1]
    return (
        "tests" in parts
        or "test" in parts
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


async def _run(command: list[str], cwd: Path | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = stdout.decode("utf-8", errors="replace")
    error = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise WorkspaceError(
            f"命令执行失败({process.returncode}): {' '.join(command[:3])}\n{error[-2000:]}"
        )
    return output
