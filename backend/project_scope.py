"""项目路径隔离与文件范围校验。"""

from __future__ import annotations

import os


def real_project_path(project_path: str) -> str:
    """解析并校验项目根目录，必须是已存在的目录。"""
    if not project_path or not str(project_path).strip():
        raise ValueError("project_path 不能为空")
    resolved = os.path.realpath(os.path.abspath(str(project_path)))
    if not os.path.isdir(resolved):
        raise ValueError(f"项目目录不存在: {resolved}")
    return resolved


def project_file_path(project_path: str, file_path: str) -> str:
    """将文件路径解析到项目目录内，拒绝路径穿越和符号链接逃逸。"""
    root = real_project_path(project_path)
    candidate = os.path.realpath(os.path.abspath(os.path.join(root, file_path)))
    try:
        inside = os.path.commonpath([root, candidate]) == root
    except ValueError:
        inside = False
    if not inside:
        raise PermissionError(f"路径不在项目范围内: {file_path}")
    return candidate

