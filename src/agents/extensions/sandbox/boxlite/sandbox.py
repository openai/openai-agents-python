"""BoxLite sandbox (https://github.com/boxlite-ai/boxlite) implementation.

BoxLite is a local-first micro-VM sandbox for AI agents with hardware-level isolation
(KVM on Linux, Hypervisor.framework on macOS) and no daemon. This module wires
``boxlite.SimpleBox`` into the Agents SDK sandbox contract.

The ``boxlite`` dependency is optional, so package-level exports guard the import of
this module. Within this module, BoxLite imports are normal so users with the extra
installed get full type navigation.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from boxlite import SimpleBox

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecNonZeroError,
    ExecTimeoutError,
    ExecTransportError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session import SandboxSession, SandboxSessionState
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.dependencies import Dependencies
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, User
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tarfile

DEFAULT_BOXLITE_WORKSPACE_ROOT = "/workspace"
_DEFAULT_MANIFEST_ROOT = cast(str, Manifest.model_fields["root"].default)


def _resolve_manifest_root(manifest: Manifest | None) -> Manifest:
    if manifest is None:
        return Manifest(root=DEFAULT_BOXLITE_WORKSPACE_ROOT)
    if manifest.root == _DEFAULT_MANIFEST_ROOT:
        return manifest.model_copy(update={"root": DEFAULT_BOXLITE_WORKSPACE_ROOT})
    return manifest


def _to_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return str(value).encode("utf-8")


class BoxliteSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the BoxLite sandbox backend."""

    type: Literal["boxlite"] = "boxlite"
    image: str | None = None
    rootfs_path: str | None = None
    cpus: int | None = None
    memory_mib: int | None = None
    auto_remove: bool = True
    name: str | None = None
    reuse_existing: bool = False
    env: dict[str, str] | None = None

    def __init__(
        self,
        image: str | None = None,
        rootfs_path: str | None = None,
        cpus: int | None = None,
        memory_mib: int | None = None,
        auto_remove: bool = True,
        name: str | None = None,
        reuse_existing: bool = False,
        env: dict[str, str] | None = None,
        *,
        type: Literal["boxlite"] = "boxlite",
    ) -> None:
        super().__init__(
            type=type,
            image=image,
            rootfs_path=rootfs_path,
            cpus=cpus,
            memory_mib=memory_mib,
            auto_remove=auto_remove,
            name=name,
            reuse_existing=reuse_existing,
            env=env,
        )


class BoxliteSandboxSessionState(SandboxSessionState):
    """Serializable state for a BoxLite-backed session."""

    type: Literal["boxlite"] = "boxlite"
    box_id: str = ""
    image: str | None = None
    rootfs_path: str | None = None
    cpus: int | None = None
    memory_mib: int | None = None
    auto_remove: bool = True
    name: str | None = None
    env: dict[str, str] | None = None


class BoxliteSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by a BoxLite SimpleBox."""

    state: BoxliteSandboxSessionState
    _box: SimpleBox | None

    def __init__(
        self,
        *,
        state: BoxliteSandboxSessionState,
        box: SimpleBox | None = None,
    ) -> None:
        self.state = state
        self._box = box

    @classmethod
    def from_state(
        cls,
        state: BoxliteSandboxSessionState,
        *,
        box: SimpleBox | None = None,
    ) -> BoxliteSandboxSession:
        return cls(state=state, box=box)

    def supports_pty(self) -> bool:
        return False

    def _reject_user_arg(self, *, op: Literal["exec", "read", "write"], user: str | User) -> None:
        user_name = user.name if isinstance(user, User) else user
        raise ConfigurationError(
            message=(
                "BoxliteSandboxSession does not support sandbox-local users; "
                f"`{op}` must be called without `user`"
            ),
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op=op,
            context={"backend": "boxlite", "user": user_name},
        )

    def _prepare_exec_command(
        self,
        *command: str | Path,
        shell: bool | list[str],
        user: str | User | None,
    ) -> list[str]:
        if user is not None:
            self._reject_user_arg(op="exec", user=user)
        return super()._prepare_exec_command(*command, shell=shell, user=user)

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _validate_tar_bytes(self, raw: bytes) -> None:
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
                validate_tarfile(tar)
        except UnsafeTarMemberError as err:
            raise ValueError(str(err)) from err
        except (tarfile.TarError, OSError) as err:
            raise ValueError("invalid tar stream") from err

    async def _ensure_box(self) -> SimpleBox:
        box = self._box
        if box is not None:
            return box

        box = SimpleBox(
            image=self.state.image,
            rootfs_path=self.state.rootfs_path,
            cpus=self.state.cpus,
            memory_mib=self.state.memory_mib,
            auto_remove=self.state.auto_remove,
            name=self.state.name,
        )
        await box.start()
        self._box = box
        try:
            self.state.box_id = box.id
        except Exception:
            self.state.box_id = self.state.name or ""
        return box

    async def _prepare_backend_workspace(self) -> None:
        root = PurePosixPath(os.path.normpath(self.state.manifest.root))
        try:
            box = await self._ensure_box()
            result = await box.exec("mkdir", "-p", "--", root.as_posix())
        except Exception as err:
            raise WorkspaceStartError(path=Path(str(root)), cause=err) from err

        exit_code = int(getattr(result, "exit_code", 0) or 0)
        if exit_code != 0:
            raise WorkspaceStartError(
                path=Path(str(root)),
                context={
                    "exit_code": exit_code,
                    "stdout": _to_bytes(getattr(result, "stdout", b"")).decode(
                        "utf-8", errors="replace"
                    ),
                    "stderr": _to_bytes(getattr(result, "stderr", b"")).decode(
                        "utf-8", errors="replace"
                    ),
                },
            )

    async def running(self) -> bool:
        box = self._box
        if box is None:
            return False
        try:
            info = box.info()
        except Exception:
            return False
        state = getattr(info, "state", None) or getattr(info, "status", None)
        if state is None:
            return True
        return str(state).lower() in {"running", "up", "started"}

    async def shutdown(self) -> None:
        box = self._box
        if box is None:
            return
        try:
            await box.__aexit__(None, None, None)
        except Exception:
            pass
        finally:
            self._box = None

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        box = await self._ensure_box()
        normalized = [str(part) for part in command]
        if not normalized:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        try:
            result = await asyncio.wait_for(
                box.exec(
                    normalized[0],
                    *normalized[1:],
                    cwd=self.state.manifest.root,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as err:
            raise ExecTimeoutError(command=normalized, timeout_s=timeout, cause=err) from err
        except ExecTimeoutError:
            raise
        except Exception as err:
            raise ExecTransportError(
                command=normalized,
                context={"backend": "boxlite", "box_id": self.state.box_id},
                cause=err,
            ) from err

        return ExecResult(
            stdout=_to_bytes(getattr(result, "stdout", b"")),
            stderr=_to_bytes(getattr(result, "stderr", b"")),
            exit_code=int(getattr(result, "exit_code", 0) or 0),
        )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            self._reject_user_arg(op="read", user=user)

        normalized_path = await self._validate_path_access(path)
        cmd = ("sh", "-c", 'base64 "$1" || exit 1', "--", str(normalized_path))
        try:
            result = await self._exec_internal(*cmd)
        except Exception as err:
            raise WorkspaceArchiveReadError(path=normalized_path, cause=err) from err

        if result.exit_code != 0:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            if "No such file" in stderr or "not found" in stderr.lower():
                raise WorkspaceReadNotFoundError(path=normalized_path)
            raise WorkspaceArchiveReadError(
                path=normalized_path,
                cause=ExecNonZeroError(
                    result,
                    command=cmd,
                    context={"backend": "boxlite", "box_id": self.state.box_id},
                ),
            )

        try:
            payload = base64.b64decode(result.stdout)
        except Exception as err:
            raise WorkspaceArchiveReadError(path=normalized_path, cause=err) from err
        return io.BytesIO(payload)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        if user is not None:
            self._reject_user_arg(op="write", user=user)

        normalized_path = await self._validate_path_access(path, for_write=True)
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=normalized_path,
                actual_type=type(payload).__name__,
            )

        encoded = base64.b64encode(bytes(payload)).decode("ascii")
        parent = str(PurePosixPath(str(normalized_path)).parent)
        cmd = (
            "sh",
            "-c",
            'mkdir -p "$1" && printf "%s" "$2" | base64 -d > "$3"',
            "--",
            parent,
            encoded,
            str(normalized_path),
        )
        try:
            result = await self._exec_internal(*cmd)
        except Exception as err:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=err) from err

        if result.exit_code != 0:
            raise WorkspaceArchiveWriteError(
                path=normalized_path,
                cause=ExecNonZeroError(
                    result,
                    command=cmd,
                    context={"backend": "boxlite", "box_id": self.state.box_id},
                ),
            )

    async def persist_workspace(self) -> io.IOBase:
        root = Path(self.state.manifest.root)
        archive_path = f"/tmp/openai-agents-{self.state.session_id.hex}.tar"
        excludes = [
            f"--exclude=./{rel_path.as_posix()}"
            for rel_path in sorted(
                self._persist_workspace_skip_relpaths(),
                key=lambda item: item.as_posix(),
            )
        ]
        tar_cmd = ("tar", "cf", archive_path, *excludes, ".")
        try:
            result = await self.exec(*tar_cmd, shell=False)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    cause=ExecNonZeroError(
                        result,
                        command=tar_cmd,
                        context={"backend": "boxlite", "box_id": self.state.box_id},
                    ),
                )
            buf = await self.read(Path(archive_path))
            return buf
        except WorkspaceArchiveReadError:
            raise
        except Exception as err:
            raise WorkspaceArchiveReadError(path=root, cause=err) from err
        finally:
            try:
                await self._exec_internal("rm", "-f", archive_path)
            except Exception:
                pass

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=Path(self.state.manifest.root),
                actual_type=type(raw).__name__,
            )

        root = Path(self.state.manifest.root)
        archive_path = f"/tmp/openai-agents-{self.state.session_id.hex}.tar"
        try:
            self._validate_tar_bytes(bytes(raw))
            await self.mkdir(root, parents=True)
            await self.write(Path(archive_path), io.BytesIO(bytes(raw)))
            tar_cmd = ("tar", "xf", archive_path, "-C", str(root))
            result = await self.exec(*tar_cmd, shell=False)
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    cause=ExecNonZeroError(
                        result,
                        command=tar_cmd,
                        context={"backend": "boxlite", "box_id": self.state.box_id},
                    ),
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as err:
            raise WorkspaceArchiveWriteError(path=root, cause=err) from err
        finally:
            try:
                await self._exec_internal("rm", "-f", archive_path)
            except Exception:
                pass


class BoxliteSandboxClient(BaseSandboxClient[BoxliteSandboxClientOptions]):
    """BoxLite-backed sandbox client."""

    backend_id = "boxlite"
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    def _build_state(
        self,
        options: BoxliteSandboxClientOptions,
        *,
        manifest: Manifest,
        snapshot: SnapshotBase,
        session_id: uuid.UUID,
    ) -> BoxliteSandboxSessionState:
        return BoxliteSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot,
            image=options.image,
            rootfs_path=options.rootfs_path,
            cpus=options.cpus,
            memory_mib=options.memory_mib,
            auto_remove=options.auto_remove,
            name=options.name,
            env=dict(options.env or {}) or None,
        )

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: BoxliteSandboxClientOptions,
    ) -> SandboxSession:
        if not options.image and not options.rootfs_path:
            raise ConfigurationError(
                message="BoxliteSandboxClientOptions requires `image` or `rootfs_path`",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": "boxlite"},
            )
        resolved_manifest = _resolve_manifest_root(manifest)
        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = self._build_state(
            options,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            session_id=session_id,
        )
        inner = BoxliteSandboxSession.from_state(state)
        await inner._ensure_box()
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, BoxliteSandboxSession):
            raise TypeError("BoxliteSandboxClient.delete expects a BoxliteSandboxSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, BoxliteSandboxSessionState):
            raise TypeError("BoxliteSandboxClient.resume expects a BoxliteSandboxSessionState")
        state.workspace_root_ready = False
        inner = BoxliteSandboxSession.from_state(state)
        await inner._ensure_box()
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return BoxliteSandboxSessionState.model_validate(payload)


__all__ = [
    "BoxliteSandboxClient",
    "BoxliteSandboxClientOptions",
    "BoxliteSandboxSession",
    "BoxliteSandboxSessionState",
    "DEFAULT_BOXLITE_WORKSPACE_ROOT",
]
