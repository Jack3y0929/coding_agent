from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

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


async def read_file(path: str) -> str:
    """读取文件内容"""
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return f"错误: 文件不存在 — {abs_path}"

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        logger.info(f"读取文件: {abs_path} ({len(content)} 字符)")
        return content
    except PermissionError:
        return f"错误: 无权限读取 — {abs_path}"
    except Exception as exc:
        return f"错误: 读取文件异常 — {abs_path}: {exc}"


async def write_file(path: str, content: str) -> str:
    """写入文件。路径必须通过安全校验。"""
    try:
        abs_path = _validate_write_path(path)
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
