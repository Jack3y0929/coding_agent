"""Skill 的机器可读模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SkillTriggers(BaseModel):
    task_types: list[str] = Field(default_factory=list)
    stages: list[str] = Field(default_factory=list)


class SkillManifest(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    name: str
    description: str = ""
    triggers: SkillTriggers = Field(default_factory=SkillTriggers)
    allowed_tools: list[str] = Field(default_factory=list)
    output_contract: str = ""
    risk_level: str = "medium"
    priority: int = 0
    enabled: bool = True
    prompt_file: str = "SKILL.md"


class LoadedSkill(BaseModel):
    manifest: SkillManifest
    root_path: str
    prompt: str
    content_hash: str
    status: str = "loaded"
    load_error: str | None = None

    @property
    def id(self) -> str:
        return self.manifest.id

    @property
    def version(self) -> str:
        return self.manifest.version

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.manifest.model_dump(),
            "root_path": self.root_path,
            "content_hash": self.content_hash,
            "status": self.status,
            "load_error": self.load_error,
        }
