from __future__ import annotations

import abc
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from ...tool import FunctionTool, Tool
from ..entries import BaseEntry, Dir, File, LocalDir, LocalFile
from ..errors import SkillsConfigError
from ..manifest import Manifest
from ..session.base_sandbox_session import BaseSandboxSession
from ..types import User
from ..workspace_paths import coerce_posix_path, posix_path_as_path, windows_absolute_path
from .capability import Capability

_SKILLS_SECTION_INTRO = (
    "A skill is a set of local instructions to follow that is stored in a `SKILL.md` file. "
    "Below is the list of skills that can be used. Each entry includes a name, description, "
    "and file path so you can open the source for full instructions when using a specific skill."
)

_HOW_TO_USE_SKILLS_SECTION = "\n".join(
    [
        "### How to use skills",
        "- Discovery: The list above is the skills available in this session "
        "(name + description + file path). Skill bodies live on disk at the listed paths.",
        "- Trigger rules: If the user names a skill (with `$SkillName` or plain text) "
        "OR the task clearly matches a skill's description shown above, you must use that "
        "skill for that turn. Multiple mentions mean use them all. Do not carry skills "
        "across turns unless re-mentioned.",
        "- Missing/blocked: If a named skill isn't in the list or the path can't be read, "
        "say so briefly and continue with the best fallback.",
        "- How to use a skill (progressive disclosure):",
        "  1) After deciding to use a skill, open its `SKILL.md`. Read only enough to "
        "follow the workflow.",
        "  2) If `SKILL.md` points to extra folders such as `references/`, load only the "
        "specific files needed for the request; don't bulk-load everything.",
        "  3) If `scripts/` exist, prefer running or patching them instead of retyping "
        "large code blocks.",
        "  4) If `assets/` or templates exist, reuse them instead of recreating from scratch.",
        "- Coordination and sequencing:",
        "  - If multiple skills apply, choose the minimal set that covers the request "
        "and state the order you'll use them.",
        "  - Announce which skill(s) you're using and why (one short line). "
        "If you skip an obvious skill, say why.",
        "- Context hygiene:",
        "  - Keep context small: summarize long sections instead of pasting them; "
        "only load extra files when needed.",
        "  - Avoid deep reference-chasing: prefer opening only files directly linked "
        "from `SKILL.md` unless you're blocked.",
        "  - When variants exist (frameworks, providers, domains), pick only the relevant "
        "reference file(s) and note that choice.",
        "- Safety and fallback: If a skill can't be applied cleanly (missing files, "
        "unclear instructions), state the issue, pick the next-best approach, and continue.",
    ]
)

_HOW_TO_USE_LAZY_SKILLS_SECTION = "\n".join(
    [
        "### How to use skills",
        "- Discovery: The list above is the skill index available in this session "
        "(name + description + workspace path). In lazy mode, those paths are loaded "
        "on demand instead of being present up front.",
        "- Trigger rules: If the user names a skill (with `$SkillName` or plain text) "
        "OR the task clearly matches a skill's description shown above, you must use that "
        "skill for that turn. Multiple mentions mean use them all. Do not carry skills "
        "across turns unless re-mentioned.",
        "- Missing/blocked: If a named skill isn't in the list or the path can't be read, "
        "say so briefly and continue with the best fallback.",
        "- How to use a skill (progressive disclosure):",
        "  1) After deciding to use a lazy skill, call `load_skill` for that skill first, "
        "then open its `SKILL.md`.",
        "  2) If `SKILL.md` points to extra folders such as `references/`, load only the "
        "specific files needed for the request; don't bulk-load everything.",
        "  3) If `scripts/` exist, prefer running or patching them instead of retyping "
        "large code blocks.",
        "  4) If `assets/` or templates exist, reuse them instead of recreating from scratch.",
        "- Coordination and sequencing:",
        "  - If multiple skills apply, choose the minimal set that covers the request "
        "and state the order you'll use them.",
        "  - Announce which skill(s) you're using and why (one short line). "
        "If you skip an obvious skill, say why.",
        "- Context hygiene:",
        "  - Keep context small: summarize long sections instead of pasting them; "
        "only load extra files when needed.",
        "  - Avoid deep reference-chasing: prefer opening only files directly linked "
        "from `SKILL.md` unless you're blocked.",
        "  - When variants exist (frameworks, providers, domains), pick only the relevant "
        "reference file(s) and note that choice.",
        "- Safety and fallback: If a skill can't be applied cleanly (missing files, "
        "unclear instructions), state the issue, pick the next-best approach, and continue.",
    ]
)


@dataclass(frozen=True)
class SkillMetadata:
    """Indexed metadata for a skill that can be rendered into instructions."""

    name: str
    description: str
    path: Path


class LazySkillSource(BaseModel, abc.ABC):
    """Source of skill metadata and on-demand skill materialization."""

    @abc.abstractmethod
    def list_skill_metadata(self, *, skills_path: str) -> list[SkillMetadata]: ...

    @abc.abstractmethod
    async def load_skill(
        self,
        *,
        skill_name: str,
        session: BaseSandboxSession,
        skills_path: str,
        user: str | User | None = None,
    ) -> dict[str, str]: ...


class LocalDirLazySkillSource(LazySkillSource):
    """Load skills lazily from a local directory on the host filesystem."""

    source: LocalDir

    def _src_root(self) -> Path | None:
        if self.source.src is None:
            return None
        src_root = (Path.cwd() / self.source.src).resolve()
        if not src_root.exists() or not src_root.is_dir():
            return None
        return src_root

    def list_skill_metadata(self, *, skills_path: str) -> list[SkillMetadata]:
        src_root = self._src_root()
        if src_root is None:
            return []

        metadata: list[SkillMetadata] = []
        for child in sorted(src_root.iterdir(), key=lambda entry: entry.name):
            if not child.is_dir():
                continue
            skill_md_path = child / "SKILL.md"
            if not skill_md_path.is_file():
                continue
            try:
                markdown = skill_md_path.read_text(encoding="utf-8")
            except OSError:
                continue
            frontmatter = _parse_frontmatter(markdown)
            metadata.append(
                SkillMetadata(
                    name=frontmatter.get("name", child.name),
                    description=frontmatter.get("description", "No description provided."),
                    path=Path(skills_path) / child.name,
                )
            )
        return metadata

    async def load_skill(
        self,
        *,
        skill_name: str,
        session: BaseSandboxSession,
        skills_path: str,
        user: str | User | None = None,
    ) -> dict[str, str]:
        src_root = self._src_root()
        if src_root is None:
            raise SkillsConfigError(
                message="lazy skill source directory is unavailable",
                context={"skill_name": skill_name},
            )

        matches = [
            skill
            for skill in self.list_skill_metadata(skills_path=skills_path)
            if skill.name == skill_name or skill.path.name == skill_name
        ]
        if not matches:
            raise SkillsConfigError(
                message="lazy skill not found",
                context={"skill_name": skill_name, "skills_path": skills_path},
            )
        if len(matches) > 1:
            raise SkillsConfigError(
                message="lazy skill name is ambiguous",
                context={
                    "skill_name": skill_name,
                    "matching_paths": [str(skill.path) for skill in matches],
                },
            )
        metadata = matches[0]

        workspace_root = Path(session.state.manifest.root)
        skill_dest = workspace_root / metadata.path
        skill_md_path = skill_dest / "SKILL.md"
        try:
            handle = await session.read(skill_md_path, user=user)
        except Exception:
            handle = None
        if handle is not None:
            handle.close()
            return {
                "status": "already_loaded",
                "skill_name": metadata.name,
                "path": str(metadata.path).replace("\\", "/"),
            }

        await LocalDir(src=src_root / metadata.path.name).apply(
            session,
            skill_dest,
            base_dir=Path.cwd(),
            user=user,
        )
        return {
            "status": "loaded",
            "skill_name": metadata.name,
            "path": str(metadata.path).replace("\\", "/"),
        }


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


def _validate_relative_path(
    value: str | Path,
    *,
    field_name: str,
    context: Mapping[str, object] | None = None,
) -> Path:
    if (windows_path := windows_absolute_path(value)) is not None:
        raise SkillsConfigError(
            message=f"{field_name} must be a relative path",
            context={
                "field": field_name,
                "path": windows_path.as_posix(),
                "reason": "absolute",
                **(context or {}),
            },
        )
    rel_posix = coerce_posix_path(value)
    if rel_posix.is_absolute():
        raise SkillsConfigError(
            message=f"{field_name} must be a relative path",
            context={
                "field": field_name,
                "path": rel_posix.as_posix(),
                "reason": "absolute",
                **(context or {}),
            },
        )
    if ".." in rel_posix.parts:
        raise SkillsConfigError(
            message=f"{field_name} must not escape the skills root",
            context={
                "field": field_name,
                "path": rel_posix.as_posix(),
                "reason": "escape_root",
                **(context or {}),
            },
        )
    if rel_posix.parts in [(), (".",)]:
        raise SkillsConfigError(
            message=f"{field_name} must be non-empty",
            context={
                "field": field_name,
                "path": rel_posix.as_posix(),
                "reason": "empty",
                **(context or {}),
            },
        )
    return posix_path_as_path(rel_posix)


def _manifest_entry_paths(manifest: Manifest) -> set[Path]:
    return {posix_path_as_path(coerce_posix_path(key)) for key in manifest.entries}


def _get_manifest_entry_by_path(manifest: Manifest, path: Path) -> BaseEntry | None:
    path = posix_path_as_path(coerce_posix_path(path))
    for key, entry in manifest.entries.items():
        normalized = posix_path_as_path(coerce_posix_path(key))
        if normalized == path:
            return entry
    return None


def _parse_frontmatter(markdown: str) -> dict[str, str]:
    """Parse the simple YAML frontmatter shape used by skill indexes."""

    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    end_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}

    metadata: dict[str, str] = {}
    for line in lines[1:end_index]:
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        parsed_key = key.strip()
        parsed_value = value.strip()
        if (
            len(parsed_value) >= 2
            and parsed_value[0] == parsed_value[-1]
            and parsed_value[0] in {"'", '"'}
        ):
            parsed_value = parsed_value[1:-1]
        metadata[parsed_key] = parsed_value
    return metadata


def _read_text(handle: io.IOBase) -> str:
    """Normalize sandbox file reads into text for metadata extraction."""

    payload = handle.read()
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes | bytearray):
        return bytes(payload).decode("utf-8", errors="replace")
    return str(payload)


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
        skill_context = {"skill_name": self.name}
        _validate_relative_path(self.name, field_name="name", context=skill_context)

        content_artifact = self.content_artifact()
        if not isinstance(content_artifact, File | LocalFile):
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

    type: Literal["skills"] = "skills"
    skills: list[Skill] = Field(default_factory=list)
    from_: BaseEntry | None = Field(default=None)
    lazy_from: LazySkillSource | None = Field(default=None)
    skills_path: str = Field(default=".agents")

    _skills_metadata: list[SkillMetadata] | None = PrivateAttr(default=None)

    @field_validator("skills", mode="before")
    @classmethod
    def _coerce_skills(
        cls,
        value: Sequence[Skill | Mapping[str, object]] | None,
    ) -> list[Skill]:
        if value is None:
            return []
        return [
            skill if isinstance(skill, Skill) else Skill.model_validate(dict(skill))
            for skill in value
        ]

    @field_validator("from_", mode="before")
    @classmethod
    def _coerce_entry(
        cls,
        entry: BaseEntry | Mapping[str, object] | None,
    ) -> BaseEntry | None:
        if entry is None or isinstance(entry, BaseEntry):
            return entry
        return BaseEntry.parse(entry)

    def model_post_init(self, context: Any, /) -> None:
        _ = context
        skills_root = _validate_relative_path(self.skills_path, field_name="skills_path")
        self.skills_path = str(skills_root)

        if not self.skills and self.from_ is None and self.lazy_from is None:
            raise SkillsConfigError(
                message="skills capability requires `skills`, `from_`, or `lazy_from`",
                context={"field": "skills"},
            )

        configured_sources = sum(
            1
            for has_source in (
                bool(self.skills),
                self.from_ is not None,
                self.lazy_from is not None,
            )
            if has_source
        )
        if configured_sources > 1:
            raise SkillsConfigError(
                message="skills capability accepts only one of `skills`, `from_`, or `lazy_from`",
                context={"field": "skills", "has_from": self.from_ is not None},
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
        skills_root = posix_path_as_path(coerce_posix_path(self.skills_path))
        existing_paths = _manifest_entry_paths(manifest)

        if self.lazy_from:
            # Lazy sources do not claim `skills_root` in the manifest up front, so reserve the
            # whole namespace here and fail fast if any existing manifest entry is equal to,
            # above, or below that path.
            overlaps = sorted(
                str(path)
                for path in existing_paths
                if path == skills_root or path in skills_root.parents or skills_root in path.parents
            )
            if overlaps:
                raise SkillsConfigError(
                    message="skills lazy_from path overlaps existing manifest entries",
                    context={
                        "path": str(skills_root),
                        "source": "lazy_from",
                        "overlaps": overlaps,
                    },
                )
            return manifest

        if self.from_:
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

    def bind(self, session: BaseSandboxSession) -> None:
        super().bind(session)
        self._skills_metadata = None

    def tools(self) -> list[Tool]:
        if self.lazy_from is None:
            return []
        if self.session is None:
            raise ValueError(f"{type(self).__name__} is not bound to a SandboxSession")
        return [_LoadSkillTool(skills=self)]

    async def load_skill(self, skill_name: str) -> dict[str, str]:
        if self.lazy_from is None:
            raise SkillsConfigError(
                message="load_skill is only available when lazy_from is configured",
                context={"skill_name": skill_name},
            )
        if self.session is None:
            raise ValueError(f"{type(self).__name__} is not bound to a SandboxSession")
        return await self.lazy_from.load_skill(
            skill_name=skill_name,
            session=self.session,
            skills_path=self.skills_path,
            user=self.run_as,
        )

    async def _resolve_runtime_metadata(self, manifest: Manifest) -> list[SkillMetadata]:
        if self.session is None:
            return []

        skills_root = posix_path_as_path(
            coerce_posix_path(manifest.root) / coerce_posix_path(self.skills_path)
        )
        try:
            entries = await self.session.ls(skills_root, user=self.run_as)
        except Exception:
            return []

        metadata: list[SkillMetadata] = []
        for entry in entries:
            if not entry.is_dir():
                continue

            skill_dir = posix_path_as_path(coerce_posix_path(entry.path))
            skill_name = skill_dir.name
            skill_path = posix_path_as_path(coerce_posix_path(self.skills_path) / skill_name)
            skill_md_path = skill_dir / "SKILL.md"

            try:
                handle = await self.session.read(skill_md_path, user=self.run_as)
            except Exception:
                continue

            try:
                markdown = _read_text(handle)
            finally:
                handle.close()

            frontmatter = _parse_frontmatter(markdown)
            metadata.append(
                SkillMetadata(
                    name=frontmatter.get("name", skill_name),
                    description=frontmatter.get("description", "No description provided."),
                    path=skill_path,
                )
            )
        return metadata

    async def _skill_metadata(self, manifest: Manifest) -> list[SkillMetadata]:
        if self._skills_metadata is not None:
            return self._skills_metadata

        metadata: list[SkillMetadata] = []

        for skill in self.skills:
            metadata.append(
                SkillMetadata(
                    name=skill.name,
                    description=skill.description,
                    path=posix_path_as_path(coerce_posix_path(self.skills_path) / skill.name),
                )
            )

        if self.lazy_from is not None:
            metadata.extend(self.lazy_from.list_skill_metadata(skills_path=self.skills_path))
        elif self.from_ is not None:
            metadata.extend(await self._resolve_runtime_metadata(manifest))

        if isinstance(self.from_, Dir) and not metadata:
            for key, entry in self.from_.children.items():
                if not isinstance(entry, Dir):
                    continue
                skill_name = coerce_posix_path(key).as_posix()
                metadata.append(
                    SkillMetadata(
                        name=skill_name,
                        description=entry.description or "No description provided.",
                        path=posix_path_as_path(coerce_posix_path(self.skills_path) / skill_name),
                    )
                )

        deduped: dict[tuple[str, str], SkillMetadata] = {}
        for item in metadata:
            deduped[(item.name, str(item.path))] = item

        self._skills_metadata = sorted(deduped.values(), key=lambda item: item.name)
        return self._skills_metadata

    async def instructions(self, manifest: Manifest) -> str | None:
        skills = await self._skill_metadata(manifest)
        if not skills:
            return None

        available_skill_lines: list[str] = []
        for skill in skills:
            path_str = str(skill.path).replace("\\", "/")
            available_skill_lines.append(f"- {skill.name}: {skill.description} (file: {path_str})")

        how_to_use_section = (
            _HOW_TO_USE_LAZY_SKILLS_SECTION
            if self.lazy_from is not None
            else _HOW_TO_USE_SKILLS_SECTION
        )
        return "\n".join(
            [
                "## Skills",
                _SKILLS_SECTION_INTRO,
                "### Available skills",
                *available_skill_lines,
                *(
                    [
                        "### Lazy loading",
                        "- These skills are indexed for planning, but they are not materialized "
                        "in the workspace yet.",
                        "- Call `load_skill` with a single skill name from the list before "
                        "reading its `SKILL.md` or other files from the workspace.",
                        "- `load_skill` stages exactly one skill under the listed path. "
                        "If you need more than one skill, call it multiple times.",
                    ]
                    if self.lazy_from is not None
                    else []
                ),
                how_to_use_section,
            ]
        )
