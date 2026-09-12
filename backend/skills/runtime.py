"""将 Skill 注入角色 Prompt 并限制可用工具。"""

from __future__ import annotations

from typing import Any

from backend.skills.models import LoadedSkill


def compose_prompt(role_prompt: str, skill: LoadedSkill | None) -> str:
    if skill is None:
        return role_prompt
    return (
        f"{role_prompt}\n\n"
        f"--- 当前 Skill: {skill.manifest.name} v{skill.version} ---\n"
        f"{skill.prompt}\n"
        "--- Skill 结束 ---"
    )


def restrict_tool_names(skill: LoadedSkill | None, mode: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Skill 权限与 Plan/Build 模式取交集。"""
    allowed = set(skill.manifest.allowed_tools) if skill else {item["function"]["name"] for item in tools}
    if mode == "plan":
        allowed -= {"write_file", "execute_shell", "add_logging"}
    return [item for item in tools if item["function"]["name"] in allowed]
