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
            call.side_effect = [
                *({"content": "checking", "tool_calls": [tool_call]} for _ in range(10)),
                {"content": "正式最终报告", "tool_calls": []},
            ]
            tool.return_value = "工具 read_file 返回：raw content"
            result = await nodes.run_fc_loop(
                [{"role": "user", "content": "x"}],
                [],
                TokenBudget(),
                "plan",
                "",
            )
        assert result == "正式最终报告"
        assert "raw content" not in result
        assert call.call_count == 11

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


def test_react_plan_continues_until_checkpoint_then_finalizes() -> None:
    async def scenario() -> None:
        read_call = {"function": {"name": "read_file", "arguments": '{"path":"src/app.py"}'}}
        checkpoint_call = {"function": {"name": "save_checkpoint", "arguments": '{"reason":"方案已完成"}'}}
        report: dict = {}
        with patch.object(nodes, "call_deepseek") as call, patch.object(nodes, "execute_tool") as tool:
            call.side_effect = [
                {"content": "先核对源码", "tool_calls": [read_call]},
                {"content": "证据已足够", "tool_calls": [checkpoint_call]},
                {"content": "## 开发方案\n涉及文件与验证", "tool_calls": []},
            ]
            tool.side_effect = ["读取成功: src/app.py", "checkpoint已保存"]
            result = await nodes.run_fc_loop(
                [{"role": "user", "content": "x"}],
                [],
                TokenBudget(),
                "plan",
                "",
                report,
                react_enabled=True,
                react_policy=nodes.PLAN_REACT_POLICY,
            )
        assert "涉及文件与验证" in result
        assert call.call_count == 3
        assert report["react_stop_requested"] is True
        assert report["react_finalized"] is True
        assert len(report["react_steps"]) == 2

    asyncio.run(scenario())


def test_react_build_can_validate_after_write() -> None:
    async def scenario() -> None:
        write_call = {
            "function": {
                "name": "write_file",
                "arguments": '{"path":"src/app.py","content":"print(1)"}',
            }
        }
        shell_call = {"function": {"name": "execute_shell", "arguments": '{"command":"pytest"}'}}
        report: dict = {"successful_writes": [], "failed_writes": [], "tool_calls": []}
        with patch.object(nodes, "call_deepseek") as call, patch.object(nodes, "execute_tool") as tool:
            call.side_effect = [
                {"content": "写入实现", "tool_calls": [write_call]},
                {"content": "继续验证", "tool_calls": [shell_call]},
                {"content": "## 代码变更\n已完成\n## 实现说明\n验证通过", "tool_calls": []},
            ]

            async def fake_execute(tool_name, arguments, mode, project_path, execution_report):
                execution_report["tool_calls"].append(tool_name)
                if tool_name == "write_file":
                    execution_report["successful_writes"].append({"path": "src/app.py", "content": "print(1)"})
                return "验证通过" if tool_name == "execute_shell" else "写入成功: src/app.py"

            tool.side_effect = fake_execute
            result = await nodes.run_fc_loop(
                [{"role": "user", "content": "x"}],
                [],
                TokenBudget(),
                "build",
                "",
                report,
                react_enabled=True,
                react_policy=nodes.BUILD_REACT_POLICY,
            )
        assert "验证通过" in result
        assert call.call_count == 3
        assert report["tool_calls"] == ["write_file", "execute_shell"]

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
