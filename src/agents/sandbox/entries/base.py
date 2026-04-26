from __future__ import annotations

import abc
import builtins
import inspect
import posixpath
import stat
from collections.abc import Mapping
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from ..errors import InvalidManifestPathError
from ..materialization import MaterializedFile
from ..types import FileMode, Group, Permissions, User
from ..workspace_paths import (
    coerce_posix_path,
    posix_path_as_path,
    sandbox_path_str,
    windows_absolute_path,
)

if TYPE_CHECKING:
    from ..session.base_sandbox_session import BaseSandboxSession


def resolve_workspace_path(
    workspace_root: str | PurePath,
    rel: str | PurePath,
    *,
    allow_absolute_within_root: bool = False,
) -> Path:
    if (windows_path := windows_absolute_path(rel)) is not None:
        raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
    rel_path = coerce_posix_path(rel)
    root_path = coerce_posix_path(workspace_root)

    if rel_path.is_absolute():
        if not allow_absolute_within_root:
            raise InvalidManifestPathError(rel=rel_path.as_posix(), reason="absolute")
        rel_path = PurePosixPath(posixpath.normpath(rel_path.as_posix()))
        root_path = PurePosixPath(posixpath.normpath(root_path.as_posix()))
        host_root = Path(root_path.as_posix())
        if _path_exists(host_root):
            try:
                Path(rel_path.as_posix()).resolve(strict=False).relative_to(
                    host_root.resolve(strict=False)
                )
            except ValueError as exc:
                raise InvalidManifestPathError(
                    rel=rel_path.as_posix(), reason="absolute", cause=exc
                ) from exc
        try:
            rel_path.relative_to(root_path)
        except ValueError as exc:
            raise InvalidManifestPathError(
                rel=rel_path.as_posix(), reason="absolute", cause=exc
            ) from exc
        return posix_path_as_path(rel_path)

    if ".." in rel_path.parts:
        raise InvalidManifestPathError(rel=rel_path.as_posix(), reason="escape_root")

    resolved = root_path / rel_path if rel_path.parts else root_path
    if allow_absolute_within_root and resolved.is_absolute():
        try:
            resolved.relative_to(root_path)
        except ValueError as exc:
            raise InvalidManifestPathError(
                rel=rel_path.as_posix(), reason="escape_root", cause=exc
            ) from exc
    return posix_path_as_path(resolved)


def _path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


class BaseEntry(BaseModel, abc.ABC):
    type: str
    _subclass_registry: ClassVar[dict[str, builtins.type[BaseEntry]]] = {}
    _abstract_entry_base: ClassVar[bool] = False

    description: str | None = Field(default=None)
    ephemeral: bool = Field(default=False)
    group: Group | User | None = Field(default=None)
    # Whether this entry should be treated as a directory in the sandbox filesystem.
    # Concrete subclasses override this (e.g. Dir/Mount types -> True).
    is_dir: bool = Field(default=False)
    permissions: Permissions = Field(
        default_factory=lambda: Permissions(
            owner=FileMode.ALL,
            group=FileMode.READ | FileMode.EXEC,
            other=FileMode.READ | FileMode.EXEC,
        )
    )

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)

        type_field = cls.model_fields.get("type")
        type_default = type_field.default if type_field is not None else None
        if not isinstance(type_default, str) or type_default == "":
            if inspect.isabstract(cls) or getattr(cls, "_abstract_entry_base", False):
                return
            raise TypeError(f"{cls.__name__} must define a non-empty string default for `type`")

        cls._register_subclass(cls, allow_override=False)

    @classmethod
    def _register_subclass(
        cls,
        entry_cls: builtins.type[BaseEntry],
        *,
        allow_override: bool = False,
    ) -> builtins.type[BaseEntry]:
        type_field = entry_cls.model_fields.get("type")
        type_default = type_field.default if type_field is not None else None
        if not isinstance(type_default, str) or type_default == "":
            raise ValueError(f"{entry_cls.__name__} must define a string `type` field default")

        existing = BaseEntry._subclass_registry.get(type_default)
        if existing is not None and existing is not entry_cls and not allow_override:
            raise ValueError(
                f"Artifact type `{type_default}` is already registered to {existing.__name__}; "
                f"refusing to register {entry_cls.__name__}"
            )

        BaseEntry._subclass_registry[type_default] = entry_cls
        return entry_cls

    @classmethod
    def registered_types(cls) -> dict[str, builtins.type[BaseEntry]]:
        return dict(BaseEntry._subclass_registry)

    @classmethod
    def parse(cls, payload: object) -> BaseEntry:
        if isinstance(payload, BaseEntry):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError(
                f"Artifact entry must be a BaseEntry or mapping, got {type(payload).__name__}"
            )

        entry_type = payload.get("type")
        if not isinstance(entry_type, str):
            raise ValueError("Artifact entry mapping must include a string `type` field")

        entry_cls = BaseEntry._subclass_registry.get(entry_type)
        if entry_cls is None:
            known = ", ".join(sorted(BaseEntry._subclass_registry)) or "<none>"
            raise ValueError(f"Unknown artifact type `{entry_type}`. Registered types: {known}")
        return entry_cls.model_validate(dict(payload))

    async def _apply_metadata(
        self,
        session: BaseSandboxSession,
        dest: Path,
    ) -> None:
        dest_arg = sandbox_path_str(dest)
        if self.group is not None:
            await session._exec_checked_nonzero("chgrp", self.group.name, dest_arg)

        chmod_perms = f"{stat.S_IMODE(self.permissions.to_mode()):o}".zfill(4)
        await session._exec_checked_nonzero("chmod", chmod_perms, dest_arg)

    @abc.abstractmethod
    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        raise NotImplementedError
