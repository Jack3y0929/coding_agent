"""从受控目录发现并加载 Skill。"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from backend.skills.models import LoadedSkill, SkillManifest

logger = logging.getLogger("skills.loader")


def load_skill(skill_dir: Path, allowed_tools: set[str]) -> LoadedSkill:
    """加载单个 Skill，并校验其工具声明和路径。"""
    manifest_path = skill_dir / "skill.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = SkillManifest.model_validate(raw)
    unknown = sorted(set(manifest.allowed_tools) - allowed_tools)
    if unknown:
        raise ValueError(f"声明了未知工具: {', '.join(unknown)}")
    prompt_path = (skill_dir / manifest.prompt_file).resolve()
    if skill_dir.resolve() not in prompt_path.parents:
        raise ValueError("Prompt 路径逃逸 Skill 目录")
    prompt = prompt_path.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("Skill Prompt 为空")
    digest = hashlib.sha256(
        (manifest_path.read_bytes() + prompt_path.read_bytes()).replace(b"\r\n", b"\n")
    ).hexdigest()
    return LoadedSkill(
        manifest=manifest,
        root_path=str(skill_dir.resolve()),
        prompt=prompt,
        content_hash=digest,
    )


def discover_skills(root: Path, allowed_tools: set[str]) -> list[LoadedSkill]:
    """发现目录下所有合法 Skill；单个 Skill 失败不阻断服务启动。"""
    if not root.is_dir():
        return []
    skills: list[LoadedSkill] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            skills.append(load_skill(child, allowed_tools))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Skill 加载失败 %s: %s", child, exc)
    return skills
