"""LangGraph 节点共享逻辑：API调用、工具执行、prompt加载"""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any, TypedDict

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
    MAX_IDENTICAL_TOOL_CALLS,
)
from backend.db.models import Database
from backend.tools.file_tools import read_file, write_file
from backend.tools.shell_tools import execute_shell, validate_command
from backend.tools.search_tools import search_code
from backend.tools.models import ToolResult, normalize_tool_result
from backend.tools.recovery import record_tool_failure
from backend.skills.runtime import restrict_tool_names

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


class ReactPolicy(TypedDict, total=False):
    """节点内受限 ReAct 工具循环策略。"""

    max_rounds: int
    max_tool_calls: int
    stop_tools: list[str]


PLAN_REACT_POLICY: ReactPolicy = {
    "max_rounds": 6,
    "max_tool_calls": 12,
    "stop_tools": ["save_checkpoint"],
}

BUILD_REACT_POLICY: ReactPolicy = {
    "max_rounds": 10,
    "max_tool_calls": 20,
}


# ---- System Prompt 加载 ----

def load_system_prompt(role: str) -> str:
    """加载角色的 system prompt"""
    filename = f"{role}_prompt.md"
    filepath = os.path.join(PROMPT_DIR, filename)

    if os.path.isfile(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            prompt = f.read()
            return _append_skill_prompt(role, prompt)

    # 回退：从项目根目录的 agents/ 加载
    alt_path = os.path.join(PROJECT_ROOT, "agents", filename)
    if os.path.isfile(alt_path):
        with open(alt_path, "r", encoding="utf-8") as f:
            return _append_skill_prompt(role, f.read())

    logger.warning(f"Prompt文件未找到: {role} ({filepath})")
    return _append_skill_prompt(role, f"你是{role}角色。请根据上下文完成你的任务。")


def _append_skill_prompt(role: str, prompt: str) -> str:
    """加载对应 Skill 的流程规则；加载失败时保留旧 Prompt 行为。"""
    from backend.skills.registry import get_skill_registry, initialize_skill_registry
    from backend.skills.runtime import compose_prompt

    initialize_skill_registry({item["function"]["name"] for item in FC_TOOLS})
    skill_id = {
        "analyst": "requirements-analysis",
        "debugger": "debug-diagnosis",
        "developer": "code-development",
        "reviewer": "code-review",
        "review_logic": "code-review",
        "review_security": "code-review",
        "review_quality": "code-review",
    }.get(role)
    skill = get_skill_registry().get(skill_id) if skill_id else None
    return compose_prompt(prompt, skill)


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
    execution_report: dict[str, Any] | None = None,
) -> ToolResult:
    """执行FC工具调用，mode为'plan'或'build'"""
    if mode == "plan" and tool_name in ("write_file", "execute_shell"):
        return ToolResult(
            ok=False, code="permission_denied", message="Plan模式下禁止执行此操作。",
            retryable=False, tool_name=tool_name,
        )
    required_arguments = {
        "read_file": ("path",),
        "write_file": ("path", "content"),
        "search_code": ("pattern",),
        "execute_shell": ("command",),
        "save_checkpoint": ("reason",),
        "add_logging": ("file", "line", "variables"),
    }.get(tool_name)
    if required_arguments is None:
        return ToolResult(
            ok=False, code="invalid_arguments", message=f"未知工具: {tool_name}",
            retryable=False, tool_name=tool_name,
        )
    if not isinstance(arguments, dict) or any(
        key not in arguments or arguments[key] in (None, "") for key in required_arguments
    ):
        missing = [key for key in required_arguments if not isinstance(arguments, dict) or key not in arguments]
        return ToolResult(
            ok=False,
            code="invalid_arguments",
            message=f"工具 {tool_name} 缺少必填参数: {', '.join(missing) or '参数值为空'}。",
            retryable=True,
            suggestion="请根据工具参数 schema 补全参数后重试。",
            tool_name=tool_name,
        )

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
            from backend.project_scope import project_file_path
            from backend.intent.context import current_intent
            from backend.intent.rules import validate_write_scope
            filepath = project_file_path(project_path, arguments["file"]) if project_path else arguments["file"]
            intent = current_intent()
            if intent:
                scope_error = validate_write_scope(project_path, filepath, intent)
                if scope_error:
                    result = f"错误：研发槽位范围校验未通过：{scope_error}"
                else:
                    line_num = arguments["line"]
                    variables = arguments.get("variables", [])
                    result = await _add_logging(filepath, line_num, variables)
            else:
                line_num = arguments["line"]
                variables = arguments.get("variables", [])
                result = await _add_logging(filepath, line_num, variables)

        else:
            result = f"未知工具: {tool_name}"

        from backend.trace import record_event
        normalized = normalize_tool_result(result, tool_name)
        await record_event("tool_call", None, {
            "tool": tool_name, "arguments": arguments,
            "result": normalized.model_dump(mode="json"),
        })
        if execution_report is not None:
            execution_report.setdefault("tool_calls", []).append(tool_name)
            if tool_name == "write_file":
                write_record = {
                    "path": str(arguments.get("path", "")),
                    "content": str(arguments.get("content", "")),
                    "result": normalized.message[:500],
                }
                # 以结构化结果为准，避免成功消息文案变化导致误判为未写入。
                if normalized.ok:
                    execution_report.setdefault("successful_writes", []).append(write_record)
                else:
                    execution_report.setdefault("failed_writes", []).append(write_record)
        return normalized

    except Exception as exc:
        logger.error(f"工具执行异常: {tool_name}: {exc}")
        from backend.trace import record_event
        await record_event("tool_call_failed", None, {
            "tool": tool_name, "arguments": arguments, "error": str(exc),
        })
        return ToolResult(
            ok=False, code="internal_error", message=f"工具执行异常: {exc}",
            retryable=False, tool_name=tool_name,
        )


# ---- FC 工具循环 ----

async def run_fc_loop(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    budget: TokenBudget,
    mode: str,
    project_path: str,
    execution_report: dict[str, Any] | None = None,
    skill_id: str | None = None,
    react_enabled: bool = False,
    react_policy: ReactPolicy | None = None,
) -> str:
    """
    运行 FC 工具循环：发送消息 → 检查 tool_calls → 执行工具 → 继续。
    返回最终的文本内容。
    """
    base_messages = list(messages)  # 节点独立上下文，不修改调用方消息
    if skill_id:
        from backend.skills.registry import get_skill_registry, initialize_skill_registry
        initialize_skill_registry({item["function"]["name"] for item in FC_TOOLS})
        skill = get_skill_registry().get(skill_id)
        tools = restrict_tool_names(skill, mode, tools)
    current_messages = base_messages
    last_tool_summary = ""
    policy = react_policy or {}
    max_rounds = int(policy.get("max_rounds", MAX_TOOL_ROUNDS)) if react_enabled else MAX_TOOL_ROUNDS
    max_tool_calls = int(policy.get("max_tool_calls", max_rounds * 2)) if react_enabled else None
    stop_tools = set(policy.get("stop_tools", [])) if react_enabled else set()
    retry_counts: dict[str, int] = {}
    identical_counts: dict[str, int] = {}
    total_tool_calls = 0
    stop_requested = False
    react_steps: list[dict[str, Any]] = []

    if react_enabled and execution_report is not None:
        execution_report.setdefault("react_rounds", 0)
        execution_report.setdefault("react_steps", react_steps)
        execution_report.setdefault("react_stop_requested", False)
        execution_report.setdefault("react_finalized", False)

    for round_index in range(1, max_rounds + 1):
        result = await call_deepseek(current_messages, tools=tools, budget=budget)

        if not result.get("tool_calls"):
            if react_enabled and execution_report is not None:
                execution_report["react_finalized"] = True
            return result["content"]

        if react_enabled and max_tool_calls is not None:
            remaining_calls = max_tool_calls - total_tool_calls
            if remaining_calls <= 0:
                if execution_report is not None:
                    execution_report["react_limit_reached"] = "tool_calls"
                break
            tool_calls = result["tool_calls"][:remaining_calls]
        else:
            tool_calls = result["tool_calls"]

        # 轮次结束后只保留短摘要，避免历史工具原文累积到下一次请求。
        tool_summaries: list[str] = []
        if result.get("content"):
            tool_summaries.append(f"模型本轮说明：\n{str(result['content'])[:TOOL_CONTEXT_MAX_CHARS]}")

        # 执行每个工具调用
        round_retryable = False
        for tc in tool_calls:
            func_name = tc["function"]["name"]
            total_tool_calls += 1
            try:
                func_args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                func_args = {}
                raw_tool_result = ToolResult(
                    ok=False,
                    code="invalid_arguments",
                    message=f"工具 {func_name} 的 arguments 不是合法 JSON。",
                    retryable=True,
                    suggestion="请按工具参数 schema 重新生成 JSON。",
                    tool_name=func_name,
                )
            else:
                raw_tool_result = await execute_tool(
                    func_name, func_args, mode, project_path, execution_report
                )
            tool_result = normalize_tool_result(raw_tool_result, func_name)
            call_key = f"{func_name}:{json.dumps(func_args, sort_keys=True, ensure_ascii=False)}"
            identical_counts[call_key] = identical_counts.get(call_key, 0) + 1
            if identical_counts[call_key] > MAX_IDENTICAL_TOOL_CALLS:
                tool_result = tool_result.model_copy(update={
                    "ok": False,
                    "code": "internal_error",
                    "retryable": False,
                    "message": "检测到相同工具调用重复执行，已停止以避免死循环。",
                    "suggestion": "请改用不同参数，或直接输出当前阶段报告。",
                })
            if not tool_result.ok and tool_result.retryable:
                retry_counts[func_name] = retry_counts.get(func_name, 0) + 1
                if not record_tool_failure(
                    execution_report, tool_result, func_args, retry_counts[func_name]
                ):
                    tool_result = tool_result.model_copy(update={
                        "retryable": False,
                        "message": f"{tool_result.message}（已达到该工具重试上限）",
                    })
                else:
                    round_retryable = True
            elif not tool_result.ok:
                retry_counts[func_name] = retry_counts.get(func_name, 0)
                record_tool_failure(execution_report, tool_result, func_args, retry_counts[func_name])
            if not tool_result.ok:
                from backend.trace import record_event
                await record_event(
                    "tool_retry" if round_retryable else "tool_retry_exhausted",
                    "tool",
                    {
                        "tool": func_name,
                        "code": tool_result.code,
                        "retryable": round_retryable,
                        "retry_count": retry_counts.get(func_name, 0),
                    },
                )
            result_text = tool_result.to_context_text()
            bounded_result = result_text[:TOOL_CONTEXT_MAX_CHARS]
            if len(result_text) > TOOL_CONTEXT_MAX_CHARS:
                bounded_result += "\n...（工具结果已截断，必要时请缩小读取范围）"
            tool_summaries.append(f"工具 {func_name} 返回：\n{bounded_result}")

            if react_enabled:
                step = {
                    "round": round_index,
                    "action": func_name,
                    "arguments": _summarize_react_value(func_args),
                    "observation": bounded_result[:2000],
                    "ok": tool_result.ok,
                }
                react_steps.append(step)
                if execution_report is not None:
                    execution_report["react_rounds"] = round_index
                from backend.trace import record_event
                await record_event("react_step", mode, step)
                if func_name in stop_tools and tool_result.ok:
                    stop_requested = True
                    if execution_report is not None:
                        execution_report["react_stop_requested"] = True
                    # 停止工具表示当前节点已完成动作规划；同一响应中的后续动作不再执行。
                    break

        last_tool_summary = "\n\n".join(tool_summaries)
        if len(last_tool_summary) > TOOL_ROUND_MAX_CHARS:
            last_tool_summary = (
                last_tool_summary[:TOOL_ROUND_MAX_CHARS]
                + "\n...（本轮工具摘要已截断，请拆分工具调用或缩小读取范围）"
            )
        # 由摘要中的失败提示决定是否给模型纠错机会；成功轮次直接要求输出最终报告。
        if react_enabled:
            instruction = (
                "断点请求已接受。请基于以上已核实信息输出最终方案，不要再调用工具。"
                if stop_requested else
                "请根据以上 observation 判断下一步。需要更多证据时继续调用工具；完成后直接输出正式结果。"
            )
            if round_retryable:
                instruction = "请根据工具错误修正参数后重试一次；不要重复完全相同的调用。"
        else:
            instruction = (
                "请根据工具错误修正参数后重试一次；不要重复完全相同的调用。"
                if round_retryable else
                "请基于以上已核实信息输出最终回答；不要再调用工具。"
            )
        current_messages = [
            *base_messages,
            {
                "role": "user",
                "content": (
                    "上一轮工具执行摘要（仅保留本轮结果，历史工具原文已丢弃）：\n"
                    + last_tool_summary
                    + "\n\n"
                    + instruction
                ),
            },
        ]

    if react_enabled and execution_report is not None:
        execution_report["react_limit_reached"] = execution_report.get("react_limit_reached", "rounds")
    final_messages = [
        *base_messages,
        {
            "role": "user",
            "content": (
                "工具调用已达到本节点上限。下面是最后一轮工具摘要：\n"
                f"{last_tool_summary}\n\n"
                "现在必须立即输出本节点的正式最终结果，不得再次调用工具。"
                "严格遵守原始输出契约；审查节点必须包含审查标记和审查判定数据 JSON。"
            ),
        },
    ]
    try:
        final_result = await call_deepseek(final_messages, tools=None, budget=budget)
        if final_result.get("content"):
            if react_enabled and execution_report is not None:
                execution_report["react_finalized"] = True
            return final_result["content"]
    except BudgetExceededError:
        raise
    except Exception as exc:
        logger.warning("工具轮次耗尽后请求最终报告失败: %s", exc)
    return (
        "工具调用轮次已达上限，模型未输出最终结论。"
        "请基于已有摘要直接输出符合输出契约的最终报告。"
    )


def _summarize_react_value(value: Any, limit: int = 1200) -> str:
    """限制 ReAct Trace 中参数的大小，避免把完整源码写入运行轨迹。"""
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else f"{text[:limit]}...（已截断）"


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
