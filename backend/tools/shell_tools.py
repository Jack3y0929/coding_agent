from __future__ import annotations

import asyncio
import logging
import locale
import os
import re
from typing import Optional

from backend.config import SHELL_WHITELIST
from backend.tools.models import ToolResult

logger = logging.getLogger("tools.shell")


def _decode_process_output(raw: bytes) -> str:
    """按 UTF-8 优先解码，兼容 Windows 中文命令行的系统代码页输出。"""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        # cmd.exe 在中文 Windows 通常使用 CP936；locale 可覆盖其他区域设置。
        encodings = ["cp936", locale.getpreferredencoding(False)]
        for encoding in dict.fromkeys(encodings):
            try:
                return raw.decode(encoding, errors="strict")
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", errors="replace")

# execute_subprocess_shell 会经过系统 Shell；即使命令前缀命中，也不能允许
# 通过组合语法追加第二条命令或重定向输出。
_SHELL_CONTROL_PATTERN = re.compile(r"[;&|<>`\r\n]|\^|\$\(|%")


def validate_command(command: str) -> bool:
    """检查命令是否命中安全白名单，并拒绝 Shell 组合语法。"""
    cmd = command.strip()
    if not cmd:
        logger.warning("Shell命令被拒绝（命令为空）")
        return False
    if _SHELL_CONTROL_PATTERN.search(cmd):
        logger.warning("Shell命令被拒绝（包含Shell控制语法）: %s", cmd)
        return False
    for allowed in SHELL_WHITELIST:
        if cmd == allowed or cmd.startswith(f"{allowed} "):
            return True
    logger.warning("Shell命令被拒绝（不在白名单）: %s", cmd)
    return False


def get_whitelist() -> list[str]:
    """获取当前白名单列表"""
    return list(SHELL_WHITELIST)


async def execute_shell(command: str, timeout: int = 60,
                        cwd: str | None = None) -> ToolResult:
    """执行白名单内的shell命令，返回stdout+stderr"""
    if not validate_command(command):
        whitelist_str = ", ".join(SHELL_WHITELIST)
        return ToolResult(
            ok=False, code="command_not_allowed",
            message=f"拒绝执行：命令 '{command}' 未通过 Shell 安全校验。",
            suggestion=f"允许的命令: {whitelist_str}", retryable=False,
            tool_name="execute_shell",
        )

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
        stdout_text = _decode_process_output(stdout)
        stderr_text = _decode_process_output(stderr)
        if stdout:
            output_parts.append(stdout_text)
        if stderr:
            output_parts.append("[stderr]\n" + stderr_text)
        output_parts.append(f"[exit_code: {process.returncode}]")

        result = "\n".join(output_parts)
        logger.info(f"命令执行完成: exit_code={process.returncode}")
        return ToolResult(
            ok=process.returncode == 0,
            code="ok" if process.returncode == 0 else "nonzero_exit",
            message=result,
            data={"stdout": stdout_text, "stderr": stderr_text, "exit_code": process.returncode},
            retryable=False,
            tool_name="execute_shell",
        )

    except asyncio.TimeoutError:
        logger.error(f"命令超时（>{timeout}秒）: {command}")
        return ToolResult(
            ok=False, code="timeout", message=f"命令超时（>{timeout}秒）: {command}",
            retryable=True, suggestion="检查命令是否会长时间阻塞，必要时拆分验证步骤。",
            tool_name="execute_shell",
        )
    except FileNotFoundError:
        return ToolResult(
            ok=False, code="file_not_found", message=f"错误: 命令未找到 — {command}",
            retryable=False, tool_name="execute_shell",
        )
    except Exception as exc:
        logger.error(f"命令执行异常: {command}: {exc}")
        return ToolResult(
            ok=False, code="internal_error", message=f"命令执行异常: {exc}",
            retryable=False, tool_name="execute_shell",
        )
