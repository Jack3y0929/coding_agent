import asyncio

from backend.graph.nodes import execute_tool


def test_write_tool_resolves_project_relative_path_once(tmp_path) -> None:
    async def scenario() -> None:
        project = tmp_path / "project"
        project.mkdir()
        result = await execute_tool(
            "write_file",
            {"path": "app.py", "content": "value = 1\n"},
            "build",
            str(project),
        )
        assert result.startswith("写入成功")
        assert (project / "app.py").read_text(encoding="utf-8") == "value = 1\n"
        assert not (project / "project" / "app.py").exists()

    asyncio.run(scenario())
