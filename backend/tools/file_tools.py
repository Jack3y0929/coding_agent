from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional
from backend.project_scope import project_file_path

from backend.config import READ_FILE_MAX_CHARS, READ_FILE_MAX_LINES

logger = logging.getLogger("tools.file")

# 允许写入的目录列表
_ALLOWED_DIRS: list[str] = []


def set_allowed_dirs(dirs: list[str]) -> None:
    """设置允许写入的目录（通常在应用启动时配置）"""
    global _ALLOWED_DIRS
    _ALLOWED_DIRS = [os.path.abspath(d) for d in dirs]


def _validate_write_path(file_path: str) -> str:
    """校验写入路径是否在允许的目录内。成功返回绝对路径，失败抛异常。"""
    abs_path = os.path.abspath(file_path)
    if _ALLOWED_DIRS:
        for allowed in _ALLOWED_DIRS:
            if abs_path.startswith(allowed):
                return abs_path
        raise PermissionError(
            f"写入路径不在允许范围内: {abs_path}。"
            f"允许的目录: {_ALLOWED_DIRS}"
        )
    # 无白名单时不限制（开发模式）
    return abs_path


async def read_file(path: str, start: int | None = None, end: int | None = None,
                    project_path: str | None = None) -> str:
    """按行读取文件，默认只返回有限片段，并提供续读提示。"""
    try:
        abs_path = project_file_path(project_path, path) if project_path else os.path.realpath(os.path.abspath(path))
    except (ValueError, PermissionError) as exc:
        return f"错误: 文件路径不在项目范围内 — {exc}"
    if not os.path.isfile(abs_path):
        return f"错误: 文件不存在 — {abs_path}"

    try:
        requested_start = 1 if start is None else max(1, int(start))
        requested_end = None if end is None else max(requested_start, int(end))
        max_selected_end = requested_start + READ_FILE_MAX_LINES - 1
        selected_lines: list[str] = []
        total_lines = 0
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                total_lines = line_no
                if line_no < requested_start or line_no > max_selected_end:
                    continue
                if requested_end is not None and line_no > requested_end:
                    continue
                selected_lines.append(line)

        selected_start = min(requested_start, total_lines + 1)
        selected_end = selected_start + len(selected_lines) - 1

        content = "".join(
            f"{line_no:>6}: {line}" for line_no, line in
            zip(range(selected_start, selected_end + 1), selected_lines)
        )
        truncated_by_chars = len(content) > READ_FILE_MAX_CHARS
        if truncated_by_chars:
            content = content[:READ_FILE_MAX_CHARS]
            content += "\n...（读取结果已按字符数截断，请使用 start/end 分段读取）"

        requested_end_for_display = total_lines if requested_end is None else requested_end
        truncated_by_lines = (
            selected_end < requested_end_for_display
            or (start is None and total_lines > selected_end)
        )
        if truncated_by_lines and "读取结果已按字符数截断" not in content:
            content += (
                f"\n...（仅读取第 {selected_start}-{selected_end} 行，共 {total_lines} 行；"
                "如需继续，请传入 start 和 end）"
            )
        if not content:
            content = f"（文件为空或请求行范围超出文件：共 {total_lines} 行）"

        logger.info(
            "读取文件: %s (返回第%d-%d行/%d行, %d字符%s)",
            abs_path, selected_start, selected_end, total_lines, len(content),
            ", 已截断" if truncated_by_chars or truncated_by_lines else "",
        )
        return content
    except PermissionError:
        return f"错误: 无权限读取 — {abs_path}"
    except Exception as exc:
        return f"错误: 读取文件异常 — {abs_path}: {exc}"


async def write_file(path: str, content: str, project_path: str | None = None) -> str:
    """写入文件。路径必须通过安全校验。"""
    try:
        abs_path = project_file_path(project_path, path) if project_path else _validate_write_path(path)
        abs_path = _validate_write_path(abs_path)
    except PermissionError as exc:
        logger.warning(f"写文件被拒绝: {exc}")
        return f"错误: {exc}"

    try:
        parent = os.path.dirname(abs_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"写入文件: {abs_path} ({len(content)} 字符)")
        return f"写入成功: {abs_path} ({len(content)} 字符)"
    except PermissionError:
        return f"错误: 无权限写入 — {abs_path}"
    except Exception as exc:
        return f"错误: 写入文件异常 — {abs_path}: {exc}"


def get_file_info(path: str) -> Optional[dict]:
    """获取文件基本信息（不读内容）"""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        return None
    stat = os.stat(abs_path)
    return {
        "path": abs_path,
        "name": os.path.basename(abs_path),
        "size": stat.st_size,
        "modified": stat.st_mtime,
    }
