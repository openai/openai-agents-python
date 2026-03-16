"""
Modal sandbox (https://modal.com) implementation.

Run `python -m modal setup` to configure Modal locally.

This module provides a Modal-backed sandbox client/session implementation backed by
`modal.Sandbox`.

Note: The `modal` dependency is intended to be optional (installed via an extra),
so package-level exports should guard imports of this module. Within this module,
we import Modal normally so IDEs can resolve and navigate Modal types.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import logging
import math
import shlex
import tarfile
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, TypeVar, cast

import modal
from modal.container_process import ContainerProcess

from ....sandbox.codex_config import (
    CodexConfig,
    apply_codex_to_manifest,
    apply_codex_to_session_state,
)
from ....sandbox.entries import resolve_workspace_path
from ....sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceStopError,
    WorkspaceWriteTypeError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session import SandboxSession, SandboxSessionState
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.dependencies import Dependencies
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.sandbox_client import BaseSandboxClient
from ....sandbox.snapshot import SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult
from ....sandbox.util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_contains_type,
    exception_chain_has_status_code,
    retry_async,
)
from ....sandbox.util.tar_utils import UnsafeTarMemberError, should_skip_tar_member

_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_IMAGE_TAG = "python:3.11-slim"
_DEFAULT_SNAPSHOT_FILESYSTEM_TIMEOUT_S = 60.0

WorkspacePersistenceMode = Literal["tar", "snapshot_filesystem"]

_WORKSPACE_PERSISTENCE_TAR: WorkspacePersistenceMode = "tar"
_WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM: WorkspacePersistenceMode = "snapshot_filesystem"

# Magic prefix for snapshot_filesystem payloads that cannot be represented as tar bytes.
_UC_MODAL_SNAPSHOT_FS_MAGIC = b"UC_MODAL_SNAPSHOT_FS_V1\n"

logger = logging.getLogger(__name__)
R = TypeVar("R")


@dataclass(frozen=True)
class ModalSandboxClientOptions:
    app_name: str
    sandbox_create_timeout_s: float | None = None
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    snapshot_filesystem_timeout_s: float | None = None
    snapshot_filesystem_restore_timeout_s: float | None = None


def _encode_snapshot_filesystem_ref(*, snapshot_id: str) -> bytes:
    # Small JSON envelope so we can round-trip a non-tar snapshot reference
    # through Snapshot.persist().
    body = json.dumps({"snapshot_id": snapshot_id}, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return _UC_MODAL_SNAPSHOT_FS_MAGIC + body


def _decode_snapshot_filesystem_ref(raw: bytes) -> str | None:
    if not raw.startswith(_UC_MODAL_SNAPSHOT_FS_MAGIC):
        return None
    body = raw[len(_UC_MODAL_SNAPSHOT_FS_MAGIC) :]
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    snapshot_id = obj.get("snapshot_id")
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


@dataclass(frozen=True)
class ModalImageSelector:
    """
    A single "image selector" type to avoid juggling image/image_id/image_tag separately.
    """

    kind: Literal["image", "id", "tag"]
    value: modal.Image | str

    @classmethod
    def from_image(cls, image: modal.Image) -> ModalImageSelector:
        return cls(kind="image", value=image)

    @classmethod
    def from_id(cls, image_id: str) -> ModalImageSelector:
        return cls(kind="id", value=image_id)

    @classmethod
    def from_tag(cls, image_tag: str) -> ModalImageSelector:
        return cls(kind="tag", value=image_tag)


@dataclass(frozen=True)
class ModalSandboxSelector:
    """
    A single "sandbox selector" type to avoid juggling sandbox/sandbox_id separately.
    """

    kind: Literal["sandbox", "id"]
    value: modal.Sandbox | str

    @classmethod
    def from_sandbox(cls, sandbox: modal.Sandbox) -> ModalSandboxSelector:
        return cls(kind="sandbox", value=sandbox)

    @classmethod
    def from_id(cls, sandbox_id: str) -> ModalSandboxSelector:
        return cls(kind="id", value=sandbox_id)


class ModalSandboxSessionState(SandboxSessionState):
    """
    Serializable state for a Modal-backed session.

    We store only values that can be safely persisted and later used by `resume()`.
    """

    app_name: str
    # Optional Modal image object id (enables reconstructing a custom image via Image.from_id()).
    image_id: str | None = None
    # Registry image tag (e.g. "debian:bookworm" or "ghcr.io/org/img:tag").
    # Used when `image_id` isn't available and no in-memory image override was provided.
    image_tag: str | None = None
    # Timeout for creating a sandbox (Modal calls are synchronous from the user's perspective
    # and can block; we wrap them in a thread with asyncio timeout).
    sandbox_create_timeout_s: float = _DEFAULT_TIMEOUT_S
    sandbox_id: str | None = None
    # Workspace persistence mode:
    # - "tar": create a tar stream in the sandbox via `tar cf - ...` and pull bytes back via stdout.
    # - "snapshot_filesystem": use Modal's `Sandbox.snapshot_filesystem()`
    #   (if available) and persist a snapshot reference.
    workspace_persistence: WorkspacePersistenceMode = _WORKSPACE_PERSISTENCE_TAR
    # Async timeouts for snapshot_filesystem-based persistence and restore.
    snapshot_filesystem_timeout_s: float = _DEFAULT_SNAPSHOT_FILESYSTEM_TIMEOUT_S
    snapshot_filesystem_restore_timeout_s: float = _DEFAULT_SNAPSHOT_FILESYSTEM_TIMEOUT_S


class ModalSandboxSession(BaseSandboxSession):
    """
    SandboxSession implementation backed by a Modal Sandbox.
    """

    state: ModalSandboxSessionState

    _sandbox: modal.Sandbox | None
    _image: modal.Image | None
    _running: bool

    def __init__(
        self,
        *,
        state: ModalSandboxSessionState,
        # Optional in-memory handles. These are not guaranteed to be resumable; state holds ids.
        image: modal.Image | None = None,
        sandbox: modal.Sandbox | None = None,
    ) -> None:
        self.state = state
        self._image = image
        self._sandbox = sandbox
        if image is not None:
            self.state.image_id = getattr(image, "object_id", self.state.image_id)
        if sandbox is not None:
            self.state.sandbox_id = getattr(sandbox, "object_id", self.state.sandbox_id)
        self._running = False

    @classmethod
    def from_state(
        cls,
        state: ModalSandboxSessionState,
        *,
        image: modal.Image | None = None,
        sandbox: modal.Sandbox | None = None,
    ) -> ModalSandboxSession:
        return cls(state=state, image=image, sandbox=sandbox)

    async def _call_modal(
        self,
        fn: Callable[..., R],
        *args: object,
        call_timeout: float | None = None,
        **kwargs: object,
    ) -> R:
        """
        Prefer Modal's async interface (`fn.aio(...)`) when available.

        Falls back to running the blocking call in a thread to preserve compatibility
        with SDK surfaces that do not expose `.aio`.
        """

        aio_fn = getattr(fn, "aio", None)
        if callable(aio_fn):
            coro = cast(Awaitable[R], aio_fn(*args, **kwargs))
        else:
            loop = asyncio.get_running_loop()
            bound = functools.partial(fn, *args, **kwargs)
            coro = loop.run_in_executor(None, bound)
        if call_timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=call_timeout)

    async def start(self) -> None:
        try:
            # Ensure workspace root exists before SandboxSession.start() needs it.
            await self.exec("mkdir", "-p", "--", str(Path(self.state.manifest.root)), shell=False)
        except Exception as e:
            raise WorkspaceStartError(path=Path(self.state.manifest.root), cause=e) from e

        self._running = True
        await super().start()

    async def stop(self) -> None:
        try:
            await super().stop()
        except Exception as e:
            raise WorkspaceStopError(path=Path(self.state.manifest.root), cause=e) from e

    async def shutdown(self) -> None:
        terminated = False
        try:
            sandbox = self._sandbox
            if sandbox is not None:
                await self._call_modal(sandbox.terminate, call_timeout=5.0)
                terminated = True
            elif self.state.sandbox_id:
                sid = self.state.sandbox_id
                assert sid is not None
                sb = await self._call_modal(modal.Sandbox.from_id, sid, call_timeout=10.0)
                await self._call_modal(sb.terminate, call_timeout=5.0)
                terminated = True
        except Exception:
            pass
        finally:
            if terminated:
                self.state.sandbox_id = None
            self._sandbox = None
            self._running = False

    async def _ensure_sandbox(self) -> None:
        if self._sandbox is not None:
            return

        # If resuming, try to rehydrate the sandbox handle from the persisted id.
        sid = self.state.sandbox_id
        if sid:
            try:
                sb = await self._call_modal(modal.Sandbox.from_id, sid, call_timeout=10.0)

                # `poll()` returns an exit code when the sandbox is terminated, else None.
                poll_result = await self._call_modal(sb.poll, call_timeout=5.0)
                is_running = poll_result is None
                if is_running:
                    self._sandbox = sb
                    return
            except Exception:
                pass

            # Resumed sandbox handle is dead or invalid; clear and create a fresh one.
            self._sandbox = None
            self.state.sandbox_id = None

        app = await self._call_modal(
            modal.App.lookup,
            self.state.app_name,
            create_if_missing=True,
            call_timeout=10.0,
        )
        if not self._image:
            image_id = self.state.image_id
            if image_id:
                self._image = await self._call_modal(
                    modal.Image.from_id, image_id, call_timeout=30.0
                )
            else:
                tag = self.state.image_tag
                if not isinstance(tag, str) or not tag:
                    tag = _DEFAULT_IMAGE_TAG
                    # Record the default for better debuggability/resume.
                    self.state.image_tag = tag
                self._image = await self._call_modal(
                    modal.Image.from_registry, tag, call_timeout=30.0
                )

        manifest_envs = cast(dict[str, str | None], await self.state.manifest.environment.resolve())
        self._sandbox = await self._call_modal(
            modal.Sandbox.create,
            app=app,
            image=self._image,
            workdir=self.state.manifest.root,
            env=manifest_envs,
            call_timeout=self.state.sandbox_create_timeout_s,
        )

        # Persist sandbox id for future resume.
        assert self._sandbox is not None
        self.state.sandbox_id = self._sandbox.object_id

        assert self._image is not None
        self.state.image_id = self._image.object_id

    async def _exec_internal(
        self, *command: str | Path, timeout: float | None = None
    ) -> ExecResult:
        await self._ensure_sandbox()
        assert self._sandbox is not None

        modal_timeout: int | None = None
        if timeout is not None:
            # Modal's Sandbox.exec timeout is integer seconds; use ceil so the command
            # is guaranteed to be terminated server-side at or before our timeout window
            # (modulo 1s granularity).
            modal_timeout = int(max(_DEFAULT_TIMEOUT_S, math.ceil(timeout)))

        def _run() -> ExecResult:
            assert self._sandbox is not None
            try:
                argv: tuple[str, ...] = tuple(str(part) for part in command)
                proc: ContainerProcess[bytes] = self._sandbox.exec(
                    *argv,
                    text=False,
                    timeout=modal_timeout,
                )
                # Drain full output; Modal buffers process output server-side.
                stdout = proc.stdout.read()
                stderr = proc.stderr.read()
                exit_code = proc.wait()
                return ExecResult(
                    stdout=stdout or b"", stderr=stderr or b"", exit_code=exit_code or 0
                )
            except Exception as e:
                raise e

        try:
            return cast(ExecResult, await self._call_modal(_run, call_timeout=timeout))
        except asyncio.TimeoutError as e:
            # The worker thread continues running; prevent background mutations by terminating
            # the sandbox and clearing our handle.
            sandbox = self._sandbox
            if sandbox is not None:
                try:
                    await self._call_modal(sandbox.terminate, call_timeout=5.0)
                except Exception:
                    pass
            self._sandbox = None
            self.state.sandbox_id = None
            self._running = False
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except ExecTimeoutError:
            raise
        except Exception as e:
            raise ExecTransportError(command=command, cause=e) from e

    async def read(self, path: Path) -> io.IOBase:
        # Read by `cat` so the payload is returned as bytes.
        workspace_path = resolve_workspace_path(
            Path(self.state.manifest.root),
            path,
            allow_absolute_within_root=True,
        )
        cmd = ["sh", "-lc", f"cat -- {shlex.quote(str(workspace_path))}"]
        try:
            out = await self.exec(*cmd, shell=False)
        except ExecTimeoutError as e:
            raise WorkspaceArchiveReadError(path=workspace_path, cause=e) from e
        except ExecTransportError as e:
            raise WorkspaceArchiveReadError(path=workspace_path, cause=e) from e

        if not out.ok():
            raise WorkspaceReadNotFoundError(
                path=path, context={"stderr": out.stderr.decode("utf-8", "replace")}
            )

        return io.BytesIO(out.stdout)

    async def write(self, path: Path, data: io.IOBase) -> None:
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=path, actual_type=type(payload).__name__)

        await self._ensure_sandbox()
        assert self._sandbox is not None

        workspace_path = resolve_workspace_path(
            Path(self.state.manifest.root),
            path,
            allow_absolute_within_root=True,
        )

        def _run() -> None:
            assert self._sandbox is not None
            # Ensure parent directory exists.
            parent = str(workspace_path.parent)
            self._sandbox.exec("mkdir", "-p", "--", parent, text=False).wait()

            # Stream bytes into `cat > file` to avoid quoting/binary issues.
            cmd = ["sh", "-lc", f"cat > {shlex.quote(str(workspace_path))}"]
            proc = self._sandbox.exec(*cmd, text=False)
            proc.stdin.write(bytes(payload))
            proc.stdin.write_eof()
            proc.stdin.drain()
            exit_code = proc.wait()
            if exit_code != 0:
                stderr: bytes = proc.stderr.read()
                raise WorkspaceArchiveWriteError(
                    path=workspace_path,
                    context={
                        "reason": "write_nonzero_exit",
                        "exit_code": exit_code,
                        "stderr": stderr.decode("utf-8", "replace"),
                    },
                )

        try:
            await self._call_modal(_run, call_timeout=30.0)
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        if not self._running or self._sandbox is None:
            return False

        try:
            assert self._sandbox is not None
            poll_result = await self._call_modal(self._sandbox.poll, call_timeout=5.0)
            return poll_result is None
        except Exception:
            return False

    async def persist_workspace(self) -> io.IOBase:
        if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM:
            return await self._persist_workspace_via_snapshot_filesystem()
        return await self._persist_workspace_via_tar()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        if self.state.workspace_persistence == _WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM:
            return await self._hydrate_workspace_via_snapshot_filesystem(data)
        return await self._hydrate_workspace_via_tar(data)

    async def _persist_workspace_via_snapshot_filesystem(self) -> io.IOBase:
        """
        Persist the workspace using Modal's snapshot_filesystem API when available.

        Modal's snapshot_filesystem is expected to return a snapshot reference
        (typically a Modal object such as an Image/Snapshot handle, or an id
        string). We serialize a small reference envelope that
        `_hydrate_workspace_via_snapshot_filesystem` can interpret.
        """

        root = Path(self.state.manifest.root)
        await self._ensure_sandbox()
        assert self._sandbox is not None

        sandbox = self._sandbox
        if not hasattr(sandbox, "snapshot_filesystem"):
            # Feature not present in this Modal SDK version; fall back to tar implementation.
            return await self._persist_workspace_via_tar()

        skip = self.state.manifest.ephemeral_persistence_paths()

        # Modal's snapshot_filesystem does not support excluding paths. To
        # preserve the semantics of "ephemeral manifest entries are not
        # persisted", we temporarily remove those paths, snapshot, then
        # restore them back into the running session.
        skip_abs = [root / rel for rel in sorted(skip, key=lambda p: p.as_posix())]
        ephemeral_backup: bytes | None = None
        if skip_abs:
            # Best-effort: tar up the ephemeral paths (if they exist). We run
            # via shell so missing paths do not cause a hard failure
            # (`|| true`).
            rel_args = " ".join(shlex.quote(p.relative_to(root).as_posix()) for p in skip_abs)
            cmd = f"cd -- {shlex.quote(str(root))} && (tar cf - -- {rel_args} 2>/dev/null || true)"
            out = await self.exec("sh", "-lc", cmd, shell=False)
            ephemeral_backup = out.stdout or b""

            # Remove ephemeral paths before snapshot so they are not captured.
            rm_cmd = ["rm", "-rf", "--", *[str(p) for p in skip_abs]]
            _ = await self.exec(*rm_cmd, shell=False)

        restore_error: WorkspaceArchiveReadError | None = None

        async def _restore_ephemeral_paths() -> WorkspaceArchiveReadError | None:
            if not ephemeral_backup:
                return None

            backup = bytes(ephemeral_backup)

            def _restore_ephemeral() -> None:
                assert self._sandbox is not None
                proc = self._sandbox.exec("tar", "xf", "-", "-C", str(root), text=False)
                proc.stdin.write(backup)
                proc.stdin.write_eof()
                proc.stdin.drain()
                exit_code = proc.wait()
                if exit_code != 0:
                    stderr: bytes = proc.stderr.read()
                    raise WorkspaceArchiveReadError(
                        path=root,
                        context={
                            "reason": "snapshot_filesystem_ephemeral_restore_failed",
                            "exit_code": exit_code,
                            "stderr": stderr.decode("utf-8", "replace"),
                        },
                    )

            try:
                await self._call_modal(
                    _restore_ephemeral,
                    call_timeout=self.state.snapshot_filesystem_restore_timeout_s,
                )
            except WorkspaceArchiveReadError as exc:
                return exc
            except Exception as exc:
                return WorkspaceArchiveReadError(
                    path=root,
                    context={"reason": "snapshot_filesystem_ephemeral_restore_failed"},
                    cause=exc,
                )
            return None

        try:
            snap = await self._call_modal(
                sandbox.snapshot_filesystem,
                call_timeout=self.state.snapshot_filesystem_timeout_s,
            )
        except Exception as e:
            restore_error = await _restore_ephemeral_paths()
            if restore_error is not None:
                logger.warning(
                    "Failed to restore Modal ephemeral paths after snapshot failure: %s",
                    restore_error,
                )
            raise WorkspaceArchiveReadError(
                path=root, context={"reason": "snapshot_filesystem_failed"}, cause=e
            ) from e

        if isinstance(snap, (bytes, bytearray)):
            # should never happen, just a safe guardrail
            raise WorkspaceArchiveReadError(
                path=root,
                context={
                    "reason": "snapshot_filesystem_unexpected_bytes",
                    "type": type(snap).__name__,
                },
            )

        # Snapshot is expected to be a Modal Image (or a compatible handle with an object_id).
        if not hasattr(snap, "object_id") and not isinstance(snap, str):
            raise WorkspaceArchiveReadError(
                path=root,
                context={
                    "reason": "snapshot_filesystem_unexpected_return",
                    "type": type(snap).__name__,
                },
            )

        restore_error = await _restore_ephemeral_paths()
        if restore_error is not None:
            raise restore_error

        snapshot_id: str | None = None
        if isinstance(snap, str):
            snapshot_id = snap
        else:
            snapshot_id = getattr(snap, "object_id", None) or getattr(snap, "id", None)
            if snapshot_id is not None and not isinstance(snapshot_id, str):
                snapshot_id = None

        if not snapshot_id:
            raise WorkspaceArchiveReadError(
                path=root,
                context={
                    "reason": "snapshot_filesystem_unexpected_return",
                    "type": type(snap).__name__,
                },
            )

        return io.BytesIO(_encode_snapshot_filesystem_ref(snapshot_id=snapshot_id))

    @retry_async(
        retry_if=lambda exc, self: exception_chain_contains_type(exc, (ExecTransportError,))
        or exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
    )
    async def _persist_workspace_via_tar(self) -> io.IOBase:
        # Existing tar implementation extracted so snapshot_filesystem mode can fall back cleanly.
        root = Path(self.state.manifest.root)
        skip = self.state.manifest.ephemeral_persistence_paths()

        excludes: list[str] = []
        for rel in sorted(skip, key=lambda p: p.as_posix()):
            excludes.extend(["--exclude", f"./{rel.as_posix().lstrip('./')}"])

        cmd: list[str] = [
            "tar",
            "cf",
            "-",
            *excludes,
            "-C",
            str(root),
            ".",
        ]

        try:
            out = await self.exec(*cmd, shell=False)
            if not out.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "reason": "tar_nonzero_exit",
                        "exit_code": out.exit_code,
                        "stderr": out.stderr.decode("utf-8", "replace"),
                    },
                )
            return io.BytesIO(out.stdout)
        except WorkspaceArchiveReadError:
            raise
        except Exception as e:
            raise WorkspaceArchiveReadError(path=root, cause=e) from e

    async def _hydrate_workspace_via_snapshot_filesystem(self, data: io.IOBase) -> None:
        """
        Hydrate using Modal's snapshot_filesystem restore API when the
        persisted payload is a snapshot ref. Otherwise, fall back to tar
        extraction (to support SDKs that return tar bytes).
        """

        root = Path(self.state.manifest.root)
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, (bytes, bytearray)):
            raise WorkspaceArchiveWriteError(path=root, context={"reason": "non_bytes_payload"})

        snapshot_id = _decode_snapshot_filesystem_ref(bytes(raw))
        if snapshot_id is None:
            # Not an envelope; treat as tar payload.
            return await self._hydrate_workspace_via_tar(io.BytesIO(bytes(raw)))
        if not snapshot_id:
            raise WorkspaceArchiveWriteError(
                path=root, context={"reason": "snapshot_filesystem_invalid_snapshot_id"}
            )

        # Best-effort: if a sandbox already exists, terminate it to avoid leaking resources.
        # We want the restored snapshot image to define the new sandbox filesystem.
        prior = self._sandbox
        if prior is not None:
            try:
                await self._call_modal(prior.terminate, call_timeout=5.0)
            except Exception:
                pass
            finally:
                self._sandbox = None
                self.state.sandbox_id = None

        manifest_envs = cast(dict[str, str | None], await self.state.manifest.environment.resolve())

        def _run_restore() -> None:
            # Rehydrate an image from the snapshot id.
            image = modal.Image.from_id(snapshot_id)

            # Prefer the existing app-based sandbox creation signature to match `_ensure_sandbox`.
            app = modal.App.lookup(self.state.app_name, create_if_missing=True)

            try:
                sb = modal.Sandbox.create(
                    app=app,
                    image=image,
                    workdir=self.state.manifest.root,
                    env=manifest_envs,
                )
            except TypeError:
                # Older/newer SDKs may not accept app/workdir; fall back to simpler signatures.
                try:
                    sb = modal.Sandbox.create(
                        name=self.state.app_name,
                        image=image,
                        env=manifest_envs,
                    )
                except TypeError:
                    sb = modal.Sandbox.create(
                        image=image,
                        env=manifest_envs,
                    )

            # Ensure workspace root exists even if the image does not contain it.
            try:
                sb.exec("mkdir", "-p", "--", str(root), text=False).wait()
            except Exception:
                pass

            # Update in-memory handles and persisted ids.
            self._image = image
            self.state.image_id = getattr(image, "object_id", None) or snapshot_id
            self._sandbox = sb
            self.state.sandbox_id = getattr(sb, "object_id", None)

        try:
            await self._call_modal(
                _run_restore, call_timeout=self.state.snapshot_filesystem_restore_timeout_s
            )
        except Exception as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "reason": "snapshot_filesystem_restore_failed",
                    "snapshot_id": snapshot_id,
                },
                cause=e,
            ) from e

    async def _hydrate_workspace_via_tar(self, data: io.IOBase) -> None:
        root = Path(self.state.manifest.root)

        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, (bytes, bytearray)):
            raise WorkspaceArchiveWriteError(path=root, context={"reason": "non_bytes_tar_payload"})

        try:
            with tarfile.open(fileobj=io.BytesIO(bytes(raw)), mode="r:*") as tar:
                for member in tar.getmembers():
                    name = member.name
                    if name in ("", ".", "./"):
                        continue
                    if should_skip_tar_member(
                        name,
                        skip_rel_paths=self.state.manifest.ephemeral_persistence_paths(),
                        root_name=None,
                    ):
                        continue
                    # Mirror tar_utils safety checks (no extraction here).
                    if Path(name).is_absolute():
                        raise UnsafeTarMemberError(member=name, reason="absolute path")
                    if ".." in Path(name).parts:
                        raise UnsafeTarMemberError(member=name, reason="parent traversal")
                    if member.issym() or member.islnk():
                        raise UnsafeTarMemberError(member=name, reason="link member not allowed")
                    if not (member.isdir() or member.isreg()):
                        raise UnsafeTarMemberError(member=name, reason="unsupported member type")
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root, context={"reason": e.reason, "member": e.member}, cause=e
            ) from e
        except (tarfile.TarError, OSError) as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e

        await self._ensure_sandbox()
        assert self._sandbox is not None

        def _run() -> None:
            assert self._sandbox is not None
            self._sandbox.exec("mkdir", "-p", "--", str(root), text=False).wait()
            proc = self._sandbox.exec("tar", "xf", "-", "-C", str(root), text=False)
            proc.stdin.write(bytes(raw))
            proc.stdin.write_eof()
            proc.stdin.drain()
            exit_code = proc.wait()
            if exit_code != 0:
                stderr: bytes = proc.stderr.read()
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "reason": "tar_extract_nonzero_exit",
                        "exit_code": exit_code,
                        "stderr": stderr.decode("utf-8", "replace"),
                    },
                )

        try:
            await self._call_modal(_run, call_timeout=60.0)
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e


class ModalSandboxClient(BaseSandboxClient[ModalSandboxClientOptions]):
    backend_id = "modal"
    _default_image: ModalImageSelector | None
    _default_sandbox: ModalSandboxSelector | None
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        image: ModalImageSelector | None = None,
        sandbox: ModalSandboxSelector | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._default_image = image
        self._default_sandbox = sandbox
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | None = None,
        manifest: Manifest | None = None,
        codex: bool | CodexConfig = False,
        options: ModalSandboxClientOptions,
    ) -> SandboxSession:
        """
        Create a new Modal-backed session.

        Expected options:
        - app_name: str (required)
        - sandbox_create_timeout_s: float | None (async timeout for sandbox creation call)
        - workspace_persistence: Literal["tar", "snapshot_filesystem"] (optional)
        - snapshot_filesystem_timeout_s: float | None
          (async timeout for snapshot_filesystem call)
        - snapshot_filesystem_restore_timeout_s: float | None
          (async timeout for snapshot restore call)
        """

        if options is None:
            raise ValueError("ModalSandboxClient.create requires options with app_name")
        app_name = options.app_name
        if not app_name:
            raise ValueError("ModalSandboxClient.create requires a valid app_name")

        image_sel = self._default_image

        sandbox_sel = self._default_sandbox

        sandbox_create_timeout_s = options.sandbox_create_timeout_s
        if sandbox_create_timeout_s is not None and not isinstance(
            sandbox_create_timeout_s, (int, float)
        ):
            raise ValueError(
                "ModalSandboxClient.create requires sandbox_create_timeout_s to be a number"
            )

        workspace_persistence = options.workspace_persistence
        if workspace_persistence not in (
            _WORKSPACE_PERSISTENCE_TAR,
            _WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM,
        ):
            raise ValueError(
                "ModalSandboxClient.create requires workspace_persistence to be one of "
                f"{_WORKSPACE_PERSISTENCE_TAR!r} or {_WORKSPACE_PERSISTENCE_SNAPSHOT_FILESYSTEM!r}"
            )

        snapshot_filesystem_timeout_s = options.snapshot_filesystem_timeout_s
        if snapshot_filesystem_timeout_s is not None and not isinstance(
            snapshot_filesystem_timeout_s, (int, float)
        ):
            raise ValueError(
                "ModalSandboxClient.create requires snapshot_filesystem_timeout_s to be a number"
            )

        snapshot_filesystem_restore_timeout_s = options.snapshot_filesystem_restore_timeout_s
        if snapshot_filesystem_restore_timeout_s is not None and not isinstance(
            snapshot_filesystem_restore_timeout_s, (int, float)
        ):
            raise ValueError(
                "ModalSandboxClient.create requires "
                "snapshot_filesystem_restore_timeout_s to be a number"
            )

        manifest = apply_codex_to_manifest(manifest, codex)

        session_id = uuid.uuid4()
        state_image_id: str | None = None
        state_image_tag: str | None = None
        session_image: modal.Image | None = None
        if image_sel is not None:
            if image_sel.kind == "image":
                if not isinstance(image_sel.value, modal.Image):
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires image to be a modal.Image"
                    )
                session_image = image_sel.value
                state_image_id = getattr(session_image, "object_id", None)
            elif image_sel.kind == "id":
                if not isinstance(image_sel.value, str) or not image_sel.value:
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires image_id to be a non-empty string"
                    )
                state_image_id = image_sel.value
            else:
                if not isinstance(image_sel.value, str) or not image_sel.value:
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires image_tag to be a non-empty string"
                    )
                state_image_tag = image_sel.value

        state_sandbox_id: str | None = None
        session_sandbox: modal.Sandbox | None = None
        if sandbox_sel is not None:
            if sandbox_sel.kind == "sandbox":
                if not isinstance(sandbox_sel.value, modal.Sandbox):
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires sandbox to be a modal.Sandbox"
                    )
                session_sandbox = sandbox_sel.value
                state_sandbox_id = getattr(session_sandbox, "object_id", None)
            else:
                if not isinstance(sandbox_sel.value, str) or not sandbox_sel.value:
                    raise ValueError(
                        "ModalSandboxClient.__init__ requires sandbox_id to be a non-empty string"
                    )
                state_sandbox_id = sandbox_sel.value

        snapshot_id = str(session_id)
        snapshot_instance = resolve_snapshot(snapshot, snapshot_id)
        state = ModalSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            app_name=app_name,
            image_tag=state_image_tag,
            image_id=state_image_id,
            sandbox_id=state_sandbox_id,
            workspace_persistence=workspace_persistence,
        )
        if sandbox_create_timeout_s is not None:
            state.sandbox_create_timeout_s = float(sandbox_create_timeout_s)
        if snapshot_filesystem_timeout_s is not None:
            state.snapshot_filesystem_timeout_s = float(snapshot_filesystem_timeout_s)
        if snapshot_filesystem_restore_timeout_s is not None:
            state.snapshot_filesystem_restore_timeout_s = float(
                snapshot_filesystem_restore_timeout_s
            )

        # Pass the in-memory handles through to the session (they may not be resumable).
        inner = ModalSandboxSession.from_state(
            state,
            image=session_image,
            sandbox=session_sandbox,
        )
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        """
        Best-effort cleanup of Modal sandbox resources.
        """

        inner = session._inner
        if not isinstance(inner, ModalSandboxSession):
            raise TypeError("ModalSandboxClient.delete expects a ModalSandboxSession")

        # Prefer the live handle if present.
        sandbox = getattr(inner, "_sandbox", None)
        try:
            if sandbox is not None:
                await asyncio.get_running_loop().run_in_executor(None, sandbox.terminate)
                return session
        except Exception:
            return session

        # Otherwise, best-effort terminate via sandbox_id.
        sid = inner.state.sandbox_id
        if sid:
            try:
                sb = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: modal.Sandbox.from_id(sid)
                )
                await asyncio.get_running_loop().run_in_executor(None, sb.terminate)
            except Exception:
                pass

        return session

    async def resume(
        self,
        state: SandboxSessionState,
        *,
        codex: bool | CodexConfig = False,
    ) -> SandboxSession:
        if not isinstance(state, ModalSandboxSessionState):
            raise TypeError("ModalSandboxClient.resume expects a ModalSandboxSessionState")
        inner = ModalSandboxSession.from_state(apply_codex_to_session_state(state, codex))
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return ModalSandboxSessionState.model_validate(payload)
