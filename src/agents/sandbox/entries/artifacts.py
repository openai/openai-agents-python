from __future__ import annotations

import io
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_serializer, field_validator

from ..errors import (
    GitCloneError,
    GitCopyError,
    GitMissingInImageError,
    LocalChecksumError,
    LocalDirReadError,
    LocalFileReadError,
)
from ..materialization import MaterializedFile, gather_in_order
from ..types import ExecResult
from ..util.checksums import sha256_file
from .base import BaseEntry

if TYPE_CHECKING:
    from ..session.base_sandbox_session import BaseSandboxSession

_COMMIT_REF_RE = re.compile(r"[0-9a-fA-F]{7,40}")


class Dir(BaseEntry):
    type: Literal["dir"] = "dir"
    is_dir: bool = True
    children: dict[str | Path, BaseEntry] = Field(default_factory=dict)

    @field_validator("children", mode="before")
    @classmethod
    def _parse_children(cls, value: object) -> dict[str | Path, BaseEntry]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"Artifact mapping must be a mapping, got {type(value).__name__}")
        return {key: BaseEntry.parse(entry) for key, entry in value.items()}

    @field_serializer("children", when_used="json")
    def _serialize_children(self, children: Mapping[str | Path, BaseEntry]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, entry in children.items():
            key_str = key.as_posix() if isinstance(key, Path) else str(key)
            out[key_str] = entry.model_dump(mode="json")
        return out

    def model_post_init(self, context: object, /) -> None:
        _ = context
        self.permissions.directory = True

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        await session.mkdir(dest, parents=True)
        await self._apply_metadata(session, dest)
        return await session._apply_entry_batch(
            [(dest / Path(rel_dest), artifact) for rel_dest, artifact in self.children.items()],
            base_dir=base_dir,
        )


class File(BaseEntry):
    type: Literal["file"] = "file"
    content: bytes

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        await session.write(dest, io.BytesIO(self.content))
        await self._apply_metadata(session, dest)
        return []


class LocalFile(BaseEntry):
    type: Literal["local_file"] = "local_file"
    src: Path

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        src = (base_dir / self.src).resolve()
        try:
            checksum = sha256_file(src)
        except OSError as e:
            raise LocalChecksumError(src=src, cause=e) from e
        await session.mkdir(Path(dest).parent, parents=True)
        try:
            with src.open("rb") as f:
                await session.write(dest, f)
        except OSError as e:
            raise LocalFileReadError(src=src, cause=e) from e
        await self._apply_metadata(session, dest)
        return [MaterializedFile(path=dest, sha256=checksum)]


class LocalDir(BaseEntry):
    type: Literal["local_dir"] = "local_dir"
    is_dir: bool = True
    src: Path | None = Field(default=None)

    def model_post_init(self, context: object, /) -> None:
        _ = context
        self.permissions.directory = True

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        files: list[MaterializedFile] = []
        if self.src:
            src_root = (base_dir / self.src).resolve()
            if not src_root.exists():
                raise LocalDirReadError(src=src_root, context={"reason": "path_not_found"})
            # Minimal v1: copy all files recursively.
            try:
                await session.mkdir(dest, parents=True)
                files = []
                local_files = [child for child in src_root.rglob("*") if child.is_file()]

                def _make_copy_task(child: Path) -> Callable[[], Awaitable[MaterializedFile]]:
                    async def _copy() -> MaterializedFile:
                        return await self._copy_local_dir_file(
                            session=session,
                            src_root=src_root,
                            src=child,
                            dest_root=dest,
                        )

                    return _copy

                copied_files = await gather_in_order(
                    [_make_copy_task(child) for child in local_files]
                )
                files.extend(copied_files)
            except OSError as e:
                raise LocalDirReadError(src=src_root, cause=e) from e
            await self._apply_metadata(session, dest)
        else:
            await session.mkdir(dest, parents=True)
            await self._apply_metadata(session, dest)
        return files

    async def _copy_local_dir_file(
        self,
        *,
        session: BaseSandboxSession,
        src_root: Path,
        src: Path,
        dest_root: Path,
    ) -> MaterializedFile:
        rel_child = src.relative_to(src_root)
        child_dest = dest_root / rel_child
        try:
            checksum = sha256_file(src)
        except OSError as e:
            raise LocalChecksumError(src=src, cause=e) from e
        await session.mkdir(child_dest.parent, parents=True)
        try:
            with src.open("rb") as f:
                await session.write(child_dest, f)
        except OSError as e:
            raise LocalFileReadError(src=src, cause=e) from e
        return MaterializedFile(path=child_dest, sha256=checksum)


class GitRepo(BaseEntry):
    type: Literal["git_repo"] = "git_repo"
    is_dir: bool = True
    host: str = "github.com"
    repo: str  # "owner/name" (or any host-specific path)
    ref: str  # tag/branch/sha
    subpath: str | None = None

    def model_post_init(self, context: object, /) -> None:
        _ = context
        self.permissions.directory = True

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        # Ensure git exists in the container.
        git_check = await session.exec("command -v git >/dev/null 2>&1")
        if not git_check.ok():
            context: dict[str, object] = {"repo": self.repo, "ref": self.ref}
            image = getattr(session.state, "image", None)
            if image is not None:
                context["image"] = image
            raise GitMissingInImageError(context=context)

        tmp_dir = f"/tmp/uc-git-{session.state.session_id.hex}-{uuid.uuid4().hex}"
        url = f"https://{self.host}/{self.repo}.git"

        _ = await session.exec("rm", "-rf", "--", tmp_dir, shell=False)
        clone_error: ExecResult | None = None
        if self._looks_like_commit_ref(self.ref):
            clone = await self._fetch_commit_ref(session=session, url=url, tmp_dir=tmp_dir)
            if not clone.ok():
                clone_error = clone
                _ = await session.exec("rm", "-rf", "--", tmp_dir, shell=False)
                clone = await self._clone_named_ref(session=session, url=url, tmp_dir=tmp_dir)
        else:
            clone = await self._clone_named_ref(session=session, url=url, tmp_dir=tmp_dir)
        if not clone.ok():
            if clone_error is not None:
                clone = clone_error
            raise GitCloneError(
                url=url,
                ref=self.ref,
                stderr=clone.stderr.decode("utf-8", errors="replace"),
                context={"repo": self.repo, "subpath": self.subpath},
            )

        git_src_root: str = tmp_dir
        if self.subpath is not None:
            git_src_root = f"{tmp_dir}/{self.subpath.lstrip('/')}"

        # Copy into destination in the container.
        await session.mkdir(dest, parents=True)
        copy = await session.exec("cp", "-R", "--", f"{git_src_root}/.", f"{dest}/", shell=False)
        if not copy.ok():
            raise GitCopyError(
                src_root=git_src_root,
                dest=dest,
                stderr=copy.stderr.decode("utf-8", errors="replace"),
                context={"repo": self.repo, "ref": self.ref, "subpath": self.subpath},
            )

        _ = await session.exec("rm", "-rf", "--", tmp_dir, shell=False)
        await self._apply_metadata(session, dest)

        # Receipt: leave checksums empty for now. (Computing them would
        # require reading each file back out of the container.)
        return []

    @staticmethod
    def _looks_like_commit_ref(ref: str) -> bool:
        return _COMMIT_REF_RE.fullmatch(ref) is not None

    async def _clone_named_ref(
        self,
        *,
        session: BaseSandboxSession,
        url: str,
        tmp_dir: str,
    ) -> ExecResult:
        return await session.exec(
            "git",
            "clone",
            "--depth",
            "1",
            "--no-tags",
            "--branch",
            self.ref,
            url,
            tmp_dir,
            shell=False,
        )

    async def _fetch_commit_ref(
        self,
        *,
        session: BaseSandboxSession,
        url: str,
        tmp_dir: str,
    ) -> ExecResult:
        init = await session.exec("git", "init", tmp_dir, shell=False)
        if not init.ok():
            return init

        remote_add = await session.exec(
            "git",
            "-C",
            tmp_dir,
            "remote",
            "add",
            "origin",
            url,
            shell=False,
        )
        if not remote_add.ok():
            return remote_add

        fetch = await session.exec(
            "git",
            "-C",
            tmp_dir,
            "fetch",
            "--depth",
            "1",
            "--no-tags",
            "origin",
            self.ref,
            shell=False,
        )
        if not fetch.ok():
            return fetch

        return await session.exec(
            "git",
            "-C",
            tmp_dir,
            "checkout",
            "--detach",
            "FETCH_HEAD",
            shell=False,
        )
