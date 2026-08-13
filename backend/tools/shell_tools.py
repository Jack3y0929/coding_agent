from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from backend.config import SHELL_WHITELIST

logger = logging.getLogger("tools.shell")


def validate_command(command: str) -> bool:
    """检查命令是否在白名单内（前缀匹配）"""
    cmd = command.strip()
    for allowed in SHELL_WHITELIST:
        if cmd.startswith(allowed):
            return True
    logger.warning(f"Shell命令被拒绝（不在白名单）: {cmd}")
    return False


def get_whitelist() -> list[str]:
    """获取当前白名单列表"""
    return list(SHELL_WHITELIST)


async def execute_shell(command: str, timeout: int = 60,
                        cwd: str | None = None) -> str:
    """执行白名单内的shell命令，返回stdout+stderr"""
    if not validate_command(command):
        whitelist_str = ", ".join(SHELL_WHITELIST)
        return (f"拒绝执行：命令 '{command}' 不在白名单内。\n"
                f"允许的命令: {whitelist_str}")

    work_dir = cwd or os.getcwd()
    logger.info(f"执行shell命令: {command} (cwd={work_dir})")

    try:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
        output_parts: list[str] = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            output_parts.append("[stderr]\n" + stderr.decode("utf-8", errors="replace"))
        output_parts.append(f"[exit_code: {process.returncode}]")

        result = "\n".join(output_parts)
        logger.info(f"命令执行完成: exit_code={process.returncode}")
        return result

    except asyncio.TimeoutError:
        logger.error(f"命令超时（>{timeout}秒）: {command}")
        return f"命令超时（>{timeout}秒）: {command}"
    except FileNotFoundError:
        return f"错误: 命令未找到 — {command}"
    except Exception as exc:
        logger.error(f"命令执行异常: {command}: {exc}")
        return f"命令执行异常: {exc}"
