"""Classic tool bindings over shared sandbox skill resources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from _agents_shared.sandbox_skills import (
    LazySkillSource as LazySkillSource,
    LocalDirLazySkillSource as LocalDirLazySkillSource,
    Skill as Skill,
    SkillMetadata as SkillMetadata,
    SkillSet as _SkillSet,
)

from ...tool import FunctionTool, Tool
from .capability import Capability


class _LoadSkillArgs(BaseModel):
    skill_name: str


@dataclass(init=False)
class _LoadSkillTool(FunctionTool):
    tool_name = "load_skill"
    args_model = _LoadSkillArgs
    tool_description = (
        "Load a single lazily configured skill into the sandbox so its SKILL.md, scripts, "
        "references, and assets can be read from the workspace."
    )
    skills: Skills = field(init=False, repr=False, compare=False)

    def __init__(self, *, skills: Skills) -> None:
        self.skills = skills
        super().__init__(
            name=self.tool_name,
            description=self.tool_description,
            params_json_schema=self.args_model.model_json_schema(),
            on_invoke_tool=self._invoke,
            strict_json_schema=False,
        )

    async def _invoke(self, _: object, raw_input: str) -> dict[str, str]:
        return await self.run(self.args_model.model_validate_json(raw_input))

    async def run(self, args: _LoadSkillArgs) -> dict[str, str]:
        return await self.skills.load_skill(args.skill_name)


class Skills(_SkillSet, Capability):
    type: Literal["skills"] = "skills"

    def tools(self) -> list[Tool]:
        if self.lazy_from is None:
            return []
        if self.session is None:
            raise ValueError(f"{type(self).__name__} is not bound to a SandboxSession")
        return [_LoadSkillTool(skills=self)]
