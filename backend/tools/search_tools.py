from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional
from backend.project_scope import real_project_path

logger = logging.getLogger("tools.search")

# 支持的源码文件扩展名
_SOURCE_EXTENSIONS = {".py", ".js", ".ts", ".vue", ".jsx", ".tsx", ".rs",
                      ".java", ".go", ".c", ".cpp", ".h", ".hpp", ".css",
                      ".html", ".json", ".yaml", ".yml", ".toml", ".md"}


async def search_code(pattern: str, file_pattern: str | None = None,
                      directory: str | None = None,
                      max_results: int = 50,
                      project_path: str | None = None) -> str:
    """在项目源码中搜索匹配正则表达式的内容"""
    try:
        scope = real_project_path(project_path) if project_path else None
    except ValueError as exc:
        return f"错误: 项目路径无效 — {exc}"
    search_dir = scope or directory or os.getcwd()
    if scope and directory:
        candidate = os.path.realpath(os.path.abspath(directory))
        if os.path.commonpath([scope, candidate]) != scope:
            return f"错误: 搜索目录不在项目范围内 — {directory}"
    if not os.path.isdir(search_dir):
        return f"错误: 目录不存在 — {search_dir}"

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"错误: 无效的正则表达式 — {pattern}: {exc}"

    results: list[str] = []
    try:
        for root, dirs, files in os.walk(search_dir):
            # 跳过隐藏目录和常见非源码目录
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("node_modules", "__pycache__", "target",
                                     ".git", "dist", "build", ".venv", "venv")]

            for filename in files:
                if file_pattern and not _match_file_pattern(filename, file_pattern):
                    continue
                if not _is_source_file(filename):
                    continue

                filepath = os.path.join(root, filename)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, 1):
                            if regex.search(line):
                                rel_path = os.path.relpath(filepath, search_dir)
                                results.append(f"{rel_path}:{line_no}: {line.rstrip()}")
                                if len(results) >= max_results:
                                    break
                except (PermissionError, OSError):
                    continue
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break
    except (PermissionError, OSError) as exc:
        return f"错误: 搜索异常: {exc}"

    if not results:
        return f"未找到匹配 '{pattern}' 的内容（搜索范围: {search_dir}）"

    header = f"搜索结果 ({len(results)} 条匹配 '{pattern}'):\n"
    return header + "\n".join(results)


def _match_file_pattern(filename: str, glob_pattern: str) -> bool:
    """简单的文件名通配符匹配"""
    if glob_pattern.startswith("*."):
        return filename.endswith(glob_pattern[1:])
    if glob_pattern.startswith("*"):
        return glob_pattern[1:] in filename
    return filename == glob_pattern


def _is_source_file(filename: str) -> bool:
    """判断是否为源码文件"""
    _, ext = os.path.splitext(filename)
    return ext.lower() in _SOURCE_EXTENSIONS
