"""LangGraph 节点共享逻辑：API调用、工具执行、prompt加载"""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any

from openai import AsyncOpenAI

from backend.budget import BudgetExceededError, TokenBudget
from backend.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    MAX_RETRIES,
    MAX_TOOL_ROUNDS,
    API_TIMEOUT,
    PROJECT_ROOT,
)
from backend.db.models import Database
from backend.tools.file_tools import read_file, write_file
from backend.tools.shell_tools import execute_shell, validate_command
from backend.tools.search_tools import search_code

logger = logging.getLogger("graph.nodes")

# Prompt 文件路径
PROMPT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "agents")

# ---- FC 工具定义 ----

FC_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取源码文件内容。返回文件全文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对于项目根目录）"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入代码到指定文件。仅在Build模式下可用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "文件完整内容"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在项目源码中搜索匹配正则表达式的内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_pattern": {"type": "string", "description": "限定文件类型，如 *.py"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell",
            "description": "执行shell命令。仅在Build模式下可用，且命令必须在白名单内。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_checkpoint",
            "description": "保存当前进度的断点。同时触发Plan→Build模式切换请求。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "请求切换的理由"}
                },
                "required": ["reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_logging",
            "description": "添加日志打点代码（仅用于DEBUG诊断，不改业务逻辑）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "variables": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["file", "line", "variables"]
            }
        }
    },
]

PLAN_MODE_TOOLS = [t for t in FC_TOOLS if t["function"]["name"] in
                   ("read_file", "search_code", "save_checkpoint")]
BUILD_MODE_TOOLS = FC_TOOLS


# ---- System Prompt 加载 ----

def load_system_prompt(role: str) -> str:
    """加载角色的 system prompt"""
    filename = f"{role}_prompt.md"
    filepath = os.path.join(PROMPT_DIR, filename)

    if os.path.isfile(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()

    # 回退：从项目根目录的 agents/ 加载
    alt_path = os.path.join(PROJECT_ROOT, "agents", filename)
    if os.path.isfile(alt_path):
        with open(alt_path, "r", encoding="utf-8") as f:
            return f.read()

    logger.warning(f"Prompt文件未找到: {role} ({filepath})")
    return f"你是{role}角色。请根据上下文完成你的任务。"


# ---- DeepSeek API 调用（单次） ----

async def call_deepseek(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str = DEEPSEEK_MODEL,
    api_key: str = DEEPSEEK_API_KEY,
    budget: TokenBudget | None = None,
) -> dict[str, Any]:
    """
    单次调用 DeepSeek API（不含 FC 循环），带重试机制。
    返回: {"content": str, "tool_calls": list | None, "usage": dict}
    """
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY 未设置")

    client = AsyncOpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

    # 预算检查
    input_tokens = 0
    if budget:
        input_tokens = budget.before_call(messages)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        started_at = __import__("time").perf_counter()
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                timeout=API_TIMEOUT,
            )

            usage = response.usage
            if budget and usage:
                budget.after_call(
                    usage.prompt_tokens or input_tokens,
                    usage.completion_tokens or 0
                )

            msg = response.choices[0].message
            from backend.trace import record_event
            await record_event("llm_call", None, {
                "model": model,
                "attempt": attempt,
                "input_tokens": usage.prompt_tokens if usage else input_tokens,
                "output_tokens": usage.completion_tokens if usage else 0,
                "tool_call_count": len(msg.tool_calls or []),
            }, int((__import__("time").perf_counter() - started_at) * 1000))
            return {
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    }
                    for tc in msg.tool_calls
                ] if msg.tool_calls else None,
                "usage": {
                    "prompt_tokens": usage.prompt_tokens if usage else 0,
                    "completion_tokens": usage.completion_tokens if usage else 0,
                },
            }

        except BudgetExceededError:
            raise  # 预算超限不重试
        except Exception as exc:
            from backend.trace import record_event
            await record_event("llm_call_failed", None, {
                "model": model, "attempt": attempt, "error": str(exc),
            }, int((__import__("time").perf_counter() - started_at) * 1000))
            last_error = exc
            logger.warning(f"API调用失败 (尝试 {attempt}/{MAX_RETRIES}): {exc}")
            if attempt < MAX_RETRIES:
                await __import__("asyncio").sleep(1)

    raise RuntimeError(f"API调用失败（已重试{MAX_RETRIES}次）: {last_error}")


# ---- FC 工具执行 ----

async def execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    mode: str,
    project_path: str = "",
) -> str:
    """执行FC工具调用，mode为'plan'或'build'"""
    if mode == "plan" and tool_name in ("write_file", "execute_shell"):
        return "错误：Plan模式下禁止执行此操作。"

    try:
        result: str
        if tool_name == "read_file":
            target = os.path.join(project_path, arguments["path"]) if project_path else arguments["path"]
            result = await read_file(target)

        elif tool_name == "write_file":
            target = os.path.join(project_path, arguments["path"]) if project_path else arguments["path"]
            from backend.intent.context import current_intent
            from backend.intent.rules import validate_write_scope
            intent = current_intent()
            scope_error = validate_write_scope(project_path, target, intent) if intent else None
            if scope_error:
                result = f"错误：研发槽位范围校验未通过：{scope_error}"
                from backend.trace import record_event
                await record_event("write_scope_blocked", "build", {
                    "path": arguments["path"], "reason": scope_error,
                })
            else:
                result = await write_file(target, arguments["content"])

        elif tool_name == "search_code":
            result = await search_code(
                arguments["pattern"],
                arguments.get("file_pattern"),
                directory=project_path,
            )

        elif tool_name == "execute_shell":
            result = await execute_shell(
                arguments["command"],
                cwd=project_path,
            )
            from backend.trace import record_event
            await record_event("validation_result", "validation", {
                "command": arguments["command"],
                "passed": "[exit_code: 0]" in result,
                "result": result[:1000],
            })

        elif tool_name == "save_checkpoint":
            result = "checkpoint已保存，请求切换Build模式。"

        elif tool_name == "add_logging":
            filepath = os.path.join(project_path, arguments["file"]) if project_path else arguments["file"]
            line_num = arguments["line"]
            variables = arguments.get("variables", [])
            result = await _add_logging(filepath, line_num, variables)

        else:
            result = f"未知工具: {tool_name}"

        from backend.trace import record_event
        await record_event("tool_call", None, {
            "tool": tool_name, "arguments": arguments,
            "result": result[:1000],
        })
        return result

    except Exception as exc:
        logger.error(f"工具执行异常: {tool_name}: {exc}")
        from backend.trace import record_event
        await record_event("tool_call_failed", None, {
            "tool": tool_name, "arguments": arguments, "error": str(exc),
        })
        return f"工具执行异常: {exc}"


# ---- FC 工具循环 ----

async def run_fc_loop(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    budget: TokenBudget,
    mode: str,
    project_path: str,
) -> str:
    """
    运行 FC 工具循环：发送消息 → 检查 tool_calls → 执行工具 → 继续。
    返回最终的文本内容。
    """
    current_messages = list(messages)  # 拷贝，不修改原始
    max_rounds = MAX_TOOL_ROUNDS

    for _ in range(max_rounds):
        result = await call_deepseek(current_messages, tools=tools, budget=budget)

        if not result.get("tool_calls"):
            return result["content"]

        # 追加 assistant 消息（含 tool_calls）
        current_messages.append({
            "role": "assistant",
            "content": result["content"],
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    }
                }
                for tc in result["tool_calls"]
            ]
        })

        # 执行每个工具调用
        for tc in result["tool_calls"]:
            func_name = tc["function"]["name"]
            try:
                func_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                func_args = {}

            tool_result = await execute_tool(func_name, func_args, mode, project_path)
            current_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result,
            })

    return current_messages[-1].get("content", "") if current_messages else ""


# ---- 节点间消息构建 ----

def build_node_messages(system_prompt: str, user_content: str) -> list[dict[str, Any]]:
    """构建节点的初始消息列表（独立上下文）"""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


# ---- 日志打点 ----

async def _add_logging(filepath: str, line_num: int, variables: list[str]) -> str:
    """在指定文件的指定位置添加日志打点代码"""
    if not os.path.isfile(filepath):
        return f"错误: 文件不存在 — {filepath}"

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        if line_num < 1 or line_num > len(lines):
            return f"错误: 行号 {line_num} 超出范围 (1-{len(lines)})"

        var_str = ", ".join(f"{v}={{{v}}}" for v in variables)
        indent = len(lines[line_num - 1]) - len(lines[line_num - 1].lstrip())
        log_line = " " * indent + f'print(f"[DIAGNOSE] LINE:{line_num} {var_str}")\n'

        lines.insert(line_num - 1, log_line)

        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(lines)

        logger.info(f"添加日志打点: {filepath}:{line_num} vars={variables}")
        return f"日志打点已添加: {filepath}:{line_num}, 变量: {variables}"

    except Exception as exc:
        return f"错误: 添加日志打点失败 — {exc}"


# ---- RAG 上下文注入 ----

async def inject_rag_context(role: str, query: str, project_path: str = "") -> str:
    """为指定角色注入 RAG 检索上下文"""
    from backend.rag.retriever import hybrid_retrieve_for_role
    try:
        rag_context = await hybrid_retrieve_for_role(query, role)
        from backend.trace import get_active_preferences
        preferences = await get_active_preferences(project_path) if project_path else []
        if not preferences:
            return rag_context
        preference_lines = ["--- 人类确认的开发偏好 ---"]
        for item in preferences:
            preference_lines.append(
                f"- 【{item['scope_type']}】{item['title']}: {item['content']}"
            )
        preference_lines.append("--- 开发偏好结束 ---")
        return f"{rag_context}\n\n" + "\n".join(preference_lines)
    except Exception as exc:
        logger.warning(f"RAG检索失败: {exc}")
        return "(RAG检索暂时不可用)"
