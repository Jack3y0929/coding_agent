from pathlib import Path

from backend.graph.nodes import FC_TOOLS
from backend.skills.loader import discover_skills
from backend.skills.runtime import restrict_tool_names
from backend.tools.models import normalize_tool_result
from backend.tools.models import ToolResult
from backend.tools.recovery import record_tool_failure
from backend.budget import TokenBudget
from backend.graph import nodes
from unittest.mock import patch
import asyncio


def test_builtin_skills_are_loadable() -> None:
    root = Path(__file__).resolve().parents[1] / "skills"
    skills = discover_skills(root, {item["function"]["name"] for item in FC_TOOLS})
    ids = {skill.id for skill in skills}
    assert {"coding-intent", "debug-diagnosis", "code-development", "code-review"} <= ids


def test_skill_tools_are_restricted_by_plan_mode() -> None:
    root = Path(__file__).resolve().parents[1] / "skills" / "code-development"
    from backend.skills.loader import load_skill

    skill = load_skill(root, {item["function"]["name"] for item in FC_TOOLS})
    tools = restrict_tool_names(skill, "plan", FC_TOOLS)
    assert {item["function"]["name"] for item in tools} == {
        "read_file", "search_code", "save_checkpoint"
    }


def test_tool_error_is_structured_and_retryable() -> None:
    result = normalize_tool_result("错误: 文件不存在 — src/missing.py", "read_file")
    assert result.ok is False
    assert result.code == "file_not_found"
    assert result.retryable is True


def test_tool_failure_policy_exhausts_non_retryable_errors() -> None:
    report: dict = {}
    result = ToolResult(
        ok=False,
        code="command_not_allowed",
        message="命令被拒绝",
        retryable=False,
        tool_name="execute_shell",
    )
    assert record_tool_failure(report, result, {"command": "rm"}, 0) is False
    assert report["tool_failure_exhausted"] is True
    assert report["last_tool_error"]["code"] == "command_not_allowed"


def test_fc_loop_allows_one_retry_after_recoverable_tool_error() -> None:
    async def scenario() -> None:
        call = {
            "function": {"name": "read_file", "arguments": '{"path":"missing.py"}'}
        }
        with patch.object(nodes, "call_deepseek") as model, patch.object(nodes, "execute_tool") as tool:
            model.side_effect = [
                {"content": "", "tool_calls": [call]},
                {"content": "已修正", "tool_calls": []},
            ]
            tool.return_value = "错误: 文件不存在 — missing.py"
            result = await nodes.run_fc_loop(
                [{"role": "user", "content": "x"}], [], TokenBudget(), "plan", ""
            )
        assert result == "已修正"
        assert model.call_count == 2

    asyncio.run(scenario())
