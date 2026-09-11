"""LangGraph 节点共享逻辑：API调用、工具执行、prompt加载"""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any

from openai import AsyncOpenAI
import httpx

from backend.budget import BudgetExceededError, TokenBudget
from backend.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_FALLBACK_MODELS,
    MAX_RETRIES,
    MAX_TOOL_ROUNDS,
    API_TIMEOUT,
    API_CONNECT_TIMEOUT,
    API_WRITE_TIMEOUT,
    API_POOL_TIMEOUT,
    PROJECT_ROOT,
    TOOL_CONTEXT_MAX_CHARS,
    TOOL_ROUND_MAX_CHARS,
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
            "description": "按行读取源码文件；默认返回有限片段，需要更多内容时传入 start/end。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（相对于项目根目录）"},
                    "start": {"type": "integer", "minimum": 1, "description": "起始行号（可选，默认从第1行开始）"},
                    "end": {"type": "integer", "minimum": 1, "description": "结束行号（可选，默认按工具上限返回）"},
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

async def _call_deepseek_once(
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

    # SDK 不再自行重试，由项目层统一控制重试次数、退避和 Trace。
    request_timeout = httpx.Timeout(
        timeout=API_TIMEOUT,
        connect=API_CONNECT_TIMEOUT,
        write=API_WRITE_TIMEOUT,
        pool=API_POOL_TIMEOUT,
    )
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEEPSEEK_BASE_URL,
        max_retries=0,
        timeout=request_timeout,
    )

    # 预算检查
    input_tokens = 0
    if budget:
        input_tokens = budget.before_call(messages)

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        started_at = __import__("time").perf_counter()
        try:
            if DEEPSEEK_BASE_URL.rstrip("/").endswith("/anthropic"):
                result = await _call_anthropic_compatible(messages, tools, model, api_key)
                response_content = result["content"]
                response_tool_calls = result["tool_calls"]
                usage_input = result["usage"]["prompt_tokens"]
                usage_output = result["usage"]["completion_tokens"]
            else:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    timeout=request_timeout,
                )
                msg = response.choices[0].message
                response_content = msg.content or ""
                response_tool_calls = [
                    {"id": tc.id, "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ] if msg.tool_calls else None
                usage_input = response.usage.prompt_tokens if response.usage else input_tokens
                usage_output = response.usage.completion_tokens if response.usage else 0

            from backend.trace import record_event
            await record_event("llm_call", None, {
                "model": model,
                "attempt": attempt,
                "input_tokens": usage_input,
                "output_tokens": usage_output,
                "tool_call_count": len(response_tool_calls or []),
            }, int((__import__("time").perf_counter() - started_at) * 1000))
            if budget:
                budget.after_call(usage_input, usage_output)
            return {
                "content": response_content,
                "tool_calls": response_tool_calls,
                "usage": {
                    "prompt_tokens": usage_input,
                    "completion_tokens": usage_output,
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


async def call_deepseek(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str = DEEPSEEK_MODEL,
    api_key: str = DEEPSEEK_API_KEY,
    budget: TokenBudget | None = None,
) -> dict[str, Any]:
    """调用主模型；重试耗尽后按配置顺序切换降级模型。"""
    candidates = list(dict.fromkeys([model, *DEEPSEEK_FALLBACK_MODELS]))
    last_error: Exception | None = None
    for index, candidate in enumerate(candidates):
        try:
            return await _call_deepseek_once(messages, tools, candidate, api_key, budget)
        except BudgetExceededError:
            raise
        except Exception as exc:
            last_error = exc
            if index < len(candidates) - 1:
                logger.warning("模型 %s 不可用，切换降级模型 %s: %s", candidate, candidates[index + 1], exc)
    raise RuntimeError(f"所有候选模型均调用失败: {last_error}")


async def _call_anthropic_compatible(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str,
    api_key: str,
) -> dict[str, Any]:
    """调用 DeepSeek Anthropic 兼容协议并转换为内部 OpenAI 风格结果。"""
    system_parts = [str(item.get("content", "")) for item in messages if item.get("role") == "system"]
    converted: list[dict[str, Any]] = []
    for item in messages:
        role = item.get("role")
        if role == "system":
            continue
        if role == "tool":
            converted.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": item.get("tool_call_id", ""),
                "content": str(item.get("content", "")),
            }]})
            continue
        content = item.get("content", "")
        if role == "assistant" and item.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": str(content)})
            for call in item["tool_calls"]:
                fn = call.get("function", {})
                try:
                    arguments = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append({"type": "tool_use", "id": call.get("id", ""),
                               "name": fn.get("name", ""), "input": arguments})
            content = blocks
        converted.append({"role": "assistant" if role == "assistant" else "user", "content": content})

    payload: dict[str, Any] = {
        "model": model, "max_tokens": 4096, "messages": converted,
    }
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if tools:
        payload["tools"] = [{
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "input_schema": tool["function"].get("parameters", {"type": "object"}),
        } for tool in tools]

    request_timeout = httpx.Timeout(
        timeout=API_TIMEOUT,
        connect=API_CONNECT_TIMEOUT,
        write=API_WRITE_TIMEOUT,
        pool=API_POOL_TIMEOUT,
    )
    async with httpx.AsyncClient(timeout=request_timeout) as http:
        response = await http.post(
            f"{DEEPSEEK_BASE_URL.rstrip('/')}/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    text_parts = [block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"]
    tool_calls = [{
        "id": block.get("id", ""),
        "function": {"name": block.get("name", ""), "arguments": json.dumps(block.get("input", {}), ensure_ascii=False)},
    } for block in data.get("content", []) if block.get("type") == "tool_use"]
    usage = data.get("usage", {})
    return {"content": "\n".join(text_parts), "tool_calls": tool_calls or None,
            "usage": {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0)}}


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
            result = await read_file(arguments["path"], arguments.get("start"), arguments.get("end"), project_path=project_path or None)

        elif tool_name == "write_file":
            from backend.intent.context import current_intent
            from backend.intent.rules import validate_write_scope
            intent = current_intent()
            from backend.project_scope import project_file_path
            target = project_file_path(project_path, arguments["path"]) if project_path else arguments["path"]
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
                project_path=project_path or None,
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
    base_messages = list(messages)  # 节点独立上下文，不修改调用方消息
    current_messages = base_messages
    last_tool_summary = ""
    max_rounds = MAX_TOOL_ROUNDS

    for _ in range(max_rounds):
        result = await call_deepseek(current_messages, tools=tools, budget=budget)

        if not result.get("tool_calls"):
            return result["content"]

        # 轮次结束后只保留短摘要，避免历史工具原文累积到下一次请求。
        tool_summaries: list[str] = []
        if result.get("content"):
            tool_summaries.append(f"模型本轮说明：\n{str(result['content'])[:TOOL_CONTEXT_MAX_CHARS]}")

        # 执行每个工具调用
        for tc in result["tool_calls"]:
            func_name = tc["function"]["name"]
            try:
                func_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                func_args = {}

            tool_result = await execute_tool(func_name, func_args, mode, project_path)
            bounded_result = tool_result[:TOOL_CONTEXT_MAX_CHARS]
            if len(tool_result) > TOOL_CONTEXT_MAX_CHARS:
                bounded_result += "\n...（工具结果已截断，必要时请缩小读取范围）"
            tool_summaries.append(f"工具 {func_name} 返回：\n{bounded_result}")

        last_tool_summary = "\n\n".join(tool_summaries)
        if len(last_tool_summary) > TOOL_ROUND_MAX_CHARS:
            last_tool_summary = (
                last_tool_summary[:TOOL_ROUND_MAX_CHARS]
                + "\n...（本轮工具摘要已截断，请拆分工具调用或缩小读取范围）"
            )
        current_messages = [
            *base_messages,
            {
                "role": "user",
                "content": (
                    "上一轮工具执行摘要（仅保留本轮结果，历史工具原文已丢弃）：\n"
                    + last_tool_summary
                    + "\n\n请基于以上已核实信息输出最终回答；不要再调用工具。"
                ),
            },
        ]

    return (
        "工具调用轮次已达上限，模型未输出最终结论。"
        "请基于已有摘要直接输出符合输出契约的最终报告。"
    )


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
        rag_context = await hybrid_retrieve_for_role(query, role, project_path=project_path or None)
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
