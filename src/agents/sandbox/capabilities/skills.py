from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..entries import BaseEntry, Dir, File, LocalFile
from ..errors import SkillsConfigError
from ..manifest import Manifest
from .capability import Capability

_SKILLS_ROOT = Path(".agents/skills")


def _validate_relative_path(
    value: str | Path,
    *,
    field_name: str,
    context: Mapping[str, object] | None = None,
) -> Path:
    rel = value if isinstance(value, Path) else Path(value)
    if rel.is_absolute():
        raise SkillsConfigError(
            message=f"{field_name} must be a relative path",
            context={
                "field": field_name,
                "path": str(rel),
                "reason": "absolute",
                **(context or {}),
            },
        )
    if ".." in rel.parts:
        raise SkillsConfigError(
            message=f"{field_name} must not escape the skills root",
            context={
                "field": field_name,
                "path": str(rel),
                "reason": "escape_root",
                **(context or {}),
            },
        )
    if rel.parts in [(), (".",)]:
        raise SkillsConfigError(
            message=f"{field_name} must be non-empty",
            context={"field": field_name, "path": str(rel), "reason": "empty", **(context or {})},
        )
    return rel


def _manifest_entry_paths(manifest: Manifest) -> set[Path]:
    return {key if isinstance(key, Path) else Path(key) for key in manifest.entries}


def _get_manifest_entry_by_path(manifest: Manifest, path: Path) -> BaseEntry | None:
    for key, entry in manifest.entries.items():
        normalized = key if isinstance(key, Path) else Path(key)
        if normalized == path:
            return entry
    return None


class Skill(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    content: str | bytes | BaseEntry

    compatibility: str | None = Field(default=None)
    scripts: dict[str | Path, BaseEntry] = Field(default_factory=dict)
    references: dict[str | Path, BaseEntry] = Field(default_factory=dict)
    assets: dict[str | Path, BaseEntry] = Field(default_factory=dict)

    deferred: bool = Field(default=False)

    @field_validator("content", mode="before")
    @classmethod
    def _parse_content(cls, value: object) -> object:
        if isinstance(value, Mapping):
            return BaseEntry.parse(value)
        return value

    @field_validator("scripts", "references", "assets", mode="before")
    @classmethod
    def _parse_entry_map(cls, value: object) -> dict[str | Path, BaseEntry]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"Artifact mapping must be a mapping, got {type(value).__name__}")
        return {key: BaseEntry.parse(entry) for key, entry in value.items()}

    def model_post_init(self, context: Any, /) -> None:
        _ = context
        skill_context: dict[str, object] = {"skill_name": self.name}
        _validate_relative_path(self.name, field_name="name", context=skill_context)

        content_artifact = self.content_artifact()
        if not isinstance(content_artifact, (File, LocalFile)):
            raise SkillsConfigError(
                message="skill content must be file-like",
                context={
                    "field": "content",
                    "skill_name": self.name,
                    "content_type": content_artifact.type,
                },
            )

        self.scripts = self._normalize_entry_map(self.scripts, field_name="scripts")
        self.references = self._normalize_entry_map(self.references, field_name="references")
        self.assets = self._normalize_entry_map(self.assets, field_name="assets")

    def _normalize_entry_map(
        self,
        entries: Mapping[str | Path, BaseEntry],
        *,
        field_name: str,
    ) -> dict[str | Path, BaseEntry]:
        normalized: dict[str | Path, BaseEntry] = {}
        seen_paths: set[str] = set()
        for key, artifact in entries.items():
            rel = _validate_relative_path(
                key,
                field_name=field_name,
                context={"skill_name": self.name, "entry_path": str(key)},
            )
            rel_str = rel.as_posix()
            if rel_str in seen_paths:
                raise SkillsConfigError(
                    message=f"duplicate entry path in skill {field_name}",
                    context={
                        "skill_name": self.name,
                        "field": field_name,
                        "entry_path": rel_str,
                    },
                )
            seen_paths.add(rel_str)
            normalized[rel_str] = artifact

        return normalized

    def content_artifact(self) -> BaseEntry:
        if isinstance(self.content, bytes):
            return File(content=self.content)
        if isinstance(self.content, str):
            return File(content=self.content.encode("utf-8"))
        return self.content

    def as_dir_entry(self) -> Dir:
        children: dict[str | Path, BaseEntry] = {"SKILL.md": self.content_artifact()}
        if self.scripts:
            children["scripts"] = Dir(children=self.scripts)
        if self.references:
            children["references"] = Dir(children=self.references)
        if self.assets:
            children["assets"] = Dir(children=self.assets)
        return Dir(children=children)


class Skills(Capability):
    """Mount skills into a Codex auto-discovery root inside the sandbox."""

    type: str = "skills"
    skills: list[Skill]
    from_: BaseEntry | None

    def __init__(
        self,
        *,
        skills: Sequence[Skill | Mapping[str, object]] | None = None,
        from_: BaseEntry | Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(type="skills")
        self.skills = [self._coerce_skill(skill) for skill in (skills or [])]
        self.from_ = self._coerce_entry(from_)
        self._validate()

    @staticmethod
    def _coerce_skill(skill: Skill | Mapping[str, object]) -> Skill:
        if isinstance(skill, Skill):
            return skill
        return Skill.model_validate(dict(skill))

    @staticmethod
    def _coerce_entry(entry: BaseEntry | Mapping[str, object] | None) -> BaseEntry | None:
        if entry is None or isinstance(entry, BaseEntry):
            return entry
        return BaseEntry.parse(entry)

    def _validate(self) -> None:
        if not self.skills and self.from_ is None:
            raise SkillsConfigError(
                message="skills capability requires `skills` or `from_`",
                context={"field": "skills"},
            )
        if self.skills and self.from_ is not None:
            raise SkillsConfigError(
                message="skills capability does not allow both `skills` and `from_` together",
                context={"field": "skills", "has_from": True},
            )

        if self.from_ is not None and not self.from_.is_dir:
            raise SkillsConfigError(
                message="`from_` must be a directory-like artifact",
                context={"field": "from_", "artifact_type": self.from_.type},
            )

        seen_names: set[Path] = set()
        for skill in self.skills:
            rel = _validate_relative_path(
                skill.name,
                field_name="skills[].name",
                context={"skill_name": skill.name},
            )
            if rel in seen_names:
                raise SkillsConfigError(
                    message=f"duplicate skill name: {skill.name}",
                    context={"field": "skills[].name", "skill_name": skill.name},
                )
            seen_names.add(rel)

    def process_manifest(self, manifest: Manifest) -> Manifest:
        skills_root = _SKILLS_ROOT
        existing_paths = _manifest_entry_paths(manifest)

        if self.from_ is not None:
            if skills_root in existing_paths:
                existing_entry = _get_manifest_entry_by_path(manifest, skills_root)
                if existing_entry is None:
                    raise SkillsConfigError(
                        message="skills root path lookup failed",
                        context={"path": str(skills_root), "source": "from_"},
                    )
                if existing_entry.is_dir:
                    return manifest
                raise SkillsConfigError(
                    message="skills root path already exists in manifest",
                    context={
                        "path": str(skills_root),
                        "source": "from_",
                        "existing_type": existing_entry.type,
                    },
                )
            manifest.entries[skills_root] = self.from_
            existing_paths.add(skills_root)

        for skill in self.skills:
            relative_path = skills_root / Path(skill.name)
            rendered_skill = skill.as_dir_entry()
            if relative_path in existing_paths:
                existing_entry = _get_manifest_entry_by_path(manifest, relative_path)
                if existing_entry is None:
                    raise SkillsConfigError(
                        message="skill path lookup failed",
                        context={"path": str(relative_path), "skill_name": skill.name},
                    )
                if existing_entry == rendered_skill:
                    continue
                raise SkillsConfigError(
                    message="skill path already exists in manifest",
                    context={"path": str(relative_path), "skill_name": skill.name},
                )
            manifest.entries[relative_path] = rendered_skill
            existing_paths.add(relative_path)

        return manifest


__all__ = ["Skill", "Skills"]
