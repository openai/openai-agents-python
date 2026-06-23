"""
Upstash Box (https://upstash.com/docs/box) sandbox implementation.

Upstash Box does not ship a Python SDK, so this module talks to the Box REST API
(``/v2/box/...``) directly over HTTP using ``aiohttp``. The session extends
``BaseSandboxSession`` like the other hosted providers and maps the sandbox
contract onto the Box API: command execution, file read/write, exposed ports,
tar-based workspace persistence, and pause/resume lifecycle.

Note: The ``aiohttp`` dependency is optional (installed via the ``upstash-box``
extra), so package-level exports guard imports of this module. Within this
module we import ``aiohttp`` normally so IDEs can resolve and navigate types.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import aiohttp

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecTimeoutError,
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
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.session.tar_workspace import shell_tar_exclude_args
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

_DEFAULT_BASE_URL = "https://us-east-1.box.upstash.com"
# Box provisions the workspace at /workspace/home, so map the core default (/workspace) onto it
# the same way the TypeScript provider does.
_DEFAULT_WORKSPACE_ROOT = "/workspace/home"
# How long to keep retrying from-snapshot creation while the snapshot is still propagating.
_SNAPSHOT_NOT_READY_MAX_WAIT_S = 60.0
_SNAPSHOT_POLL_INTERVAL_S = 3.0
_DEFAULT_EXEC_TIMEOUT_S = 60.0
_DEFAULT_REQUEST_TIMEOUT_S = 120.0
_WORKSPACE_TAR_TIMEOUT_S = 300.0
_CREATE_POLL_INTERVAL_S = 2.0
_CREATE_MAX_WAIT_S = 300.0
# Box statuses that mean the sandbox is up and able to run commands.
_RUNNING_STATUSES = frozenset({"running", "idle"})

logger = logging.getLogger(__name__)


class _BoxNotFoundError(Exception):
    """Raised internally when a Box file or sandbox returns HTTP 404."""


class UpstashBoxSandboxClientOptions(BaseSandboxClientOptions):
    """Options for ``UpstashBoxSandboxClient``."""

    type: Literal["upstash_box"] = "upstash_box"
    api_key: str | None = None
    base_url: str | None = None
    name: str | None = None
    size: Literal["small", "medium", "large"] | None = None
    runtime: str | None = None
    keep_alive: bool = False
    network_policy: dict[str, Any] | None = None
    git_token: str | None = None
    snapshot_id: str | None = None
    pause_on_exit: bool = False
    env_vars: dict[str, str] | None = None
    exposed_ports: tuple[int, ...] = ()

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        name: str | None = None,
        size: Literal["small", "medium", "large"] | None = None,
        runtime: str | None = None,
        keep_alive: bool = False,
        network_policy: dict[str, Any] | None = None,
        git_token: str | None = None,
        snapshot_id: str | None = None,
        pause_on_exit: bool = False,
        env_vars: dict[str, str] | None = None,
        exposed_ports: tuple[int, ...] = (),
        *,
        type: Literal["upstash_box"] = "upstash_box",
    ) -> None:
        super().__init__(
            type=type,
            api_key=api_key,
            base_url=base_url,
            name=name,
            size=size,
            runtime=runtime,
            keep_alive=keep_alive,
            network_policy=network_policy,
            git_token=git_token,
            snapshot_id=snapshot_id,
            pause_on_exit=pause_on_exit,
            env_vars=env_vars,
            exposed_ports=exposed_ports,
        )


class UpstashBoxSandboxSessionState(SandboxSessionState):
    """Serializable state for an Upstash Box-backed session."""

    type: Literal["upstash_box"] = "upstash_box"
    box_id: str
    base_url: str
    base_env_vars: dict[str, str] = {}
    keep_alive: bool = False
    pause_on_exit: bool = False
    snapshot_id: str | None = None


class UpstashBoxSandboxSession(BaseSandboxSession):
    """``BaseSandboxSession`` backed by Upstash Box over HTTP."""

    state: UpstashBoxSandboxSessionState
    _api_key: str | None
    _http: aiohttp.ClientSession | None
    _exec_timeout_s: float | None
    _request_timeout_s: float | None

    def __init__(
        self,
        *,
        state: UpstashBoxSandboxSessionState,
        api_key: str | None = None,
        http: aiohttp.ClientSession | None = None,
        exec_timeout_s: float | None = None,
        request_timeout_s: float | None = None,
    ) -> None:
        self.state = state
        self._api_key = api_key
        self._http = http
        self._exec_timeout_s = exec_timeout_s
        self._request_timeout_s = request_timeout_s

    @classmethod
    def from_state(
        cls,
        state: UpstashBoxSandboxSessionState,
        *,
        api_key: str | None = None,
        http: aiohttp.ClientSession | None = None,
        exec_timeout_s: float | None = None,
        request_timeout_s: float | None = None,
    ) -> UpstashBoxSandboxSession:
        return cls(
            state=state,
            api_key=api_key,
            http=http,
            exec_timeout_s=exec_timeout_s,
            request_timeout_s=request_timeout_s,
        )

    @property
    def box_id(self) -> str:
        return self.state.box_id

    # ----- HTTP plumbing -------------------------------------------------

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            headers: dict[str, str] = {}
            if api_key := self._api_key or os.environ.get("UPSTASH_BOX_API_KEY"):
                headers["X-Box-Api-Key"] = api_key
            self._http = aiohttp.ClientSession(headers=headers)
        return self._http

    def _url(self, path: str) -> str:
        base = self.state.base_url.rstrip("/")
        return f"{base}/v2/box/{self.state.box_id}/{path.lstrip('/')}"

    def _request_timeout(self) -> aiohttp.ClientTimeout:
        total = (
            self._request_timeout_s
            if self._request_timeout_s is not None
            else _DEFAULT_REQUEST_TIMEOUT_S
        )
        return aiohttp.ClientTimeout(total=total)

    async def _close_http(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.box_id

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    # ----- exec ----------------------------------------------------------

    async def _resolved_envs(self) -> dict[str, str]:
        manifest_envs = await self.state.manifest.environment.resolve()
        return {**self.state.base_env_vars, **manifest_envs}

    async def _box_exec(
        self,
        argv: list[str],
        *,
        folder: str | None,
        timeout: float | None,
    ) -> ExecResult:
        payload: dict[str, Any] = {"command": argv}
        if folder is not None:
            payload["folder"] = folder

        http = self._session()
        url = self._url("exec")
        request_timeout = aiohttp.ClientTimeout(
            total=timeout + 5.0 if timeout is not None else None
        )
        try:
            async with http.post(url, json=payload, timeout=request_timeout) as resp:
                if resp.status != 200:
                    detail = await _read_error_body(resp)
                    message = _http_error_message("POST /exec", resp.status, detail)
                    raise ExecTransportError(
                        command=tuple(argv),
                        context=_error_context(status=resp.status, detail=detail),
                        cause=Exception(message),
                        message=message,
                    )
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError as e:
            raise ExecTimeoutError(command=tuple(argv), timeout_s=timeout, cause=e) from e
        except ExecTransportError:
            raise
        except aiohttp.ClientError as e:
            raise _transport_error(command=tuple(argv), cause=e, operation="exec") from e

        output = data.get("output") or ""
        error = data.get("error") or ""
        exit_code = int(data.get("exit_code", 0) or 0)
        return ExecResult(
            stdout=output.encode("utf-8") if isinstance(output, str) else bytes(output),
            stderr=error.encode("utf-8") if isinstance(error, str) else bytes(error),
            exit_code=exit_code,
        )

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        argv = [str(c) for c in command]
        envs = await self._resolved_envs()
        if envs:
            argv = ["env", "--", *[f"{key}={value}" for key, value in sorted(envs.items())], *argv]
        effective_timeout = (
            timeout
            if timeout is not None
            else (
                self._exec_timeout_s
                if self._exec_timeout_s is not None
                else _DEFAULT_EXEC_TIMEOUT_S
            )
        )
        folder = sandbox_path_str(self._workspace_root_path())
        return await self._box_exec(argv, folder=folder, timeout=effective_timeout)

    async def _prepare_backend_workspace(self) -> None:
        # Create the workspace root before exec calls use it as their cwd. Run with no folder so
        # this does not depend on the (not-yet-created) root directory existing.
        root = sandbox_path_str(self._workspace_root_path())
        try:
            result = await self._box_exec(
                ["mkdir", "-p", "--", root],
                folder=None,
                timeout=self._request_timeout_s or _DEFAULT_REQUEST_TIMEOUT_S,
            )
        except Exception as e:
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context={"backend": "upstash_box", "reason": "prepare_workspace_failed"},
                cause=e,
            ) from e
        if not result.ok():
            raise WorkspaceStartError(
                path=self._workspace_root_path(),
                context={
                    "backend": "upstash_box",
                    "reason": "prepare_workspace_nonzero_exit",
                    "exit_code": result.exit_code,
                    "output": result.stderr.decode("utf-8", errors="replace"),
                },
            )

    # ----- files ---------------------------------------------------------

    async def _box_read_file(self, path: str) -> bytes:
        http = self._session()
        params = {"path": path, "encoding": "base64"}
        url = self._url("files/read")
        async with http.get(url, params=params, timeout=self._request_timeout()) as resp:
            if resp.status == 404:
                raise _BoxNotFoundError(path)
            if resp.status != 200:
                detail = await _read_error_body(resp)
                raise WorkspaceArchiveReadError(
                    path=posix_path_as_path(coerce_posix_path(path)),
                    context={"reason": "http_error", "http_status": resp.status, "message": detail},
                )
            data = await resp.json(content_type=None)
        content = data.get("content", "")
        return base64.b64decode(content) if content else b""

    async def _box_write_file(self, path: str, payload: bytes) -> None:
        http = self._session()
        body = {
            "path": path,
            "content": base64.b64encode(payload).decode("ascii"),
            "encoding": "base64",
        }
        url = self._url("files/write")
        async with http.post(url, json=body, timeout=self._request_timeout()) as resp:
            if resp.status != 200:
                detail = await _read_error_body(resp)
                raise WorkspaceArchiveWriteError(
                    path=posix_path_as_path(coerce_posix_path(path)),
                    context={"reason": "http_error", "http_status": resp.status, "message": detail},
                )

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            workspace_path = await self._check_read_with_exec(path, user=user)
        else:
            workspace_path = await self._validate_path_access(path)

        try:
            data = await self._box_read_file(sandbox_path_str(workspace_path))
        except _BoxNotFoundError as e:
            raise WorkspaceReadNotFoundError(path=error_path, cause=e) from e
        except WorkspaceArchiveReadError:
            raise
        except aiohttp.ClientError as e:
            raise WorkspaceArchiveReadError(path=error_path, cause=e) from e
        return io.BytesIO(data)

    async def write(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            await self._check_write_with_exec(path, user=user)

        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=error_path, actual_type=type(payload).__name__)

        workspace_path = await self._validate_path_access(path, for_write=True)
        try:
            await self._box_write_file(sandbox_path_str(workspace_path), bytes(payload))
        except WorkspaceArchiveWriteError:
            raise
        except aiohttp.ClientError as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        http = self._session()
        url = self._url("status")
        try:
            async with http.get(url, timeout=self._request_timeout()) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
        except Exception:
            return False
        return str(data.get("status", "")) in _RUNNING_STATUSES

    # ----- exposed ports -------------------------------------------------

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        http = self._session()
        url = self._url("preview")
        try:
            async with http.post(url, json={"port": port}, timeout=self._request_timeout()) as resp:
                if resp.status != 200:
                    detail = await _read_error_body(resp)
                    raise ExposedPortUnavailableError(
                        port=port,
                        exposed_ports=self.state.exposed_ports,
                        reason="backend_unavailable",
                        context={
                            "backend": "upstash_box",
                            "http_status": resp.status,
                            "detail": detail,
                        },
                    )
                data = await resp.json(content_type=None)
        except ExposedPortUnavailableError:
            raise
        except Exception as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "upstash_box", "detail": "preview_request_failed"},
                cause=e,
            ) from e

        url_value = data.get("url")
        if not isinstance(url_value, str) or not url_value:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={
                    "backend": "upstash_box",
                    "detail": "invalid_preview_url",
                    "url": url_value,
                },
            )
        split = urlsplit(url_value)
        host = split.hostname
        if host is None:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={
                    "backend": "upstash_box",
                    "detail": "invalid_preview_url",
                    "url": url_value,
                },
            )
        resolved_port = split.port or (443 if split.scheme == "https" else 80)
        return ExposedPortEndpoint(
            host=host,
            port=resolved_port,
            tls=split.scheme == "https",
            query=split.query,
        )

    # ----- lifecycle -----------------------------------------------------

    async def _shutdown_backend(self) -> None:
        # Keep-alive boxes are caller-managed and stay running.
        if self.state.keep_alive:
            return
        http = self._session()
        action = "pause" if self.state.pause_on_exit else "delete"
        # Cleanup is best-effort (raising here would mask the original flow), but failures are
        # logged so a box that fails to pause/delete is not silently orphaned.
        try:
            if self.state.pause_on_exit:
                async with http.post(self._url("pause"), timeout=self._request_timeout()) as resp:
                    await self._log_lifecycle_failure(action, resp)
                return
            base = self.state.base_url.rstrip("/")
            async with http.delete(
                f"{base}/v2/box/{self.state.box_id}", timeout=self._request_timeout()
            ) as resp:
                await self._log_lifecycle_failure(action, resp)
        except Exception:
            logger.warning(
                "Failed to %s Upstash Box %s on shutdown", action, self.state.box_id, exc_info=True
            )

    async def _log_lifecycle_failure(self, action: str, resp: aiohttp.ClientResponse) -> None:
        # 404 on delete is benign (already gone); anything else >=400 is surfaced as a warning.
        if resp.status < 400 or (action == "delete" and resp.status == 404):
            return
        detail = await _read_error_body(resp)
        logger.warning(
            "Failed to %s Upstash Box %s: HTTP %s%s",
            action,
            self.state.box_id,
            resp.status,
            f": {detail}" if detail else "",
        )

    async def _after_stop(self) -> None:
        await self._close_http()

    async def _after_shutdown(self) -> None:
        await self._close_http()

    # ----- workspace persistence (tar via exec + Box files API) ----------

    def _tar_exclude_args(self) -> list[str]:
        return shell_tar_exclude_args(self._persist_workspace_skip_relpaths())

    async def persist_workspace(self) -> io.IOBase:
        root = self._workspace_root_path()
        tar_path = f"/tmp/sandbox-persist-{self.state.session_id.hex}.tar"
        excludes = self._tar_exclude_args()
        tar_cmd = ["tar", *excludes, "-C", root.as_posix(), "-cf", tar_path, "."]
        try:
            result = await self._exec_internal(*tar_cmd, timeout=_WORKSPACE_TAR_TIMEOUT_S)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "reason": "tar_failed",
                        "output": result.stderr.decode("utf-8", errors="replace"),
                    },
                    retryable=False,
                )
            raw = await self._box_read_file(tar_path)
        except WorkspaceArchiveReadError:
            raise
        except Exception as e:
            raise WorkspaceArchiveReadError(path=root, cause=e) from e
        finally:
            try:
                await self._exec_internal("rm", "-f", "--", tar_path)
            except Exception:
                pass
        return io.BytesIO(raw)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        tar_path = f"/tmp/sandbox-hydrate-{self.state.session_id.hex}.tar"
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=Path(tar_path), actual_type=type(payload).__name__)

        try:
            validate_tar_bytes(bytes(payload), allow_external_symlink_targets=False)
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={"reason": "unsafe_or_invalid_tar", "member": e.member, "detail": str(e)},
                cause=e,
            ) from e

        try:
            await self._exec_internal("mkdir", "-p", "--", root.as_posix())
            await self._box_write_file(tar_path, bytes(payload))
            result = await self._exec_internal(
                "tar", "-C", root.as_posix(), "-xf", tar_path, timeout=_WORKSPACE_TAR_TIMEOUT_S
            )
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "reason": "tar_extract_failed",
                        "output": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e
        finally:
            try:
                await self._exec_internal("rm", "-f", "--", tar_path)
            except Exception:
                pass


class UpstashBoxSandboxClient(BaseSandboxClient[UpstashBoxSandboxClientOptions]):
    """Upstash Box sandbox client managing box lifecycle over the Box REST API."""

    backend_id = "upstash_box"
    _instrumentation: Instrumentation
    _exec_timeout_s: float
    _request_timeout_s: float

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
        exec_timeout_s: float = _DEFAULT_EXEC_TIMEOUT_S,
        request_timeout_s: float = _DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies
        self._exec_timeout_s = exec_timeout_s
        self._request_timeout_s = request_timeout_s

    def _resolve_api_key(self, options: UpstashBoxSandboxClientOptions) -> str:
        api_key = options.api_key or self._api_key or os.environ.get("UPSTASH_BOX_API_KEY")
        if not api_key:
            raise ConfigurationError(
                message=(
                    "Upstash Box requires an API key. Pass api_key to "
                    "UpstashBoxSandboxClient/options or set UPSTASH_BOX_API_KEY."
                ),
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id},
            )
        return api_key

    def _resolve_base_url(self, options: UpstashBoxSandboxClientOptions) -> str:
        base_url = (
            options.base_url
            or self._base_url
            or os.environ.get("UPSTASH_BOX_BASE_URL")
            or _DEFAULT_BASE_URL
        )
        return base_url.rstrip("/")

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: UpstashBoxSandboxClientOptions,
    ) -> SandboxSession:
        if manifest is None:
            manifest = Manifest(root=_DEFAULT_WORKSPACE_ROOT)
        elif manifest.root == "/workspace":
            # Remap the core default root onto Box's workspace home.
            manifest = manifest.model_copy(update={"root": _DEFAULT_WORKSPACE_ROOT})

        api_key = self._resolve_api_key(options)
        base_url = self._resolve_base_url(options)
        box_id = await self._create_box(api_key=api_key, base_url=base_url, options=options)

        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = UpstashBoxSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            box_id=box_id,
            base_url=base_url,
            base_env_vars=dict(options.env_vars or {}),
            keep_alive=options.keep_alive,
            pause_on_exit=options.pause_on_exit,
            snapshot_id=options.snapshot_id,
            exposed_ports=options.exposed_ports,
        )
        inner = UpstashBoxSandboxSession.from_state(
            state,
            api_key=api_key,
            exec_timeout_s=self._exec_timeout_s,
            request_timeout_s=self._request_timeout_s,
        )
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, UpstashBoxSandboxSession):
            raise TypeError("UpstashBoxSandboxClient.delete expects an UpstashBoxSandboxSession")
        # Delegate to shutdown() so the configured lifecycle is honored. The runner cleanup path
        # calls session.shutdown() and then client.delete(); force-deleting here would defeat
        # pause_on_exit / keep_alive. shutdown() already handles the configured lifecycle.
        try:
            await inner.shutdown()
        except Exception:
            logger.warning(
                "Failed to shut down Upstash Box %s during delete",
                inner.state.box_id,
                exc_info=True,
            )
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, UpstashBoxSandboxSessionState):
            raise TypeError(
                "UpstashBoxSandboxClient.resume expects an UpstashBoxSandboxSessionState"
            )
        # The API key is never persisted in serialized state; resolve it from the client/env.
        api_key = self._api_key or os.environ.get("UPSTASH_BOX_API_KEY")
        inner = UpstashBoxSandboxSession.from_state(
            state,
            api_key=api_key,
            exec_timeout_s=self._exec_timeout_s,
            request_timeout_s=self._request_timeout_s,
        )
        # Reattach to the existing box. This raises on a missing box or a transient failure
        # rather than silently degrading: returning "not preserved" here would make start()
        # re-apply the manifest and clobber a healthy live workspace.
        await self._reconnect(inner, state)
        # The backend is reachable; let the base session's readiness probe decide whether the
        # workspace root still exists before reusing it.
        inner._set_start_state_preserved(True, system=True)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def _reconnect(
        self, inner: UpstashBoxSandboxSession, state: UpstashBoxSandboxSessionState
    ) -> None:
        http = inner._session()
        base = state.base_url.rstrip("/")
        try:
            async with http.get(
                f"{base}/v2/box/{state.box_id}", timeout=inner._request_timeout()
            ) as resp:
                if resp.status == 404:
                    raise ConfigurationError(
                        message=(
                            f"Upstash Box {state.box_id} no longer exists and cannot be resumed. "
                            "Create a new session instead."
                        ),
                        error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                        op="start",
                        context={"backend": self.backend_id, "box_id": state.box_id},
                    )
                if resp.status != 200:
                    detail = await _read_error_body(resp)
                    raise ConfigurationError(
                        message=_http_error_message("GET /v2/box", resp.status, detail),
                        error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                        op="start",
                        context=_error_context(status=resp.status, detail=detail),
                    )
                data = await resp.json(content_type=None)
        except ConfigurationError:
            raise
        except aiohttp.ClientError as e:
            raise ConfigurationError(
                message=f"Failed to reach Upstash Box {state.box_id} during resume: {e}",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id, "cause_type": type(e).__name__},
            ) from e

        status = str(data.get("status", ""))
        if status == "paused":
            async with http.post(
                f"{base}/v2/box/{state.box_id}/resume", timeout=inner._request_timeout()
            ) as resp:
                if resp.status >= 400:
                    detail = await _read_error_body(resp)
                    raise ConfigurationError(
                        message=_http_error_message("POST /resume", resp.status, detail),
                        error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                        op="start",
                        context=_error_context(status=resp.status, detail=detail),
                    )
            await self._wait_until_runnable(
                http, base, state.box_id, timeout=inner._request_timeout()
            )
            return

        if status in _RUNNING_STATUSES:
            return

        if status == "creating":
            await self._wait_until_runnable(
                http, base, state.box_id, timeout=inner._request_timeout()
            )
            return

        raise ConfigurationError(
            message=f"Upstash Box {state.box_id} is not runnable and cannot be resumed: {status}",
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op="start",
            context={"backend": self.backend_id, "box_id": state.box_id, "status": status},
        )

    async def _wait_until_runnable(
        self,
        http: aiohttp.ClientSession,
        base_url: str,
        box_id: str,
        *,
        timeout: aiohttp.ClientTimeout,
    ) -> None:
        deadline = asyncio.get_event_loop().time() + _CREATE_MAX_WAIT_S
        while True:
            async with http.get(f"{base_url}/v2/box/{box_id}", timeout=timeout) as resp:
                if resp.status != 200:
                    detail = await _read_error_body(resp)
                    raise ConfigurationError(
                        message=_http_error_message("GET /v2/box", resp.status, detail),
                        error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                        op="start",
                        context=_error_context(status=resp.status, detail=detail),
                    )
                data = await resp.json(content_type=None)

            status = str(data.get("status", ""))
            if status in _RUNNING_STATUSES:
                return
            if status == "error":
                raise ConfigurationError(
                    message=f"Upstash Box {box_id} failed while waiting for resume",
                    error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                    op="start",
                    context={"backend": self.backend_id, "box_id": box_id, "status": status},
                )
            if status != "creating":
                raise ConfigurationError(
                    message=f"Upstash Box {box_id} is not runnable and cannot be resumed: {status}",
                    error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                    op="start",
                    context={"backend": self.backend_id, "box_id": box_id, "status": status},
                )
            if asyncio.get_event_loop().time() >= deadline:
                raise ConfigurationError(
                    message=f"Upstash Box {box_id} did not become runnable after resume",
                    error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                    op="start",
                    context={"backend": self.backend_id, "box_id": box_id, "status": status},
                )
            await asyncio.sleep(_CREATE_POLL_INTERVAL_S)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return UpstashBoxSandboxSessionState.model_validate(payload)

    async def _create_box(
        self,
        *,
        api_key: str,
        base_url: str,
        options: UpstashBoxSandboxClientOptions,
    ) -> str:
        body: dict[str, Any] = {}
        if options.name:
            body["name"] = options.name
        if options.size:
            body["size"] = options.size
        if options.runtime:
            body["runtime"] = options.runtime
        if options.keep_alive:
            body["keep_alive"] = True
        if options.network_policy:
            body["network_policy"] = options.network_policy
        if options.git_token:
            body["github_token"] = options.git_token
        if options.env_vars:
            body["env_vars"] = options.env_vars

        headers = {"X-Box-Api-Key": api_key, "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=self._request_timeout_s)
        if options.snapshot_id:
            create_url = f"{base_url}/v2/box/from-snapshot"
            body["snapshot_id"] = options.snapshot_id
        else:
            create_url = f"{base_url}/v2/box"

        try:
            async with aiohttp.ClientSession(headers=headers) as http:
                data = await self._post_box_create(
                    http,
                    create_url,
                    body,
                    timeout=timeout,
                    is_snapshot=bool(options.snapshot_id),
                )
                box_id = data.get("id")
                if not isinstance(box_id, str) or not box_id:
                    raise ConfigurationError(
                        message="Box creation returned an invalid id",
                        error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                        op="start",
                        context={"backend": self.backend_id},
                    )
                data = await self._poll_until_ready(http, base_url, box_id, data)
                return box_id
        except ConfigurationError:
            raise
        except aiohttp.ClientError as e:
            raise ConfigurationError(
                message=f"Box creation request failed: {e}",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id, "cause_type": type(e).__name__},
            ) from e

    async def _post_box_create(
        self,
        http: aiohttp.ClientSession,
        url: str,
        body: dict[str, Any],
        *,
        timeout: aiohttp.ClientTimeout,
        is_snapshot: bool,
    ) -> dict[str, Any]:
        # Freshly created snapshots may still be propagating; the Box API answers 400 "not ready"
        # for a short window, so retry from-snapshot creation like the official SDK does.
        deadline = asyncio.get_event_loop().time() + _SNAPSHOT_NOT_READY_MAX_WAIT_S
        while True:
            async with http.post(url, json=body, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    data = await resp.json(content_type=None)
                    assert isinstance(data, dict)
                    return data
                detail = await _read_error_body(resp)
                if (
                    is_snapshot
                    and resp.status == 400
                    and detail is not None
                    and "not ready" in detail.lower()
                    and asyncio.get_event_loop().time() < deadline
                ):
                    await asyncio.sleep(_SNAPSHOT_POLL_INTERVAL_S)
                    continue
                raise ConfigurationError(
                    message=_http_error_message("POST /v2/box", resp.status, detail),
                    error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                    op="start",
                    context=_error_context(status=resp.status, detail=detail),
                )

    async def _poll_until_ready(
        self,
        http: aiohttp.ClientSession,
        base_url: str,
        box_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + _CREATE_MAX_WAIT_S
        while str(data.get("status", "")) == "creating":
            if asyncio.get_event_loop().time() >= deadline:
                raise ConfigurationError(
                    message="Box creation timed out",
                    error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                    op="start",
                    context={"backend": self.backend_id, "box_id": box_id},
                )
            await asyncio.sleep(_CREATE_POLL_INTERVAL_S)
            async with http.get(
                f"{base_url}/v2/box/{box_id}",
                timeout=aiohttp.ClientTimeout(total=self._request_timeout_s),
            ) as poll:
                if poll.status == 200:
                    data = await poll.json(content_type=None)
        if str(data.get("status", "")) == "error":
            raise ConfigurationError(
                message="Box creation failed",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": self.backend_id, "box_id": box_id},
            )
        return data


def _http_error_message(operation: str, status: int, detail: str | None) -> str:
    message = f"{operation} failed: HTTP {status}"
    if detail:
        message += f": {detail}"
    return message


def _error_context(*, status: int | None = None, detail: str | None = None) -> dict[str, object]:
    context: dict[str, object] = {"backend": "upstash_box"}
    if status is not None:
        context["http_status"] = status
    if detail:
        context["provider_error"] = detail
    return context


def _transport_error(
    *, command: tuple[str, ...], cause: BaseException, operation: str
) -> ExecTransportError:
    detail = str(cause)
    provider_error = f"{type(cause).__name__}: {detail}" if detail else type(cause).__name__
    return ExecTransportError(
        command=command,
        context={
            "backend": "upstash_box",
            "operation": operation,
            "provider_error": provider_error,
        },
        cause=cause,
        message=f"Upstash Box {operation} transport failed: {provider_error}",
    )


async def _read_error_body(resp: aiohttp.ClientResponse) -> str | None:
    try:
        raw = await resp.read()
    except Exception as e:
        return f"failed to read error body: {e}"
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    return text[:2000]


__all__ = [
    "UpstashBoxSandboxClient",
    "UpstashBoxSandboxClientOptions",
    "UpstashBoxSandboxSession",
    "UpstashBoxSandboxSessionState",
]
