import asyncio
from unittest.mock import patch

from backend.budget import TokenBudget
from backend.graph import nodes
from backend.graph.dev_workflow import (
    route_after_build,
    route_after_fix,
    _changes_from_execution,
    _parse_implementation_note,
)


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


def test_fc_loop_tracks_only_successful_file_writes() -> None:
    async def scenario() -> None:
        tool_call = {
            "function": {
                "name": "write_file",
                "arguments": '{"path":"src/app.py","content":"print(1)"}',
            }
        }
        report: dict = {"successful_writes": [], "failed_writes": [], "tool_calls": []}
        with patch.object(nodes, "call_deepseek") as call, patch.object(nodes, "execute_tool") as tool:
            call.side_effect = [
                {"content": "", "tool_calls": [tool_call]},
                {"content": "## 实现说明\n已写入", "tool_calls": []},
            ]
            async def fake_execute(*args):
                args[-1]["successful_writes"].append({
                    "path": "src/app.py", "content": "print(1)",
                    "result": "写入成功: src/app.py (8 字符)",
                })
                return "写入成功: src/app.py (8 字符)"

            tool.side_effect = fake_execute
            result = await nodes.run_fc_loop(
                [{"role": "user", "content": "x"}],
                [], TokenBudget(), "build", "", report,
            )
        assert "已写入" in result
        assert report["successful_writes"] == [{
            "path": "src/app.py", "content": "print(1)",
            "result": "写入成功: src/app.py (8 字符)",
        }]

    asyncio.run(scenario())


def test_developer_text_is_not_a_code_change_and_build_gate_blocks_empty_output() -> None:
    assert _parse_implementation_note("## 代码变更\n只是说明\n## 实现说明\n原因") == "原因"
    assert _changes_from_execution({"successful_writes": []}) == {}
    assert route_after_build({
        "build_execution": {"successful_writes": []},
        "build_attempt": 1,
    }) == "develop_build"
    assert route_after_build({
        "build_execution": {"successful_writes": []},
        "build_attempt": 3,
    }) == "human_intervene"
    assert route_after_build({
        "build_execution": {"successful_writes": [{"path": "a.py"}]},
        "build_attempt": 1,
    }) == "validate"
    assert route_after_fix({
        "fix_execution": {"successful_writes": []},
        "fix_attempt": 1,
    }) == "fix"
