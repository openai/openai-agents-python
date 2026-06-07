"""
Sailbox sandbox implementation.

This module provides a Sailbox-backed sandbox client/session implementation backed by
`sail.sailbox.Sailbox`.

The `sail-sdk` dependency is optional, so package-level exports should guard imports of this
module. Within this module, Sail SDK imports are normal so users with the extra installed get
full type navigation.
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import shlex
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar, cast
from urllib.parse import urlsplit

from pydantic import field_serializer, field_validator
from sail.app import App
from sail.image import Image, ImageDefinition
from sail.sailbox import Sailbox

from ....sandbox.errors import (
    ExecTransportError,
    ExposedPortUnavailableError,
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
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER
from ....sandbox.session.sandbox_client import (
    BaseSandboxClient,
    BaseSandboxClientOptions,
)
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import sandbox_path_str

_DEFAULT_APP_NAME = "openai-agents-sandbox"
_DEFAULT_NAME_PREFIX = "openai-agent"
_DEFAULT_MEMORY_MIB = 1024
_DEFAULT_CPU = 1
_DEFAULT_DISK_GIB = 8
_DEFAULT_IMAGE_BUILD_TIMEOUT = 1800
_UNSET = object()

R = TypeVar("R")


async def _call_sailbox(fn: Callable[..., R], *args: object, **kwargs: object) -> R:
    return await asyncio.to_thread(fn, *args, **kwargs)


def _sailbox_provider_error_detail(error: BaseException) -> str | None:
    message = str(error)
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if isinstance(status, int):
        if message:
            return f"HTTP {status}: {message}"
        return f"HTTP {status}"
    if message:
        return f"{type(error).__name__}: {message}"
    return type(error).__name__


def _sailbox_error_context(
    *,
    cause: BaseException,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {"backend": "sailbox", **(extra or {})}
    detail = _sailbox_provider_error_detail(cause)
    if detail:
        context["provider_error"] = detail
    status = getattr(cause, "status_code", None) or getattr(cause, "status", None)
    if isinstance(status, int):
        context["http_status"] = status
    return context


def _sailbox_error_message(prefix: str, cause: BaseException) -> str:
    detail = _sailbox_provider_error_detail(cause)
    if detail:
        return f"{prefix}: {detail}"
    return prefix


def _sailbox_exec_output_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    if value is None:
        return b""
    return str(value).encode("utf-8", errors="replace")


def _sailbox_exec_output_text(value: object) -> str:
    return _sailbox_exec_output_bytes(value).decode("utf-8", errors="replace")


def _serialize_sail_image(image: ImageDefinition | None) -> dict[str, str | None] | None:
    if image is None:
        return None
    to_proto = getattr(image, "to_proto", None)
    if not callable(to_proto):
        return None
    spec = to_proto()
    serialize = getattr(spec, "SerializeToString", None)
    if not callable(serialize):
        return None
    return {
        "image_id": getattr(image, "_image_id", None),
        "spec": base64.b64encode(serialize()).decode("ascii"),
    }


def _deserialize_sail_image(image: object) -> object:
    if isinstance(image, dict):
        raw_spec = image.get("spec")
        if not isinstance(raw_spec, str):
            return None
        raw_image_id = image.get("image_id")
        image_id = raw_image_id if isinstance(raw_image_id, str) else None
        try:
            from sail.pb.image.v1 import image_pb2

            image_spec_cls = cast(Any, image_pb2).ImageSpec
            spec = image_spec_cls()
            spec.ParseFromString(base64.b64decode(raw_spec))
            return ImageDefinition(spec, _image_id=image_id)
        except Exception:
            return None
    if isinstance(image, str):
        try:
            from sail.pb.image.v1 import image_pb2

            image_spec_cls = cast(Any, image_pb2).ImageSpec
            return ImageDefinition(image_spec_cls(), _image_id=image)
        except Exception:
            return None
    return image


class SailboxSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for creating OpenAI Agents SDK sessions on Sailboxes."""

    type: Literal["sailbox"] = "sailbox"
    app: App | None = None
    app_name: str | None = _DEFAULT_APP_NAME
    image: ImageDefinition | None = None
    name_prefix: str = _DEFAULT_NAME_PREFIX
    image_build_timeout: int = _DEFAULT_IMAGE_BUILD_TIMEOUT
    memory_mib: int = _DEFAULT_MEMORY_MIB
    cpu: int = _DEFAULT_CPU
    disk_gib: int = _DEFAULT_DISK_GIB
    exposed_ports: tuple[int, ...] = ()
    pause_on_exit: bool = False

    def __init__(
        self,
        app: App | None | object = _UNSET,
        app_name: str | None | object = _UNSET,
        image: ImageDefinition | None | object = _UNSET,
        name_prefix: str | object = _UNSET,
        image_build_timeout: int | object = _UNSET,
        memory_mib: int | object = _UNSET,
        cpu: int | object = _UNSET,
        disk_gib: int | object = _UNSET,
        exposed_ports: tuple[int, ...] | object = _UNSET,
        pause_on_exit: bool | object = _UNSET,
        *,
        type: Literal["sailbox"] = "sailbox",
    ) -> None:
        values: dict[str, object] = {"type": type}
        if app is not _UNSET:
            values["app"] = app
        if app_name is not _UNSET:
            values["app_name"] = app_name
        if image is not _UNSET:
            values["image"] = image
        if name_prefix is not _UNSET:
            values["name_prefix"] = name_prefix
        if image_build_timeout is not _UNSET:
            values["image_build_timeout"] = image_build_timeout
        if memory_mib is not _UNSET:
            values["memory_mib"] = memory_mib
        if cpu is not _UNSET:
            values["cpu"] = cpu
        if disk_gib is not _UNSET:
            values["disk_gib"] = disk_gib
        if exposed_ports is not _UNSET:
            values["exposed_ports"] = tuple(cast(Any, exposed_ports))
        if pause_on_exit is not _UNSET:
            values["pause_on_exit"] = pause_on_exit
        super().__init__(**values)

    @field_serializer("app", when_used="json")
    def _serialize_app(self, app: App | None) -> dict[str, object] | None:
        if app is None:
            return None
        return {"id": app.id, "name": app.name, "created_at": app.created_at}

    @field_validator("app", mode="before")
    @classmethod
    def _deserialize_app(cls, app: object) -> object:
        if isinstance(app, str):
            return App(id=app, name=app, created_at=0)
        if isinstance(app, dict):
            app_id = app.get("id")
            name = app.get("name")
            created_at = app.get("created_at")
            if isinstance(app_id, str) and isinstance(name, str) and isinstance(created_at, int):
                return App(id=app_id, name=name, created_at=created_at)
        return app

    @field_serializer("image", when_used="json")
    def _serialize_image(self, image: ImageDefinition | None) -> dict[str, str | None] | None:
        return _serialize_sail_image(image)

    @field_validator("image", mode="before")
    @classmethod
    def _deserialize_image(cls, image: object) -> object:
        return _deserialize_sail_image(image)


class SailboxSandboxSessionState(SandboxSessionState):
    """Serializable state for a Sailbox-backed OpenAI Agents SDK session."""

    type: Literal["sailbox"] = "sailbox"
    sailbox_id: str = ""
    sailbox_name: str = ""
    app_name: str | None = None
    exec_endpoint: str = ""
    worker_address: str = ""
    status: str = ""
    image: ImageDefinition | None = None
    image_build_timeout: int = _DEFAULT_IMAGE_BUILD_TIMEOUT
    memory_mib: int = _DEFAULT_MEMORY_MIB
    cpu: int = _DEFAULT_CPU
    disk_gib: int = _DEFAULT_DISK_GIB
    pause_on_exit: bool = False

    @field_serializer("image", when_used="json")
    def _serialize_image(self, image: ImageDefinition | None) -> dict[str, str | None] | None:
        return _serialize_sail_image(image)

    @field_validator("image", mode="before")
    @classmethod
    def _deserialize_image(cls, image: object) -> object:
        return _deserialize_sail_image(image)


class SailboxSandboxSession(BaseSandboxSession):
    """OpenAI Agents SDK sandbox session backed by a single Sailbox."""

    state: SailboxSandboxSessionState

    def __init__(
        self,
        *,
        state: SailboxSandboxSessionState,
        sailbox: Sailbox | None = None,
    ) -> None:
        self.state = state
        self._sailbox = sailbox

    @classmethod
    def from_state(
        cls,
        state: SailboxSandboxSessionState,
        *,
        sailbox: Sailbox | None = None,
    ) -> SailboxSandboxSession:
        return cls(state=state, sailbox=sailbox)

    @property
    def sailbox(self) -> Sailbox:
        if self._sailbox is None:
            raise RuntimeError("sailbox session has not been started")
        return self._sailbox

    def _set_sailbox(self, sailbox: Sailbox) -> None:
        self._sailbox = sailbox
        self.state.sailbox_id = sailbox.sailbox_id
        self.state.sailbox_name = sailbox.name
        self.state.status = sailbox.status
        self.state.worker_address = sailbox.worker_address
        self.state.exec_endpoint = sailbox.exec_endpoint

    async def _resume_sailbox(self, sailbox: Sailbox) -> None:
        try:
            resumed = await _call_sailbox(sailbox.resume)
        except Exception as exc:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context=_sailbox_error_context(
                    cause=exc,
                    extra={
                        "reason": "resume_failed",
                        "sailbox_id": self.state.sailbox_id or sailbox.sailbox_id,
                    },
                ),
                cause=exc,
                message=_sailbox_error_message("Sailbox resume failed", exc),
            ) from exc
        if resumed is not None:
            sailbox = resumed
        self._set_sailbox(sailbox)

    async def _ensure_backend_started(self) -> None:
        if self._sailbox is not None:
            if self._sailbox.status != "running":
                await self._resume_sailbox(self._sailbox)
                self._set_start_state_preserved(True)
            return

        if not self.state.sailbox_id:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context={"reason": "missing_sailbox_id"},
            )

        try:
            sailbox = await _call_sailbox(_connect_sailbox, self.state.sailbox_id)
        except Exception as exc:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context=_sailbox_error_context(
                    cause=exc,
                    extra={
                        "reason": "connect_failed",
                        "sailbox_id": self.state.sailbox_id,
                    },
                ),
                cause=exc,
                message=_sailbox_error_message("Sailbox connect failed", exc),
            ) from exc
        self._set_sailbox(sailbox)
        if sailbox.status != "running":
            await self._resume_sailbox(sailbox)
        self._set_start_state_preserved(True)

    async def _prepare_backend_workspace(self) -> None:
        root = self.state.manifest.root
        try:
            request = await _call_sailbox(
                self.sailbox.exec,
                f"mkdir -p {shlex.quote(root)}",
            )
            result = await _call_sailbox(request.wait)
        except Exception as exc:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context=_sailbox_error_context(
                    cause=exc,
                    extra={"reason": "mkdir_failed"},
                ),
                cause=exc,
                message=_sailbox_error_message("Sailbox workspace root preparation failed", exc),
            ) from exc
        if result.returncode != 0:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context={
                    "exit_code": result.returncode,
                    "stdout": _sailbox_exec_output_text(result.stdout),
                    "stderr": _sailbox_exec_output_text(result.stderr),
                },
            )

    async def _shutdown_backend(self) -> None:
        sailbox = self._sailbox
        if sailbox is None:
            return
        if self.state.pause_on_exit:
            await _call_sailbox(sailbox.pause)
            self.state.status = "paused"
            self.state.worker_address = ""
            return
        await _call_sailbox(sailbox.terminate)
        self.state.status = "terminated"
        self.state.worker_address = ""

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        command_tuple = tuple(str(part) for part in command)
        shell_command = await self._shell_command(command_tuple)
        try:
            request = await _call_sailbox(
                self.sailbox.exec,
                shell_command,
                timeout=_coerce_timeout(timeout),
            )
            result = await _call_sailbox(request.wait)
        except Exception as exc:
            raise ExecTransportError(
                command=command_tuple,
                context=_sailbox_error_context(
                    cause=exc,
                    extra={"sailbox_id": self.state.sailbox_id},
                ),
                cause=exc,
                message=_sailbox_error_message("Sailbox exec failed", exc),
            ) from exc

        return ExecResult(
            stdout=_sailbox_exec_output_bytes(result.stdout),
            stderr=_sailbox_exec_output_bytes(result.stderr),
            exit_code=result.returncode,
        )

    async def _shell_command(self, command: tuple[str, ...]) -> str:
        env = await self.state.manifest.environment.resolve()
        parts = ["cd", shlex.quote(self.state.manifest.root), "&&"]
        if env:
            parts.append("env")
            parts.extend(f"{key}={shlex.quote(value)}" for key, value in sorted(env.items()))
        parts.append(shlex.join(command))
        return " ".join(parts)

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            listener = await _call_sailbox(self.sailbox.listener, port)
        except Exception as exc:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context=_sailbox_error_context(
                    cause=exc,
                    extra={"detail": "listener_lookup_failed"},
                ),
                cause=exc,
            ) from exc

        parsed = urlsplit(listener.url)
        if not parsed.hostname:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={
                    "backend": "sailbox",
                    "detail": "invalid_listener_url",
                    "url": listener.url,
                },
            )
        tls = parsed.scheme in {"https", "wss"}
        endpoint_port = parsed.port or (443 if tls else 80)
        return ExposedPortEndpoint(
            host=parsed.hostname,
            port=endpoint_port,
            tls=tls,
            query=parsed.query,
        )

    async def _validate_path_access(
        self,
        path: Path | str,
        *,
        for_write: bool = False,
    ) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[Any, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> str | None:
        return self.state.sailbox_id or None

    async def read(
        self,
        path: Path | str,
        *,
        user: str | User | None = None,
    ) -> io.IOBase:
        error_path = Path(path)
        if user is not None:
            await self._check_read_as_user(path, error_path=error_path, user=user)

        workspace_path = await self._validate_path_access(path)
        try:
            data = await _call_sailbox(
                self.sailbox.read,
                sandbox_path_str(workspace_path),
            )
        except FileNotFoundError as exc:
            raise WorkspaceReadNotFoundError(path=error_path, cause=exc) from exc
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=error_path, cause=exc) from exc
        return io.BytesIO(data)

    async def _check_read_as_user(
        self,
        path: Path | str,
        *,
        error_path: Path,
        user: str | User,
    ) -> Path:
        workspace_path = await self._validate_path_access(path)
        user_name = user.name if isinstance(user, User) else user
        path_arg = sandbox_path_str(workspace_path)
        try:
            request = await _call_sailbox(
                self.sailbox.exec,
                " ".join(
                    [
                        "runuser",
                        "-u",
                        shlex.quote(user_name),
                        "--",
                        "sh",
                        "-lc",
                        shlex.quote('[ -r "$1" ]'),
                        "sh",
                        shlex.quote(path_arg),
                    ]
                ),
            )
            result = await _call_sailbox(request.wait)
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=error_path, cause=exc) from exc
        if result.returncode != 0:
            raise WorkspaceReadNotFoundError(
                path=error_path,
                context={
                    "command": ["runuser", "-u", user_name, "--", "sh", "-lc", "<read_check>"],
                    "stdout": _sailbox_exec_output_text(result.stdout),
                    "stderr": _sailbox_exec_output_text(result.stderr),
                },
            )
        return workspace_path

    async def write(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=Path(path), actual_type=type(payload).__name__)

        workspace_path = await self._validate_path_access(path, for_write=True)
        if user is not None:
            await self._write_payload_as_user(workspace_path, bytes(payload), user=user)
            return

        try:
            await _call_sailbox(
                self.sailbox.write,
                sandbox_path_str(workspace_path),
                bytes(payload),
            )
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=exc) from exc

    async def _write_payload_as_user(
        self,
        workspace_path: Path,
        payload: bytes,
        *,
        user: str | User,
    ) -> None:
        user_name = user.name if isinstance(user, User) else user
        temp_path = f"/tmp/openai-agents-write-{self.state.session_id.hex}-{uuid.uuid4().hex}"
        target_path = sandbox_path_str(workspace_path)
        write_script = (
            'tmp="$1"\n'
            'target="$2"\n'
            'if [ -e "$target" ]; then\n'
            '    [ -f "$target" ] && [ -w "$target" ] || exit $?\n'
            "else\n"
            '    parent=$(dirname "$target")\n'
            '    while [ ! -e "$parent" ]; do\n'
            '        next=$(dirname "$parent")\n'
            '        if [ "$next" = "$parent" ]; then\n'
            "            exit 1\n"
            "        fi\n"
            '        parent="$next"\n'
            "    done\n"
            '    [ -d "$parent" ] && [ -w "$parent" ] && [ -x "$parent" ] || exit $?\n'
            "fi\n"
            'mkdir -p "$(dirname "$target")" && cat "$tmp" > "$target"\n'
        )
        try:
            await _call_sailbox(self.sailbox.write, temp_path, payload)
            # Sailbox's file API does not accept a user. Stage the bytes in /tmp,
            # then switch user inside the guest for the final copy. The base
            # exec(user=...) wrapper uses sudo, which is not present in Sailbox
            # base images, so this path uses runuser directly.
            request = await _call_sailbox(
                self.sailbox.exec,
                " ".join(
                    [
                        "runuser",
                        "-u",
                        shlex.quote(user_name),
                        "--",
                        "sh",
                        "-lc",
                        shlex.quote(write_script),
                        "sh",
                        shlex.quote(temp_path),
                        shlex.quote(target_path),
                    ]
                ),
            )
            result = await _call_sailbox(request.wait)
            if result.returncode != 0:
                raise WorkspaceArchiveWriteError(
                    path=workspace_path,
                    context={
                        "reason": "write_as_user_nonzero_exit",
                        "exit_code": result.returncode,
                        "stdout": _sailbox_exec_output_text(result.stdout),
                        "stderr": _sailbox_exec_output_text(result.stderr),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=exc) from exc
        finally:
            try:
                await self.exec("rm", "-f", temp_path, shell=False)
            except Exception:
                pass

    async def running(self) -> bool:
        if self._sailbox is None:
            return False
        sailbox_id = self.state.sailbox_id or self._sailbox.sailbox_id
        if not sailbox_id:
            return False
        try:
            info = await _call_sailbox(Sailbox.get, sailbox_id)
        except LookupError:
            self.state.status = "terminated"
            object.__setattr__(self._sailbox, "status", "terminated")
            return False
        except Exception:
            return False
        self.state.status = info.status
        object.__setattr__(self._sailbox, "status", info.status)
        return info.status == "running"

    async def persist_workspace(self) -> io.IOBase:
        root = self.state.manifest.root
        archive_path = f"/tmp/openai-agents-{self.state.session_id.hex}.tar"
        excludes = " ".join(
            "--exclude=" + shlex.quote(f"./{path.as_posix()}")
            for path in sorted(
                self._persist_workspace_skip_relpaths(),
                key=lambda item: item.as_posix(),
            )
        )
        command = f"cd {shlex.quote(root)} && tar cf {shlex.quote(archive_path)} {excludes} ."
        try:
            result = await self.exec(command)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=self._workspace_root_path(),
                    context={
                        "exit_code": result.exit_code,
                        "stdout": _sailbox_exec_output_text(result.stdout),
                        "stderr": _sailbox_exec_output_text(result.stderr),
                    },
                )
            data = await _call_sailbox(self.sailbox.read, archive_path)
            return io.BytesIO(data)
        except WorkspaceArchiveReadError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveReadError(
                path=self._workspace_root_path(),
                cause=exc,
            ) from exc
        finally:
            try:
                await self.exec("rm", "-f", archive_path, shell=False)
            except Exception:
                pass

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceArchiveWriteError(
                path=self._workspace_root_path(),
                context={
                    "reason": "invalid_archive_payload",
                    "type": type(raw).__name__,
                },
            )
        try:
            validate_tar_bytes(bytes(raw), allow_external_symlink_targets=False)
        except UnsafeTarMemberError as exc:
            raise WorkspaceArchiveWriteError(
                path=self._workspace_root_path(),
                context={"reason": exc.reason, "member": exc.member},
                cause=exc,
            ) from exc

        root = self.state.manifest.root
        archive_path = f"/tmp/openai-agents-{self.state.session_id.hex}.tar"
        try:
            await _call_sailbox(self.sailbox.write, archive_path, bytes(raw))
            mkdir = await self.exec("mkdir", "-p", root, shell=False)
            if not mkdir.ok():
                raise WorkspaceArchiveWriteError(
                    path=self._workspace_root_path(),
                    context={
                        "exit_code": mkdir.exit_code,
                        "stdout": _sailbox_exec_output_text(mkdir.stdout),
                        "stderr": _sailbox_exec_output_text(mkdir.stderr),
                    },
                )
            result = await self.exec(
                "tar",
                "xf",
                archive_path,
                "-C",
                root,
                shell=False,
            )
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=self._workspace_root_path(),
                    context={
                        "exit_code": result.exit_code,
                        "stdout": _sailbox_exec_output_text(result.stdout),
                        "stderr": _sailbox_exec_output_text(result.stderr),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveWriteError(
                path=self._workspace_root_path(),
                cause=exc,
            ) from exc
        finally:
            try:
                await self.exec("rm", "-f", archive_path, shell=False)
            except Exception:
                pass


class SailboxSandboxClient(BaseSandboxClient[SailboxSandboxClientOptions | None]):
    """OpenAI Agents SDK sandbox client that creates and resumes Sailboxes."""

    backend_id = "sailbox"
    supports_default_options = True

    def __init__(
        self,
        *,
        app: App | None = None,
        app_name: str | None = _DEFAULT_APP_NAME,
        image: ImageDefinition | None = None,
        name_prefix: str = _DEFAULT_NAME_PREFIX,
        image_build_timeout: int = _DEFAULT_IMAGE_BUILD_TIMEOUT,
        memory_mib: int = _DEFAULT_MEMORY_MIB,
        cpu: int = _DEFAULT_CPU,
        disk_gib: int = _DEFAULT_DISK_GIB,
        pause_on_exit: bool = False,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._app = app
        self._app_name = app_name
        self._image = image
        self._name_prefix = name_prefix
        self._image_build_timeout = image_build_timeout
        self._memory_mib = memory_mib
        self._cpu = cpu
        self._disk_gib = disk_gib
        self._pause_on_exit = pause_on_exit
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: SailboxSandboxClientOptions | None = None,
    ) -> SandboxSession:
        resolved_options = self._resolve_options(options)
        resolved_manifest = manifest or Manifest()
        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        sailbox = await self._create_sailbox(session_id, resolved_options)
        state = SailboxSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            sailbox_id=sailbox.sailbox_id,
            sailbox_name=sailbox.name,
            app_name=resolved_options.app_name,
            exec_endpoint=sailbox.exec_endpoint,
            worker_address=sailbox.worker_address,
            status=sailbox.status,
            image=resolved_options.image,
            image_build_timeout=resolved_options.image_build_timeout,
            memory_mib=resolved_options.memory_mib,
            cpu=resolved_options.cpu,
            disk_gib=resolved_options.disk_gib,
            exposed_ports=resolved_options.exposed_ports,
            pause_on_exit=resolved_options.pause_on_exit,
        )
        inner = SailboxSandboxSession.from_state(state, sailbox=sailbox)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, SailboxSandboxSession):
            raise TypeError("SailboxSandboxClient.delete expects a SailboxSandboxSession")

        sailbox = inner._sailbox
        if sailbox is None:
            if not inner.state.sailbox_id:
                return session
            try:
                sailbox = await _call_sailbox(_connect_sailbox, inner.state.sailbox_id)
            except Exception:
                return session

        try:
            await _call_sailbox(sailbox.terminate)
        except Exception:
            return session

        inner._sailbox = sailbox
        inner.state.status = "terminated"
        inner.state.worker_address = ""
        object.__setattr__(sailbox, "status", "terminated")
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, SailboxSandboxSessionState):
            raise TypeError("SailboxSandboxClient.resume expects a SailboxSandboxSessionState")

        try:
            sailbox = await _call_sailbox(_connect_sailbox, state.sailbox_id)
            state.sailbox_id = sailbox.sailbox_id
            state.sailbox_name = sailbox.name
            state.status = sailbox.status
            state.worker_address = sailbox.worker_address
            state.exec_endpoint = sailbox.exec_endpoint
            inner = SailboxSandboxSession.from_state(state, sailbox=sailbox)
            inner._set_start_state_preserved(True)
            return self._wrap_session(inner, instrumentation=self._instrumentation)
        except Exception:
            state.workspace_root_ready = False

        options = self._resolve_options(
            SailboxSandboxClientOptions(
                app_name=state.app_name or self._app_name,
                image=state.image,
                image_build_timeout=state.image_build_timeout,
                memory_mib=state.memory_mib,
                cpu=state.cpu,
                disk_gib=state.disk_gib,
                exposed_ports=state.exposed_ports,
                pause_on_exit=state.pause_on_exit,
            )
        )
        sailbox = await self._create_sailbox(state.session_id, options)
        state.sailbox_id = sailbox.sailbox_id
        state.sailbox_name = sailbox.name
        state.status = sailbox.status
        state.worker_address = sailbox.worker_address
        state.exec_endpoint = sailbox.exec_endpoint
        inner = SailboxSandboxSession.from_state(state, sailbox=sailbox)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return SailboxSandboxSessionState.model_validate(payload)

    def _resolve_options(
        self,
        options: SailboxSandboxClientOptions | None,
    ) -> SailboxSandboxClientOptions:
        def option_or_default(field: str, client_default: R) -> R:
            if options is not None and field in options.model_fields_set:
                return cast(R, getattr(options, field))
            return client_default

        def optional_option_or_default(field: str, client_default: R | None) -> R | None:
            if options is not None and field in options.model_fields_set:
                value = getattr(options, field)
                if value is not None:
                    return cast(R, value)
            return client_default

        if options is None:
            return SailboxSandboxClientOptions(
                app=self._app,
                app_name=self._app_name,
                image=self._image or Image.debian_arm64,
                name_prefix=self._name_prefix,
                image_build_timeout=self._image_build_timeout,
                memory_mib=self._memory_mib,
                cpu=self._cpu,
                disk_gib=self._disk_gib,
                pause_on_exit=self._pause_on_exit,
            )
        return SailboxSandboxClientOptions(
            app=optional_option_or_default("app", self._app),
            app_name=optional_option_or_default("app_name", self._app_name),
            image=optional_option_or_default("image", self._image) or Image.debian_arm64,
            name_prefix=option_or_default("name_prefix", self._name_prefix),
            image_build_timeout=option_or_default("image_build_timeout", self._image_build_timeout),
            memory_mib=option_or_default("memory_mib", self._memory_mib),
            cpu=option_or_default("cpu", self._cpu),
            disk_gib=option_or_default("disk_gib", self._disk_gib),
            exposed_ports=option_or_default("exposed_ports", ()),
            pause_on_exit=option_or_default("pause_on_exit", self._pause_on_exit),
        )

    async def _resolve_app(self, options: SailboxSandboxClientOptions) -> App:
        if options.app is not None:
            return options.app
        if not options.app_name:
            raise ValueError("SailboxSandboxClientOptions requires app or app_name")
        return await _call_sailbox(
            App.find,
            name=options.app_name,
            mint_if_missing=True,
        )

    async def _create_sailbox(
        self,
        session_id: uuid.UUID,
        options: SailboxSandboxClientOptions,
    ) -> Sailbox:
        try:
            app = await self._resolve_app(options)
        except Exception as exc:
            raise WorkspaceStartError(
                path=Path(options.name_prefix),
                context=_sailbox_error_context(
                    cause=exc,
                    extra={"reason": "resolve_app_failed"},
                ),
                cause=exc,
                message=_sailbox_error_message("Sailbox app resolution failed", exc),
            ) from exc
        image = options.image or self._image or Image.debian_arm64
        name = f"{options.name_prefix}-{session_id.hex[:12]}"
        try:
            return await _call_sailbox(
                Sailbox.create,
                image=image,
                app=app,
                name=name,
                image_build_timeout=options.image_build_timeout,
                memory_mib=options.memory_mib,
                cpu=options.cpu,
                ingress_ports=list(options.exposed_ports),
                disk_gib=options.disk_gib,
            )
        except Exception as exc:
            raise WorkspaceStartError(
                path=Path(options.name_prefix),
                context=_sailbox_error_context(
                    cause=exc,
                    extra={"reason": "create_sailbox_failed"},
                ),
                cause=exc,
                message=_sailbox_error_message("Sailbox create failed", exc),
            ) from exc


def _coerce_timeout(timeout: float | None) -> int | None:
    if timeout is None:
        return None
    if timeout <= 0:
        return 1
    return int(math.ceil(timeout))


def _connect_sailbox(sailbox_id: str) -> Sailbox:
    connect = getattr(Sailbox, "connect", None)
    if callable(connect):
        return cast(Sailbox, connect(sailbox_id))
    return Sailbox(
        sailbox_id=sailbox_id,
        name=sailbox_id,
        status="paused",
        worker_address="",
        exec_endpoint="",
    ).resume()


__all__ = [
    "SailboxSandboxClient",
    "SailboxSandboxClientOptions",
    "SailboxSandboxSession",
    "SailboxSandboxSessionState",
]
