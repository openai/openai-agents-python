from __future__ import annotations

import abc
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field

from ...materialization import MaterializedFile
from ...types import FileMode, Permissions
from ..base import BaseEntry

if TYPE_CHECKING:
    from ...session.base_sandbox_session import BaseSandboxSession


class Mount(BaseEntry):
    is_dir: bool = True
    mount_path: Path | None = None
    ephemeral: bool = Field(default=True)

    def model_post_init(self, context: object, /) -> None:
        _ = context
        default_permissions = Permissions(
            owner=FileMode.ALL,
            group=FileMode.READ | FileMode.EXEC,
            other=FileMode.READ | FileMode.EXEC,
        )
        if (
            self.permissions.owner != default_permissions.owner
            or self.permissions.group != default_permissions.group
            or self.permissions.other != default_permissions.other
        ):
            warnings.warn(
                "Mount permissions are not enforced. "
                "Please configure access in the cloud provider instead; "
                "mount-level permissions can be unreliable.",
                stacklevel=2,
            )
            self.permissions.owner = default_permissions.owner
            self.permissions.group = default_permissions.group
            self.permissions.other = default_permissions.other
        self.permissions.directory = True

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _ = base_dir
        mount_path = self._resolve_mount_path(session, dest)
        await self.mount(session, mount_path)
        return []

    async def unmount(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _ = base_dir
        mount_path = self._resolve_mount_path(session, dest)
        await self.unmount_path(session, mount_path)

    async def mount(self, session: BaseSandboxSession, path: Path) -> None:
        await self._mount(session, path)

    async def unmount_path(
        self,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        await self._unmount(session, path)

    @abc.abstractmethod
    async def _mount(self, session: BaseSandboxSession, path: Path) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def _unmount(self, session: BaseSandboxSession, path: Path) -> None:
        raise NotImplementedError

    def _resolve_mount_path(
        self,
        session: BaseSandboxSession,
        dest: Path,
    ) -> Path:
        manifest_root = Path(getattr(session.state.manifest, "root", "/"))
        return self._resolve_mount_path_for_root(manifest_root, dest)

    def _resolve_mount_path_for_root(
        self,
        manifest_root: Path,
        dest: Path,
    ) -> Path:
        if self.mount_path is not None:
            mount_path = Path(self.mount_path)
            if mount_path.is_absolute():
                return mount_path
            # Relative explicit mount paths are interpreted inside the active workspace root so a
            # manifest can stay portable across backends with different concrete root prefixes.
            return manifest_root / mount_path

        if dest.is_absolute():
            try:
                rel_dest = dest.relative_to(manifest_root)
            except ValueError:
                return dest
            # `dest` may already be normalized to an absolute workspace path; re-anchor it to the
            # current manifest root instead of nesting the root twice.
            return manifest_root / rel_dest
        return manifest_root / dest
