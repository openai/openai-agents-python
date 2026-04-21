import abc
import asyncio
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePath, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, field_validator
from typing_extensions import assert_never

from .entries import BaseEntry, Dir, Mount, resolve_workspace_path
from .errors import InvalidManifestPathError
from .manifest_render import render_manifest_description
from .types import Group, User
from .workspace_paths import (
    SandboxPathGrant,
    coerce_posix_path,
    posix_path_as_path,
    windows_absolute_path,
)

DEFAULT_REMOTE_MOUNT_COMMAND_ALLOWLIST = [
    "ls",
    "find",
    "stat",
    "cat",
    "less",
    "head",
    "tail",
    "du",
    "grep",
    "rg",
    "wc",
    "sort",
    "cut",
    "cp",
    "tee",
    "echo",
    "mkdir",
    "rm",
]


# TODO (sdcoffey) env val from secret store
class EnvValue(BaseModel, abc.ABC):
    @abc.abstractmethod
    async def resolve(self) -> str: ...


class StrEnvValue(EnvValue):
    value: str

    async def resolve(self) -> str:
        return self.value


class EnvEntry(BaseModel):
    description: str | None = None
    ephemeral: bool = Field(default=False)
    value: EnvValue


class Environment(BaseModel):
    value: dict[str, str | EnvValue | EnvEntry] = Field(default_factory=dict)

    def normalized(self) -> dict[str, EnvEntry]:
        result: dict[str, EnvEntry] = {}
        for key, value in self.value.items():
            match value:
                case str():
                    result[key] = EnvEntry(value=StrEnvValue(value=value))
                case EnvValue():
                    result[key] = EnvEntry(value=value)
                case EnvEntry():
                    result[key] = value
                case _:
                    assert_never(value)

        return result

    async def resolve(self) -> dict[str, str]:
        normalized = self.normalized()
        keys = normalized.keys()
        values = await asyncio.gather(*[normalized[key].value.resolve() for key in keys])
        return dict(zip(keys, values, strict=False))


class Manifest(BaseModel):
    version: Literal[1] = 1
    root: str = Field(default="/workspace")
    entries: dict[str | Path, BaseEntry] = Field(default_factory=dict)
    environment: Environment = Field(default_factory=Environment)
    users: list[User] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    extra_path_grants: tuple[SandboxPathGrant, ...] = Field(default_factory=tuple)
    remote_mount_command_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_REMOTE_MOUNT_COMMAND_ALLOWLIST)
    )

    @field_validator("entries", mode="before")
    @classmethod
    def _parse_entries(cls, value: object) -> dict[str | Path, BaseEntry]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"Artifact mapping must be a mapping, got {type(value).__name__}")
        return {key: BaseEntry.parse(entry) for key, entry in value.items()}

    @field_serializer("entries", when_used="json")
    def _serialize_entries(self, entries: Mapping[str | Path, BaseEntry]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, entry in entries.items():
            key_str = key.as_posix() if isinstance(key, Path) else str(key)
            out[key_str] = entry.model_dump(mode="json")
        return out

    def validated_entries(self) -> dict[str | Path, BaseEntry]:
        validated: dict[str | Path, BaseEntry] = dict(self.entries)
        for _path, _artifact in self.iter_entries():
            pass
        return validated

    def ephemeral_entry_paths(self, depth: int | None = 1) -> set[Path]:
        _ = depth
        return {path for path, artifact in self.iter_entries() if artifact.ephemeral}

    def mount_targets(self) -> list[tuple[Mount, Path]]:
        root = posix_path_as_path(coerce_posix_path(self.root))
        mounts: list[tuple[Mount, Path]] = []
        for rel_path, artifact in self.iter_entries():
            if not isinstance(artifact, Mount):
                continue
            dest = resolve_workspace_path(root, rel_path)
            mount_path = artifact._resolve_mount_path_for_root(root, dest)
            normalized_mount_path = self._normalize_in_workspace_path(root, mount_path)
            if normalized_mount_path is not None:
                mount_path = normalized_mount_path
            mounts.append((artifact, mount_path))
        mounts.sort(key=lambda item: len(item[1].parts), reverse=True)
        return mounts

    def ephemeral_mount_targets(self) -> list[tuple[Mount, Path]]:
        return [(artifact, path) for artifact, path in self.mount_targets() if artifact.ephemeral]

    def ephemeral_persistence_paths(self, depth: int | None = 1) -> set[Path]:
        _ = depth
        root = posix_path_as_path(coerce_posix_path(self.root))
        skip = self.ephemeral_entry_paths(depth=depth)
        for _mount, mount_path in self.ephemeral_mount_targets():
            try:
                rel_mount_path = mount_path.relative_to(root)
            except ValueError:
                continue
            if rel_mount_path.parts:
                skip.add(rel_mount_path)
        return skip

    @staticmethod
    def _coerce_rel_path(path: str | PurePath) -> Path:
        if (windows_path := windows_absolute_path(path)) is not None:
            raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
        return posix_path_as_path(coerce_posix_path(path))

    @staticmethod
    def _validate_rel_path(rel: Path) -> None:
        if (windows_path := windows_absolute_path(rel)) is not None:
            raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
        rel_path = coerce_posix_path(rel)
        if rel_path.is_absolute():
            raise InvalidManifestPathError(rel=rel_path.as_posix(), reason="absolute")
        if ".." in rel_path.parts:
            raise InvalidManifestPathError(rel=rel_path.as_posix(), reason="escape_root")

    @staticmethod
    def _normalize_rel_path_within_root(rel: Path, *, original: Path) -> Path:
        rel_path = coerce_posix_path(rel)
        original_path = coerce_posix_path(original)
        if (windows_path := windows_absolute_path(original)) is not None:
            raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
        if rel_path.is_absolute():
            raise InvalidManifestPathError(rel=original_path.as_posix(), reason="absolute")

        normalized_parts: list[str] = []
        for part in rel_path.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not normalized_parts:
                    raise InvalidManifestPathError(
                        rel=original_path.as_posix(), reason="escape_root"
                    )
                normalized_parts.pop()
                continue
            normalized_parts.append(part)

        return posix_path_as_path(PurePosixPath(*normalized_parts))

    @classmethod
    def _normalize_in_workspace_path(cls, root: Path, path: Path) -> Path | None:
        root_path = coerce_posix_path(root)
        if (windows_path := windows_absolute_path(path)) is not None:
            raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
        path_posix = coerce_posix_path(path)
        if not path_posix.is_absolute():
            normalized_rel = cls._normalize_rel_path_within_root(
                posix_path_as_path(path_posix),
                original=posix_path_as_path(path_posix),
            )
            return root / normalized_rel if normalized_rel.parts else root

        try:
            rel_path = path_posix.relative_to(root_path)
        except ValueError:
            return None

        normalized_rel = cls._normalize_rel_path_within_root(
            posix_path_as_path(rel_path),
            original=posix_path_as_path(path_posix),
        )
        root_as_path = posix_path_as_path(root_path)
        return root_as_path / normalized_rel if normalized_rel.parts else root_as_path

    def iter_entries(self) -> Iterator[tuple[Path, BaseEntry]]:
        stack = [
            (self._coerce_rel_path(path), artifact)
            for path, artifact in reversed(list(self.entries.items()))
        ]
        while stack:
            rel_path, artifact = stack.pop()
            self._validate_rel_path(rel_path)
            yield rel_path, artifact
            if not isinstance(artifact, Dir):
                continue

            for child_name, child_artifact in reversed(list(artifact.children.items())):
                child_rel_path = rel_path / self._coerce_rel_path(child_name)
                stack.append((child_rel_path, child_artifact))

    def describe(self, depth: int | None = 1) -> str:
        """
        print a nice fs representation of things inside root with inline descriptions
        depth controls how deep the tree is rendered; None renders all levels
        eg:

        /workspace                      (root)
        ├── repo/                       # /workspace/repo — my repo
        │   └── README.md               # /workspace/repo/README.md
        ├── data/                       # /workspace/data
        │   └── config.json             # /workspace/data/config.json — config
        ├── mount-data/                 # /workspace/mount-data (mount)
        └── notes.txt                   # /workspace/notes.txt
        ...
        """
        return render_manifest_description(
            root=self.root,
            entries=self.validated_entries(),
            coerce_rel_path=self._coerce_rel_path,
            depth=depth,
        )
