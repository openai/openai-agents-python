from __future__ import annotations

import abc
import io
import os
import re
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path, PurePath
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from agents.classic.sandbox.entries import BaseEntry, Dir, File, LocalDir, LocalFile
from agents.classic.sandbox.errors import (
    LocalDirReadError,
    SkillsConfigError,
    WorkspaceReadNotFoundError,
)
from agents.classic.sandbox.manifest import Manifest as Manifest
from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.classic.sandbox.session.sandbox_session import (
    SandboxSession as SandboxSession,
    _read_with_expected_span_errors,
)
from agents.classic.sandbox.types import User
from agents.classic.sandbox.workspace_paths import (
    SandboxPathGrant,
    SandboxWorkspaceScope as SandboxWorkspaceScope,
    coerce_posix_path as coerce_posix_path,
    posix_path_as_path as posix_path_as_path,
    windows_absolute_path,
)

_SKILLS_SECTION_INTRO = (
    "A skill is a set of local instructions to follow that is stored in a `SKILL.md` file. "
    "Below is the list of skills that can be used. Each entry includes a name, description, "
    "and file path so you can open the source for full instructions when using a specific skill."
)

_SKILL_PATH_GUIDANCE = (
    "- Skill paths: Treat each listed path as the skill root. Resolve relative paths in "
    "`SKILL.md`, including `scripts/`, `references/`, and `assets/`, against that root rather "
    "than the shell working directory.",
    "- Shared resources: Skill files belong to the sandbox session and may be visible to other "
    "runs. Unless the task explicitly requires editing a skill, invoke scripts through the "
    "listed skill root and write task inputs, outputs, caches, and temporary files in the run "
    "working directory.",
)

_SCOPED_SKILL_PATH_ERROR = (
    "skill path must be non-empty and workspace-relative when sandbox.cwd is configured"
)

_MAX_METADATA_ENTRIES = 1024
_MAX_METADATA_FILE_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 4 * 1024 * 1024


@dataclass
class _MetadataBudget:
    remaining: int = _MAX_METADATA_BYTES

    def read_limit(self) -> int:
        return min(_MAX_METADATA_FILE_BYTES, self.remaining) + 1

    def parse(self, payload: bytes, *, read_limit: int) -> dict[str, str] | None:
        self.remaining -= len(payload)
        if self.remaining < 0:
            raise ValueError("Skills metadata exceeds the aggregate byte limit")
        truncated = len(payload) >= read_limit
        if truncated:
            prefix = payload[: read_limit - 1]
            end = max(prefix.rfind(b"\n"), prefix.rfind(b"\r"))
            if end < 0:
                return None
            payload = prefix[: end + 1]
        markdown = payload.decode("utf-8", errors="replace")
        return _parse_frontmatter(markdown, require_complete=truncated)


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
    def list_skill_metadata(
        self,
        *,
        skills_path: str,
        source_grants: tuple[SandboxPathGrant, ...] = (),
    ) -> list[SkillMetadata]: ...

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

    def _src_root(self, *, source_grants: tuple[SandboxPathGrant, ...] = ()) -> Path | None:
        if self.source.src is None:
            return None
        try:
            src_root = self.source._resolve_local_dir_src_root(
                Path.cwd(),
                source_grants=source_grants,
            )
        except LocalDirReadError:
            return None
        if not src_root.exists() or not src_root.is_dir():
            return None
        return src_root

    def list_skill_metadata(
        self,
        *,
        skills_path: str,
        source_grants: tuple[SandboxPathGrant, ...] = (),
    ) -> list[SkillMetadata]:
        return self._list_skill_metadata(skills_path=skills_path, source_grants=source_grants)

    def _list_skill_metadata(
        self,
        *,
        skills_path: str,
        source_grants: tuple[SandboxPathGrant, ...],
        budget: _MetadataBudget | None = None,
    ) -> list[SkillMetadata]:
        src_root = self._src_root(source_grants=source_grants)
        if src_root is None:
            return []

        if budget is None:
            children: Iterable[Path] = src_root.iterdir()
        else:
            with os.scandir(src_root) as entries:
                bounded_children = [
                    src_root / entry.name for entry in islice(entries, _MAX_METADATA_ENTRIES + 1)
                ]
            if len(bounded_children) > _MAX_METADATA_ENTRIES:
                raise ValueError("Skills metadata directory exceeds the entry limit")
            children = bounded_children
        metadata: list[SkillMetadata] = []
        for child in sorted(children, key=lambda entry: entry.name):
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(child_stat.st_mode):
                continue
            skill_md_path = child / "SKILL.md"
            try:
                skill_md_stat = skill_md_path.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(skill_md_stat.st_mode):
                continue
            try:
                if budget is None:
                    frontmatter = _parse_frontmatter(skill_md_path.read_text(encoding="utf-8"))
                else:
                    read_limit = budget.read_limit()
                    descriptor = self.source._open_local_dir_file_for_copy(
                        base_dir=Path.cwd(),
                        src_root=src_root,
                        rel_child=skill_md_path.relative_to(src_root),
                        source_grants=source_grants,
                        nonblocking=True,
                    )
                    with os.fdopen(descriptor, "rb") as handle:
                        payload = handle.read(read_limit)
                    frontmatter = budget.parse(payload, read_limit=read_limit)
            except OSError:
                continue
            if frontmatter is None:
                continue
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
        return await self._load_skill(
            skill_name=skill_name, session=session, skills_path=skills_path, user=user
        )

    async def _load_skill(
        self,
        *,
        skill_name: str,
        session: BaseSandboxSession,
        skills_path: str,
        user: str | User | None,
        indexed: SkillMetadata | None = None,
    ) -> dict[str, str]:
        source_grants = session.state.manifest.extra_path_grants
        src_root = self._src_root(source_grants=source_grants)
        if src_root is None:
            raise SkillsConfigError(
                message="lazy skill source directory is unavailable",
                context={"skill_name": skill_name},
            )

        metadata = indexed
        if metadata is None:
            matches = [
                skill
                for skill in self.list_skill_metadata(
                    skills_path=skills_path,
                    source_grants=source_grants,
                )
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
        native_bounded = indexed is not None and type(self.source).apply is LocalDir.apply
        if not native_bounded:
            try:
                if indexed is None:
                    handle = await _read_with_expected_span_errors(
                        session,
                        skill_md_path,
                        user=user,
                        expected_span_errors=(FileNotFoundError, WorkspaceReadNotFoundError),
                    )
                    handle.close()
                else:
                    await session.read_bounded(skill_md_path, max_bytes=1)
            except (FileNotFoundError, WorkspaceReadNotFoundError):
                pass
            else:
                return {
                    "status": "already_loaded",
                    "skill_name": metadata.name,
                    "path": str(metadata.path).replace("\\", "/"),
                }
        # Materialize through a copy of the configured source so the loaded skill keeps the
        # entry metadata (permissions, group) that the eager `from_` path already applies.
        skill_source = self.source.model_copy(
            update={"src": src_root / metadata.path.name},
            deep=True,
        )
        status = "loaded"
        if native_bounded:
            copied = await skill_source._apply_bounded(
                session,
                skill_dest,
                base_dir=Path.cwd(),
                user=user,
                existing_marker=skill_md_path,
            )
            if copied is None:
                status = "already_loaded"
        else:
            await skill_source.apply(session, skill_dest, base_dir=Path.cwd(), user=user)
        return {
            "status": status,
            "skill_name": metadata.name,
            "path": str(metadata.path).replace("\\", "/"),
        }


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


_BLOCK_SCALAR_HEADER = re.compile(r"[>|](?:[1-9][+-]?|[+-][1-9]?)?(?:[ \t]+#.*)?")


def _indent_of(line: str) -> int:
    """Count indentation spaces, leaving tabs in scalar content."""

    return len(line) - len(line.lstrip(" "))


def _fold_lines(lines: list[str]) -> str:
    """Fold ordinary lines, retaining paragraphs and more-indented block content."""

    parts: list[str] = []
    previous = ""
    blank_lines = 0
    for line in lines:
        if not line:
            blank_lines += 1
            continue
        if parts:
            more_indented = previous.startswith((" ", "\t")) or line.startswith((" ", "\t"))
            parts.append("\n" * (blank_lines + 1) if more_indented else "\n" * blank_lines or " ")
        parts.append(line)
        previous = line
        blank_lines = 0
    return "".join(parts)


def _take_continuation_lines(
    lines: list[str], start: int, end: int, key_indent: int, *, block_header: str | None
) -> tuple[list[str], int]:
    """Consume a field's indented body and strip a block scalar's content indentation."""

    content_indent: int | None = None
    if block_header is not None:
        content_indent = next(
            (key_indent + int(char) for char in block_header[1:] if char.isdigit()), None
        )
    body: list[str] = []
    index = start
    while index < end:
        line = lines[index]
        stripped = line.strip()
        # Plain-scalar comments may have any indentation and are not scalar content.
        if block_header is None and stripped.startswith("#"):
            index += 1
            continue
        if stripped:
            indent = _indent_of(line)
            # A dedented comment ends a block just like any other nonblank line.
            if indent < (content_indent if content_indent is not None else key_indent + 1):
                break
            if block_header is not None and content_indent is None:
                content_indent = indent
        body.append(line)
        index += 1
    while body and not body[-1].strip():
        body.pop()
    if content_indent is not None:
        body = [line[content_indent:] for line in body]
    return body, index


def _parse_frontmatter(markdown: str, *, require_complete: bool = False) -> dict[str, str] | None:
    """Read the string metadata subset used by skill indexes, without a YAML dependency.

    Supports single-line values, indented wrapped plain values, and folded/literal blocks
    with optional indentation/chomping indicators and header comments. Block values have
    outer whitespace removed for the index; chomping indicators are accepted but do not
    change that normalization. Internal paragraph breaks and block indentation survive.

    This is not a general YAML loader: tags, aliases, collections, multiline quoted values,
    and quoted escape decoding are not interpreted. Nested fields are consumed without
    promoting their keys. Single-line quote stripping and inline hash text retain their
    historical behavior. Supply explicit Skill metadata or a LazySkillSource when a full
    YAML loader is needed.
    """

    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    # Indented document markers inside scalar content do not close the frontmatter.
    delimiter_indent = _indent_of(lines[0])
    end_index = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---" and _indent_of(line) <= delimiter_indent
        ),
        None,
    )
    if end_index is None:
        return None if require_complete else {}

    metadata: dict[str, str] = {}
    index = 1
    while index < end_index:
        line = lines[index]
        index += 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        parsed_value = value.strip()
        # Header comments cannot supply an indentation or chomping indicator.
        block_header = (
            parsed_value.split()[0] if _BLOCK_SCALAR_HEADER.fullmatch(parsed_value) else None
        )
        key_indent = _indent_of(line)
        continuation, index = _take_continuation_lines(
            lines, index, end_index, key_indent, block_header=block_header
        )

        if block_header is not None:
            parsed_value = (
                "\n".join(continuation) if block_header[0] == "|" else _fold_lines(continuation)
            ).strip()
        elif (
            len(parsed_value) >= 2
            and parsed_value[0] == parsed_value[-1]
            and parsed_value[0] in {"'", '"'}
        ):
            parsed_value = parsed_value[1:-1]
        elif parsed_value and continuation:
            parsed_value = _fold_lines([parsed_value, *(item.strip() for item in continuation)])

        metadata[key.strip()] = parsed_value
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


class SkillSet(BaseModel):
    """Shared declarations and resource operations for sandbox skills."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    session: BaseSandboxSession | None = Field(default=None, exclude=True)
    run_as: User | None = Field(default=None, exclude=True)
    workspace_scope: SandboxWorkspaceScope = Field(
        default_factory=SandboxWorkspaceScope, exclude=True, repr=False
    )

    type: Literal["skills"] = "skills"
    skills: list[Skill] = Field(default_factory=list)
    from_: BaseEntry | None = Field(default=None)
    lazy_from: LazySkillSource | None = Field(default=None)
    skills_path: str = Field(default=".agents")

    _skills_metadata: list[SkillMetadata] | None = PrivateAttr(default=None)
    _bounded_metadata: ClassVar[bool] = False
    _skills_metadata_cache_key: tuple[tuple[str, bool, str | None], ...] | None = PrivateAttr(
        default=None
    )

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

        if self.lazy_from is not None:
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

    def bind(self, session: BaseSandboxSession) -> None:
        self.session = session
        self._skills_metadata = None
        self._skills_metadata_cache_key = None

    def _model_skill_path(
        self,
        *,
        manifest: Manifest,
        skill_name: str,
        path: str | PurePath,
    ) -> str:
        if self.workspace_scope.cwd is None:
            return str(path).replace("\\", "/")
        try:
            return self.workspace_scope.model_resource_path(
                workspace_root=manifest.root,
                workspace_relative_path=path,
            ).as_posix()
        except ValueError as exc:
            raise SkillsConfigError(
                message=_SCOPED_SKILL_PATH_ERROR,
                context={
                    "skill_name": skill_name,
                    "field": "path",
                    "path": path.as_posix() if isinstance(path, PurePath) else path,
                    "reason": "invalid",
                },
                cause=exc,
            ) from exc

    async def load_skill(self, skill_name: str) -> dict[str, str]:
        return await self._load_skill(skill_name)

    async def _load_skill(
        self, skill_name: str, *, indexed: SkillMetadata | None = None
    ) -> dict[str, str]:
        if self.lazy_from is None:
            raise SkillsConfigError(
                message="load_skill is only available when lazy_from is configured",
                context={"skill_name": skill_name},
            )
        if self.session is None:
            raise ValueError(f"{type(self).__name__} is not bound to a SandboxSession")
        if (
            indexed is not None
            and isinstance(self.lazy_from, LocalDirLazySkillSource)
            and type(self.lazy_from).load_skill is LocalDirLazySkillSource.load_skill
        ):
            result = await self.lazy_from._load_skill(
                skill_name=skill_name,
                session=self.session,
                skills_path=self.skills_path,
                user=self.run_as,
                indexed=indexed,
            )
        else:
            result = await self.lazy_from.load_skill(
                skill_name=skill_name,
                session=self.session,
                skills_path=self.skills_path,
                user=self.run_as,
            )
        if self.workspace_scope.cwd is None:
            return result

        source_path = result.get("path")
        if source_path is None:
            raise SkillsConfigError(
                message=_SCOPED_SKILL_PATH_ERROR,
                context={"skill_name": skill_name, "field": "path", "reason": "missing"},
            )
        return {
            **result,
            "path": self._model_skill_path(
                manifest=self.session.state.manifest,
                skill_name=skill_name,
                path=source_path,
            ),
        }

    async def _resolve_runtime_metadata(
        self, manifest: Manifest, *, budget: _MetadataBudget | None = None
    ) -> list[SkillMetadata]:
        if self.session is None:
            return []

        skills_root = posix_path_as_path(
            coerce_posix_path(manifest.root) / coerce_posix_path(self.skills_path)
        )
        listing_overflow = False
        try:
            entries = (
                await self.session.ls(skills_root, user=self.run_as)
                if budget is None
                else await self.session._ls_bounded(
                    skills_root, max_entries=_MAX_METADATA_ENTRIES + 1, user=self.run_as
                )
            )
        except OverflowError:
            listing_overflow = True
            entries = []
        except Exception:
            return []

        if listing_overflow:
            raise ValueError("Skills metadata directory exceeds the listing byte limit")
        if budget is not None and len(entries) > _MAX_METADATA_ENTRIES:
            raise ValueError("Skills metadata directory exceeds the entry limit")
        metadata: list[SkillMetadata] = []
        for entry in entries:
            if not entry.is_dir():
                continue

            skill_dir = posix_path_as_path(coerce_posix_path(entry.path))
            skill_name = skill_dir.name
            skill_path = posix_path_as_path(coerce_posix_path(self.skills_path) / skill_name)
            skill_md_path = skill_dir / "SKILL.md"

            if budget is None:
                try:
                    handle = await self.session.read(skill_md_path, user=self.run_as)
                except Exception:
                    continue
                try:
                    frontmatter = _parse_frontmatter(_read_text(handle))
                finally:
                    handle.close()
            else:
                read_limit = budget.read_limit()
                try:
                    payload = await self.session.read_bounded(skill_md_path, max_bytes=read_limit)
                except Exception:
                    continue
                frontmatter = budget.parse(payload, read_limit=read_limit)

            if frontmatter is None:
                continue
            metadata.append(
                SkillMetadata(
                    name=frontmatter.get("name", skill_name),
                    description=frontmatter.get("description", "No description provided."),
                    path=skill_path,
                )
            )
        return metadata

    async def _skill_metadata(self, manifest: Manifest) -> list[SkillMetadata]:
        cache_key = self._metadata_cache_key(manifest)
        if self._skills_metadata is not None and self._skills_metadata_cache_key == cache_key:
            return self._skills_metadata

        budget = _MetadataBudget() if self._bounded_metadata else None
        metadata: list[SkillMetadata] = []

        for skill in self.skills:
            metadata.append(
                SkillMetadata(
                    name=skill.name,
                    description=skill.description,
                    path=posix_path_as_path(coerce_posix_path(self.skills_path) / skill.name),
                )
            )

        if (
            budget is not None
            and isinstance(self.lazy_from, LocalDirLazySkillSource)
            and type(self.lazy_from).list_skill_metadata
            is LocalDirLazySkillSource.list_skill_metadata
        ):
            metadata.extend(
                self.lazy_from._list_skill_metadata(
                    skills_path=self.skills_path,
                    source_grants=manifest.extra_path_grants,
                    budget=budget,
                )
            )
        elif self.lazy_from is not None:
            metadata.extend(
                self.lazy_from.list_skill_metadata(
                    skills_path=self.skills_path,
                    source_grants=manifest.extra_path_grants,
                )
            )
        elif self.from_ is not None:
            metadata.extend(await self._resolve_runtime_metadata(manifest, budget=budget))

        if budget is None and isinstance(self.from_, Dir) and not metadata:
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
        self._skills_metadata_cache_key = cache_key
        return self._skills_metadata

    def _metadata_cache_key(
        self,
        manifest: Manifest,
    ) -> tuple[tuple[str, bool, str | None], ...]:
        if self.lazy_from is None:
            return ()
        return tuple(
            (grant.path, grant.read_only, grant.host_path) for grant in manifest.extra_path_grants
        )

    async def instructions(self, manifest: Manifest) -> str | None:
        skills = await self._skill_metadata(manifest)
        if not skills:
            return None

        available_skill_lines: list[str] = []
        for skill in skills:
            path_str = self._model_skill_path(
                manifest=manifest,
                skill_name=skill.name,
                path=skill.path,
            )
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
                    ["### Run-scoped skill paths", *_SKILL_PATH_GUIDANCE]
                    if self.workspace_scope.cwd is not None
                    else []
                ),
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
