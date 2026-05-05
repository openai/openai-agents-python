"""
Islo sandbox (https://islo.dev) implementation.

This module provides an Islo-backed sandbox client/session implementation backed by
``islo.AsyncIslo`` (the Python SDK at https://github.com/islo-labs/python-sdk).

The ``islo`` dependency is optional, so package-level exports should guard imports of this
module. Within this module, Islo SDK imports are normal so users with the extra installed get
full type navigation.

Differentiator vs. other hosted backends: ``IsloSandboxClientOptions.gateway_profile`` accepts
either a string (the name or id of a pre-existing islo gateway profile) or an inline
``IsloGatewayProfile`` definition with rule-level egress policy. When inline, the profile is
provisioned before sandbox creation and torn down on ``client.delete``. No other backend in this
package exposes per-rule allow/deny/rate-limit egress today.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx

# Islo SDK imports — required at module load (the islo extra installs them).
from islo import AsyncIslo  # noqa: E402
from islo.errors.conflict_error import ConflictError as IsloConflictError
from pydantic import BaseModel, ConfigDict

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceWriteTypeError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session import SandboxSession, SandboxSessionState
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.dependencies import Dependencies
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.retry import (
    exception_chain_contains_type,
    exception_chain_has_status_code,
    retry_async,
)
from ....sandbox.workspace_paths import sandbox_path_str

DEFAULT_ISLO_WORKDIR = "/workspace"
DEFAULT_ISLO_EXEC_POLL_INTERVAL_S = 0.5
DEFAULT_ISLO_EXEC_DEFAULT_TIMEOUT_S = 600.0
DEFAULT_ISLO_WAIT_FOR_RUNNING_TIMEOUT_S = 300.0
DEFAULT_ISLO_WAIT_POLL_INTERVAL_S = 1.0
DEFAULT_ISLO_HTTP_TIMEOUT_S = 180.0
_ISLO_SNAPSHOT_MAGIC = b"ISLO_SANDBOX_SNAPSHOT_V1\n"

_ISLO_TRANSIENT_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ReadError,
    httpx.NetworkError,
    httpx.ProtocolError,
    httpx.TimeoutException,
)

_ISLO_TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def _is_transient_islo_error(exc: BaseException) -> bool:
    if exception_chain_has_status_code(exc, _ISLO_TRANSIENT_STATUS_CODES):
        return True
    return exception_chain_contains_type(exc, _ISLO_TRANSIENT_TRANSPORT_ERRORS)


@retry_async(retry_if=lambda exc, *_args, **_kwargs: _is_transient_islo_error(exc))
async def _create_sandbox_with_retry(
    sdk: AsyncIslo,
    **kwargs: Any,
) -> Any:
    # Disable the SDK's own internal retries: connection-level retries on a non-idempotent
    # create can produce SANDBOX_ALREADY_EXISTS even when the first attempt succeeded
    # server-side. Our outer @retry_async only triggers on transient status codes / network
    # errors that are safe to repeat.
    return await sdk.sandboxes.create_sandbox(
        **kwargs,
        request_options={"max_retries": 0},
    )


def _encode_snapshot_ref(*, sandbox_name: str) -> bytes:
    body = json.dumps(
        {"sandbox_name": sandbox_name},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _ISLO_SNAPSHOT_MAGIC + body


def _decode_snapshot_ref(raw: bytes) -> str | None:
    if not raw.startswith(_ISLO_SNAPSHOT_MAGIC):
        return None
    body = raw[len(_ISLO_SNAPSHOT_MAGIC) :]
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    name = payload.get("sandbox_name")
    return name if isinstance(name, str) and name else None


# ---------------------------------------------------------------------------
# Gateway profile types (the differentiating feature)
# ---------------------------------------------------------------------------


class IsloGatewayRule(BaseModel):
    """A single egress rule in an inline ``IsloGatewayProfile``.

    Fields map 1:1 to ``islo.GatewayRuleResponse``. ``host_pattern`` accepts globs (e.g.
    ``*.github.com``). ``priority`` lower-is-evaluated-first; if omitted, priorities are assigned
    in declaration order. ``rate_limit_rpm`` is requests-per-minute per sandbox.
    """

    model_config = ConfigDict(frozen=True)

    host_pattern: str
    action: Literal["allow", "deny"] = "allow"
    priority: int | None = None
    path_pattern: str | None = None
    methods: tuple[str, ...] | None = None
    rate_limit_rpm: int | None = None
    provider_key: str | None = None


class IsloGatewayProfile(BaseModel):
    """Inline definition of an islo gateway profile.

    When a sandbox is created with ``IsloSandboxClientOptions.gateway_profile`` set to an
    ``IsloGatewayProfile`` instance, Crabbox provisions the profile (and its rules) before
    creating the sandbox, binds it, and tears it down on ``IsloSandboxClient.delete``.

    To reuse an existing profile instead, pass its name or id as a string.

    ``default_action`` of ``deny`` plus ``internet_enabled=False`` is the recommended zero-trust
    posture for agents; rules then explicitly allow the hosts the agent should be able to reach.
    """

    model_config = ConfigDict(frozen=True)

    name: str | None = None
    description: str | None = None
    default_action: Literal["allow", "deny"] = "deny"
    internet_enabled: bool = False
    rules: tuple[IsloGatewayRule, ...] = ()


# ---------------------------------------------------------------------------
# Options + state
# ---------------------------------------------------------------------------


class IsloSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Islo sandbox backend.

    ``gateway_profile`` may be a string (existing profile name or id) or an
    ``IsloGatewayProfile`` describing a profile to create inline. Inline profiles are deleted
    when the owning session is deleted.
    """

    type: Literal["islo"] = "islo"
    image: str | None = None
    workdir: str | None = None
    env: dict[str, str] | None = None
    vcpus: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    init_capabilities: tuple[str, ...] | None = None
    gateway_profile: str | IsloGatewayProfile | None = None
    snapshot_name: str | None = None
    exec_poll_interval_s: float = DEFAULT_ISLO_EXEC_POLL_INTERVAL_S
    exec_default_timeout_s: float = DEFAULT_ISLO_EXEC_DEFAULT_TIMEOUT_S
    wait_for_running_timeout_s: float = DEFAULT_ISLO_WAIT_FOR_RUNNING_TIMEOUT_S

    def __init__(
        self,
        image: str | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
        init_capabilities: tuple[str, ...] | None = None,
        gateway_profile: str | IsloGatewayProfile | None = None,
        snapshot_name: str | None = None,
        exec_poll_interval_s: float = DEFAULT_ISLO_EXEC_POLL_INTERVAL_S,
        exec_default_timeout_s: float = DEFAULT_ISLO_EXEC_DEFAULT_TIMEOUT_S,
        wait_for_running_timeout_s: float = DEFAULT_ISLO_WAIT_FOR_RUNNING_TIMEOUT_S,
        *,
        type: Literal["islo"] = "islo",
    ) -> None:
        super().__init__(
            type=type,
            image=image,
            workdir=workdir,
            env=env,
            vcpus=vcpus,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
            init_capabilities=init_capabilities,
            gateway_profile=gateway_profile,
            snapshot_name=snapshot_name,
            exec_poll_interval_s=exec_poll_interval_s,
            exec_default_timeout_s=exec_default_timeout_s,
            wait_for_running_timeout_s=wait_for_running_timeout_s,
        )


class IsloSandboxSessionState(SandboxSessionState):
    """Serializable state for an islo-backed session.

    ``sandbox_name`` is the canonical reference; islo identifies sandboxes by name. The
    ``gateway_profile_id`` and ``gateway_profile_inline`` fields track inline-created profiles
    for cleanup; pre-existing profiles bound by name are not touched on delete.
    """

    type: Literal["islo"] = "islo"
    sandbox_name: str
    image: str | None = None
    workdir: str | None = None
    env: dict[str, str] | None = None
    gateway_profile_id: str | None = None
    gateway_profile_inline: bool = False
    exec_poll_interval_s: float = DEFAULT_ISLO_EXEC_POLL_INTERVAL_S
    exec_default_timeout_s: float = DEFAULT_ISLO_EXEC_DEFAULT_TIMEOUT_S
    wait_for_running_timeout_s: float = DEFAULT_ISLO_WAIT_FOR_RUNNING_TIMEOUT_S


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class IsloSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by an islo sandbox."""

    state: IsloSandboxSessionState
    _sdk: AsyncIslo
    _attached: bool

    def __init__(
        self,
        *,
        state: IsloSandboxSessionState,
        sdk: AsyncIslo,
        attached: bool = False,
    ) -> None:
        self.state = state
        self._sdk = sdk
        self._attached = attached

    @classmethod
    def from_state(
        cls,
        state: IsloSandboxSessionState,
        *,
        sdk: AsyncIslo,
        attached: bool = False,
    ) -> IsloSandboxSession:
        return cls(state=state, sdk=sdk, attached=attached)

    def supports_pty(self) -> bool:
        return False

    def _reject_user_arg(
        self, *, op: Literal["exec", "read", "write"], user: str | User
    ) -> None:
        user_name = user.name if isinstance(user, User) else user
        raise ConfigurationError(
            message=(
                "IsloSandboxSession does not support sandbox-local users; "
                f"`{op}` must be called without `user`. Pass the user via "
                "IsloSandboxClientOptions.env or the islo image's default user."
            ),
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op=op,
            context={"backend": "islo", "user": user_name},
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

    async def _wait_for_running(self) -> None:
        deadline = time.monotonic() + self.state.wait_for_running_timeout_s
        while True:
            try:
                sb = await self._sdk.sandboxes.get_sandbox(self.state.sandbox_name)
                status = (getattr(sb, "status", "") or "").lower()
                if status == "running":
                    return
                if status in {"failed", "deleted", "stopped", "error"}:
                    raise ExecTransportError(
                        command=("wait_for_running",),
                        context={
                            "backend": "islo",
                            "sandbox_name": self.state.sandbox_name,
                            "status": status,
                        },
                    )
            except ExecTransportError:
                raise
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise ExecTransportError(
                    command=("wait_for_running",),
                    context={
                        "backend": "islo",
                        "sandbox_name": self.state.sandbox_name,
                        "reason": "wait_for_running_timeout",
                        "timeout_s": self.state.wait_for_running_timeout_s,
                    },
                )
            await asyncio.sleep(DEFAULT_ISLO_WAIT_POLL_INTERVAL_S)

    async def running(self) -> bool:
        try:
            sb = await self._sdk.sandboxes.get_sandbox(self.state.sandbox_name)
        except Exception:
            return False
        status = (getattr(sb, "status", "") or "").lower()
        return status == "running"

    async def shutdown(self) -> None:
        try:
            await self._sdk.sandboxes.delete_sandbox(self.state.sandbox_name)
        except Exception:
            return

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        normalized = [str(part) for part in command]
        if not normalized:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        effective_timeout = (
            timeout
            if timeout is not None
            else self.state.exec_default_timeout_s
        )
        deadline = time.monotonic() + effective_timeout if effective_timeout else None
        workdir = self.state.workdir or self.state.manifest.root or DEFAULT_ISLO_WORKDIR

        try:
            exec_resp = await self._sdk.sandboxes.exec_in_sandbox(
                self.state.sandbox_name,
                command=normalized,
                workdir=workdir,
            )
        except Exception as exc:
            raise ExecTransportError(
                command=normalized,
                context={"backend": "islo", "sandbox_name": self.state.sandbox_name},
                cause=exc,
            ) from exc

        exec_id = getattr(exec_resp, "exec_id", None) or getattr(exec_resp, "execId", None)
        if not exec_id:
            raise ExecTransportError(
                command=normalized,
                context={
                    "backend": "islo",
                    "sandbox_name": self.state.sandbox_name,
                    "reason": "missing_exec_id",
                },
            )

        poll_interval = max(self.state.exec_poll_interval_s, 0.1)
        while True:
            try:
                result = await self._sdk.sandboxes.get_exec_result(
                    self.state.sandbox_name,
                    exec_id,
                )
            except Exception as exc:
                raise ExecTransportError(
                    command=normalized,
                    context={
                        "backend": "islo",
                        "sandbox_name": self.state.sandbox_name,
                        "exec_id": exec_id,
                    },
                    cause=exc,
                ) from exc

            status = (getattr(result, "status", "") or "").lower()
            if status in {"finished", "completed", "succeeded", "failed", "exited", "done"}:
                stdout_text = getattr(result, "stdout", None) or ""
                stderr_text = getattr(result, "stderr", None) or ""
                exit_code = getattr(result, "exit_code", None)
                if exit_code is None:
                    exit_code = 0 if status not in {"failed"} else 1
                stdout_bytes = (
                    stdout_text.encode("utf-8")
                    if isinstance(stdout_text, str)
                    else bytes(stdout_text)
                )
                stderr_bytes = (
                    stderr_text.encode("utf-8")
                    if isinstance(stderr_text, str)
                    else bytes(stderr_text)
                )
                return ExecResult(
                    stdout=stdout_bytes,
                    stderr=stderr_bytes,
                    exit_code=int(exit_code),
                )

            if deadline is not None and time.monotonic() >= deadline:
                raise ExecTimeoutError(
                    command=normalized,
                    timeout_s=effective_timeout,
                    context={
                        "backend": "islo",
                        "sandbox_name": self.state.sandbox_name,
                        "exec_id": exec_id,
                        "last_status": status,
                    },
                )

            await asyncio.sleep(poll_interval)

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        raise ExposedPortUnavailableError(
            port=port,
            exposed_ports=self.state.exposed_ports,
            reason="backend_unavailable",
            context={
                "backend": "islo",
                "sandbox_name": self.state.sandbox_name,
                "hint": (
                    "islo gateway profiles control egress; ingress port forwarding is not "
                    "yet exposed via the Python SDK"
                ),
            },
        )

    async def _islo_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue an authenticated request through the SDK's underlying httpx client.

        The auto-generated SDK doesn't expose a body parameter on file IO endpoints
        (``POST /sandboxes/{name}/files`` etc), so we hit them directly while reusing
        the SDK's auth header injection and base URL.
        """
        wrapper = self._sdk._client_wrapper
        headers = await wrapper.async_get_headers()
        base_url = wrapper.get_base_url()
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        # Strip any Content-Type from auth headers; httpx sets it for us based on body.
        headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
        client = wrapper.httpx_client.httpx_client
        return await client.request(
            method,
            url,
            headers=headers,
            params=params,
            content=content,
            files=files,
        )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            self._reject_user_arg(op="read", user=user)

        normalized_path = await self._validate_path_access(path)
        path_str = sandbox_path_str(normalized_path)

        try:
            response = await self._islo_request(
                "GET",
                f"sandboxes/{self.state.sandbox_name}/files",
                params={"path": path_str},
            )
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=normalized_path, cause=exc) from exc

        if response.status_code == 404:
            raise WorkspaceReadNotFoundError(path=normalized_path)
        if response.status_code >= 400:
            raise WorkspaceArchiveReadError(
                path=normalized_path,
                context={
                    "status_code": response.status_code,
                    "body": response.text[:500],
                },
            )
        return io.BytesIO(response.content)

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
        path_str = sandbox_path_str(normalized_path)
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=normalized_path,
                actual_type=type(payload).__name__,
            )

        try:
            response = await self._islo_request(
                "POST",
                f"sandboxes/{self.state.sandbox_name}/files",
                params={"path": path_str},
                files={"file": (Path(path_str).name, bytes(payload), "application/octet-stream")},
            )
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=exc) from exc

        if response.status_code >= 400:
            raise WorkspaceArchiveWriteError(
                path=normalized_path,
                context={
                    "status_code": response.status_code,
                    "body": response.text[:500],
                },
            )

    async def persist_workspace(self) -> io.IOBase:
        root = self._workspace_root_path()
        try:
            response = await self._islo_request(
                "GET",
                f"sandboxes/{self.state.sandbox_name}/archive",
                params={"path": str(root)},
            )
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=root, cause=exc) from exc

        if response.status_code >= 400:
            raise WorkspaceArchiveReadError(
                path=root,
                context={
                    "status_code": response.status_code,
                    "body": response.text[:500],
                },
            )
        return io.BytesIO(response.content)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=root,
                actual_type=type(raw).__name__,
            )

        try:
            response = await self._islo_request(
                "POST",
                f"sandboxes/{self.state.sandbox_name}/archive",
                params={"path": str(root)},
                files={"file": ("workspace.tar.gz", bytes(raw), "application/gzip")},
            )
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=root, cause=exc) from exc

        if response.status_code >= 400:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "status_code": response.status_code,
                    "body": response.text[:500],
                },
            )



# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class IsloSandboxClient(BaseSandboxClient[IsloSandboxClientOptions]):
    """Islo-backed sandbox client.

    Auth is read from the ``ISLO_API_KEY`` environment variable (or the ``api_key`` constructor
    argument). The client also reads ``ISLO_BASE_URL`` (defaults to ``https://api.islo.dev``).
    """

    backend_id = "islo"
    _instrumentation: Instrumentation
    _sdk: AsyncIslo

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float | None = DEFAULT_ISLO_HTTP_TIMEOUT_S,
        sdk: AsyncIslo | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        if sdk is not None:
            self._sdk = sdk
        else:
            self._sdk = AsyncIslo(api_key=api_key, base_url=base_url, timeout=timeout_s)
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: IsloSandboxClientOptions,
    ) -> SandboxSession:
        resolved_manifest = manifest or Manifest()
        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))

        (
            gateway_profile_ref,
            gateway_profile_id,
            gateway_inline,
        ) = await self._resolve_gateway_profile(
            options.gateway_profile,
            session_id=session_id,
        )

        sandbox_name = f"agents-{session_id.hex[:12]}"

        # The islo SDK uses a sentinel for "omitted" rather than None; passing None
        # serializes as JSON null and the API rejects unknown null fields. Build
        # kwargs excluding anything we don't actually want to send.
        create_kwargs: dict[str, Any] = {"name": sandbox_name}
        if options.image is not None:
            create_kwargs["image"] = options.image
        if options.workdir is not None:
            create_kwargs["workdir"] = options.workdir
        if options.env:
            create_kwargs["env"] = dict(options.env)
        if options.vcpus is not None:
            create_kwargs["vcpus"] = options.vcpus
        if options.memory_mb is not None:
            create_kwargs["memory_mb"] = options.memory_mb
        if options.disk_gb is not None:
            create_kwargs["disk_gb"] = options.disk_gb
        if options.init_capabilities is not None:
            create_kwargs["init_capabilities"] = list(options.init_capabilities)
        if gateway_profile_ref is not None:
            create_kwargs["gateway_profile"] = gateway_profile_ref
        if options.snapshot_name is not None:
            create_kwargs["snapshot_name"] = options.snapshot_name

        try:
            sb = await _create_sandbox_with_retry(self._sdk, **create_kwargs)
        except IsloConflictError as conflict:
            # Long create_sandbox calls can race with internal retries: the first attempt
            # creates the sandbox server-side, a transient transport blip prompts a retry,
            # the retry sees SANDBOX_ALREADY_EXISTS. We win the race when the name we
            # asked for is the one that already exists. Look it up and treat as success.
            body = getattr(conflict, "body", None)
            code = getattr(body, "code", None) if body is not None else None
            if code == "SANDBOX_ALREADY_EXISTS":
                try:
                    sb = await self._sdk.sandboxes.get_sandbox(sandbox_name)
                except Exception:
                    if gateway_inline and gateway_profile_id:
                        await self._cleanup_gateway_profile(gateway_profile_id)
                    raise conflict from None
            else:
                if gateway_inline and gateway_profile_id:
                    await self._cleanup_gateway_profile(gateway_profile_id)
                raise
        except Exception:
            if gateway_inline and gateway_profile_id:
                await self._cleanup_gateway_profile(gateway_profile_id)
            raise

        actual_name = getattr(sb, "name", None) or sandbox_name

        state = IsloSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            sandbox_name=actual_name,
            image=options.image,
            workdir=options.workdir,
            env=dict(options.env) if options.env else None,
            gateway_profile_id=gateway_profile_id,
            gateway_profile_inline=gateway_inline,
            exec_poll_interval_s=options.exec_poll_interval_s,
            exec_default_timeout_s=options.exec_default_timeout_s,
            wait_for_running_timeout_s=options.wait_for_running_timeout_s,
        )

        inner = IsloSandboxSession.from_state(state, sdk=self._sdk)
        try:
            await inner._wait_for_running()
        except Exception:
            try:
                await self._sdk.sandboxes.delete_sandbox(actual_name)
            finally:
                if gateway_inline and gateway_profile_id:
                    await self._cleanup_gateway_profile(gateway_profile_id)
            raise

        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, IsloSandboxSession):
            raise TypeError("IsloSandboxClient.delete expects an IsloSandboxSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        if inner.state.gateway_profile_inline and inner.state.gateway_profile_id:
            await self._cleanup_gateway_profile(inner.state.gateway_profile_id)
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, IsloSandboxSessionState):
            raise TypeError("IsloSandboxClient.resume expects an IsloSandboxSessionState")

        # Try to reattach.
        attached = False
        try:
            sb = await self._sdk.sandboxes.get_sandbox(state.sandbox_name)
            status = (getattr(sb, "status", "") or "").lower()
            if status == "running":
                attached = True
            elif status in {"paused", "stopped"}:
                # Best-effort resume; if the SDK exposes resume_sandbox, we'd call it here.
                attached = False
            else:
                attached = False
        except Exception:
            attached = False

        if attached:
            inner = IsloSandboxSession.from_state(state, sdk=self._sdk, attached=True)
            inner._set_start_state_preserved(True)
            return self._wrap_session(inner, instrumentation=self._instrumentation)

        # Fall back: create a fresh sandbox under the same name and let the runner
        # hydrate the workspace from the snapshot state.
        recreate_kwargs: dict[str, Any] = {"name": state.sandbox_name}
        if state.image is not None:
            recreate_kwargs["image"] = state.image
        if state.workdir is not None:
            recreate_kwargs["workdir"] = state.workdir
        if state.env:
            recreate_kwargs["env"] = dict(state.env)
        if state.gateway_profile_id is not None:
            recreate_kwargs["gateway_profile"] = state.gateway_profile_id

        sb = await _create_sandbox_with_retry(self._sdk, **recreate_kwargs)

        state.workspace_root_ready = False
        inner = IsloSandboxSession.from_state(state, sdk=self._sdk)
        await inner._wait_for_running()
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return IsloSandboxSessionState.model_validate(payload)

    # ------------------------------------------------------------------
    # Gateway profile lifecycle
    # ------------------------------------------------------------------

    async def _resolve_gateway_profile(
        self,
        gateway: str | IsloGatewayProfile | None,
        *,
        session_id: uuid.UUID,
    ) -> tuple[str | None, str | None, bool]:
        """Returns ``(profile_ref_for_create_sandbox, profile_id, was_inline)``.

        ``profile_ref_for_create_sandbox`` is the value passed to
        ``create_sandbox(gateway_profile=...)`` — either the user-supplied string or the id
        of the inline profile we just created.
        """
        if gateway is None:
            return None, None, False
        if isinstance(gateway, str):
            return gateway, None, False
        if not isinstance(gateway, IsloGatewayProfile):
            raise ConfigurationError(
                message=(
                    "gateway_profile must be a string (existing profile name/id) or an "
                    "IsloGatewayProfile"
                ),
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": "islo", "type": type(gateway).__name__},
            )

        profile_name = gateway.name or f"agents-sdk-{session_id.hex[:12]}"
        try:
            profile = await self._sdk.gateway_profiles.create_gateway_profile(
                name=profile_name,
                description=gateway.description
                or "Created by openai-agents-python IsloSandboxClient",
                default_action=gateway.default_action,
                internet_enabled=gateway.internet_enabled,
            )
        except Exception as exc:
            raise ConfigurationError(
                message=f"failed to create islo gateway profile {profile_name!r}: {exc}",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": "islo", "profile_name": profile_name},
            ) from exc

        profile_id = getattr(profile, "id", None) or getattr(profile, "profile_id", None)
        if not profile_id:
            raise ConfigurationError(
                message="islo gateway profile create did not return an id",
                error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                op="start",
                context={"backend": "islo", "profile_name": profile_name},
            )

        # Add rules in declaration order. priority defaults to index*10 if unspecified.
        for index, rule in enumerate(gateway.rules):
            priority = rule.priority if rule.priority is not None else (index + 1) * 10
            try:
                await self._sdk.gateway_profiles.create_gateway_rule(
                    profile_id,
                    host_pattern=rule.host_pattern,
                    priority=priority,
                    path_pattern=rule.path_pattern,
                    methods=list(rule.methods) if rule.methods else None,
                    action=rule.action,
                    rate_limit_rpm=rule.rate_limit_rpm,
                    provider_key=rule.provider_key,
                )
            except Exception as exc:
                # Best-effort cleanup: tear down the half-built profile so we don't
                # leave a dangling resource.
                await self._cleanup_gateway_profile(profile_id)
                raise ConfigurationError(
                    message=(
                        f"failed to create islo gateway rule for host {rule.host_pattern!r}: {exc}"
                    ),
                    error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
                    op="start",
                    context={
                        "backend": "islo",
                        "profile_id": profile_id,
                        "host_pattern": rule.host_pattern,
                    },
                ) from exc

        return profile_id, profile_id, True

    async def _cleanup_gateway_profile(self, profile_id: str) -> None:
        try:
            await self._sdk.gateway_profiles.delete_gateway_profile(profile_id)
        except Exception:
            # Best-effort. If the profile is still bound elsewhere, deletion may fail; we
            # don't want a teardown error to mask the original failure path.
            return


__all__ = [
    "IsloGatewayProfile",
    "IsloGatewayRule",
    "IsloSandboxClient",
    "IsloSandboxClientOptions",
    "IsloSandboxSession",
    "IsloSandboxSessionState",
]
