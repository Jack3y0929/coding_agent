import asyncio
from unittest.mock import patch

from backend.budget import TokenBudget
from backend.graph import nodes


def test_fc_loop_returns_final_model_text() -> None:
    async def scenario() -> None:
        with patch.object(nodes, "call_deepseek") as call:
            call.return_value = {"content": "[审查通过]", "tool_calls": []}
            result = await nodes.run_fc_loop(
                [{"role": "user", "content": "x"}],
                [],
                TokenBudget(),
                "plan",
                "",
            )
        assert result == "[审查通过]"

    asyncio.run(scenario())


def test_fc_loop_does_not_return_tool_summary_on_limit() -> None:
    async def scenario() -> None:
        tool_call = {"function": {"name": "read_file", "arguments": "{}"}}
        with patch.object(nodes, "call_deepseek") as call, patch.object(nodes, "execute_tool") as tool:
            call.return_value = {"content": "checking", "tool_calls": [tool_call]}
            tool.return_value = "工具 read_file 返回：raw content"
            result = await nodes.run_fc_loop(
                [{"role": "user", "content": "x"}],
                [],
                TokenBudget(),
                "plan",
                "",
            )
        assert result == "工具调用轮次已达上限，模型未输出最终结论。请基于已有摘要直接输出符合输出契约的最终报告。"
        assert "raw content" not in result

    asyncio.run(scenario())
