"""运行时 Skill 注册表。"""

from __future__ import annotations

import logging
import json
from pathlib import Path

from backend.config import PROJECT_ROOT
from backend.skills.loader import discover_skills, load_skill
from backend.skills.models import LoadedSkill

logger = logging.getLogger("skills.registry")


class SkillRegistry:
    """按 Skill ID 管理当前激活版本。"""

    def __init__(self) -> None:
        self._skills: dict[str, LoadedSkill] = {}
        self._initialized = False

    def load_from(self, root: Path, allowed_tools: set[str]) -> int:
        loaded = discover_skills(root, allowed_tools)
        state = _read_state(root.parent / ".skill_registry.json")
        for skill in loaded:
            if skill.id in state:
                skill.manifest.enabled = bool(state[skill.id])
                skill.status = "loaded" if skill.manifest.enabled else "disabled"
            current = self._skills.get(skill.id)
            if current is None or _version_key(skill.version) >= _version_key(current.version):
                self._skills[skill.id] = skill
        logger.info("已加载 %d 个 Skill", len(self._skills))
        self._initialized = True
        return len(loaded)

    def register(self, skill: LoadedSkill) -> None:
        self._skills[skill.id] = skill

    def register_from(self, skill_dir: Path, allowed_tools: set[str]) -> LoadedSkill:
        skill = load_skill(skill_dir, allowed_tools)
        self.register(skill)
        return skill

    def get(self, skill_id: str) -> LoadedSkill | None:
        skill = self._skills.get(skill_id)
        return skill if skill and skill.manifest.enabled else None

    def set_enabled(self, skill_id: str, enabled: bool) -> bool:
        skill = self._skills.get(skill_id)
        if skill is None:
            return False
        skill.manifest.enabled = enabled
        skill.status = "loaded" if enabled else "disabled"
        _write_state(Path(PROJECT_ROOT) / ".skill_registry.json", {
            item.id: item.manifest.enabled for item in self._skills.values()
        })
        return True

    def select(self, task_type: str, stage: str) -> LoadedSkill | None:
        candidates = [
            skill for skill in self._skills.values()
            if skill.manifest.enabled
            and (not skill.manifest.triggers.task_types or task_type in skill.manifest.triggers.task_types)
            and (not skill.manifest.triggers.stages or stage in skill.manifest.triggers.stages)
        ]
        return max(candidates, key=lambda item: item.manifest.priority, default=None)

    def list(self) -> list[dict[str, object]]:
        return [skill.as_dict() for skill in sorted(self._skills.values(), key=lambda item: item.id)]


_REGISTRY = SkillRegistry()


def _version_key(version: str) -> tuple[int, int, int]:
    parts = [int(part) for part in version.split(".")]
    return (parts[0], parts[1], parts[2])


def _read_state(path: Path) -> dict[str, bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): bool(value) for key, value in payload.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _write_state(path: Path, state: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def get_skill_registry() -> SkillRegistry:
    return _REGISTRY


def initialize_skill_registry(allowed_tools: set[str]) -> int:
    if _REGISTRY._initialized:
        return len(_REGISTRY._skills)
    return _REGISTRY.load_from(Path(PROJECT_ROOT) / "skills", allowed_tools)
