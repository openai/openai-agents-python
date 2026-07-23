from __future__ import annotations

import asyncio
import io
import math
import shlex
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from azure.containerapps.sandbox import AddPortRequest, PortAuthConfig, endpoint_for_region
from azure.containerapps.sandbox.aio import SandboxGroupClient
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential

from ....sandbox.errors import (
    ExecTimeoutError,
    ExposedPortUnavailableError,
    MountConfigError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.dependencies import Dependencies
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.pty_types import PtyExecUpdate
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ....sandbox.session.sandbox_client import BaseSandboxClient
from ....sandbox.session.sandbox_session import SandboxSession
from ....sandbox.session.sandbox_session_state import SandboxSessionState
from ....sandbox.snapshot import NoopSnapshot, SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str
from .errors import (
    create_error,
    delete_error,
    exec_error,
    port_error,
    read_error,
    resume_error,
    write_error,
)
from .models import ACASandboxesClientOptions, ACASandboxesSessionState

if TYPE_CHECKING:
    from azure.containerapps.sandbox.aio import SandboxClient

_PTY_UNSUPPORTED_MESSAGE = (
    "ACA Sandboxes provider v1 does not support PTY sessions. Use session.exec(...) for "
    "one-shot commands or aca sandbox shell for manual debugging."
)
_MOUNTS_UNSUPPORTED_MESSAGE = (
    "ACA Sandboxes provider v1 does not support manifest mount entries. Platform volumes "
    "exist in ACA, but this provider release does not expose mount strategies yet."
)
_SNAPSHOTS_UNSUPPORTED_MESSAGE = (
    "ACA Sandboxes provider v1 does not integrate native ACA snapshots with OpenAI session "
    "snapshot lifecycle."
)


def _port_endpoint_from_url(url: str, *, requested_port: int) -> ExposedPortEndpoint:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ExposedPortUnavailableError(
            port=requested_port,
            exposed_ports=(requested_port,),
            reason="backend_unavailable",
            context={"backend": "aca_sandboxes", "returned_url": url},
        )
    tls = parsed.scheme == "https"
    return ExposedPortEndpoint(
        host=parsed.hostname,
        port=parsed.port or (443 if tls else 80),
        tls=tls,
        query=parsed.query,
    )


class ACASandboxesSession(BaseSandboxSession):
    """ACA-backed sandbox session."""

    state: ACASandboxesSessionState

    def __init__(
        self,
        *,
        state: ACASandboxesSessionState,
        sandbox_client: SandboxClient,
    ) -> None:
        self.state = state
        self._sandbox_client: SandboxClient | None = sandbox_client
        self._running = True

    def _require_sandbox_client(self) -> SandboxClient:
        sandbox_client = self._sandbox_client
        if sandbox_client is None:
            raise RuntimeError(f"ACA sandbox {self.state.sandbox_id!r} is not attached.")
        return sandbox_client

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.sandbox_id

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    async def _validate_manifest_application(self, *, only_ephemeral: bool = False) -> None:
        _ = only_ephemeral
        if self.state.manifest.mount_targets():
            raise MountConfigError(
                message=_MOUNTS_UNSUPPORTED_MESSAGE,
                context={"backend": "aca_sandboxes", "feature": "mounts", "release": "v1"},
            )

    async def _prepare_backend_workspace(self) -> None:
        root = self._workspace_root_path()
        try:
            sandbox_client = self._require_sandbox_client()
            try:
                await sandbox_client.stat_file(root.as_posix())
            except ResourceNotFoundError:
                await sandbox_client.mkdir(root.as_posix())
        except Exception as error:
            raise WorkspaceStartError(
                path=root,
                message=f"ACA sandbox workspace root setup failed: {error}",
                context={"backend": "aca_sandboxes", "sandbox_id": self.state.sandbox_id},
                cause=error,
            ) from error

    async def _after_start(self) -> None:
        self._running = True

    async def _after_start_failed(self) -> None:
        self._running = False

    async def _shutdown_backend(self) -> None:
        sandbox_client = self._sandbox_client
        if sandbox_client is None:
            return
        try:
            await sandbox_client.stop()
        finally:
            await sandbox_client.close()
            self._sandbox_client = None
            self._running = False

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        command_text = shlex.join(str(part) for part in command)
        operation = self._require_sandbox_client().exec(
            command_text,
            working_directory=self.state.manifest.root,
        )
        try:
            result = (
                await asyncio.wait_for(operation, timeout=timeout)
                if timeout is not None
                else await operation
            )
        except asyncio.TimeoutError as error:
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=error) from error
        except Exception as error:
            raise exec_error(error, command=command) from error
        return ExecResult(
            stdout=result.stdout.encode("utf-8", errors="replace"),
            stderr=result.stderr.encode("utf-8", errors="replace"),
            exit_code=int(result.exit_code),
        )

    def supports_pty(self) -> bool:
        return False

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        _ = (command, timeout, shell, user, tty, yield_time_s, max_output_tokens)
        raise NotImplementedError(_PTY_UNSUPPORTED_MESSAGE)

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        _ = (session_id, chars, yield_time_s, max_output_tokens)
        raise NotImplementedError(_PTY_UNSUPPORTED_MESSAGE)

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            await self._check_read_with_exec(path, user=user)
        workspace_path = await self._validate_path_access(path)
        try:
            payload = await self._require_sandbox_client().read_file(
                sandbox_path_str(workspace_path)
            )
        except Exception as error:
            raise read_error(error, path=error_path) from error
        return io.BytesIO(payload)

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
            await self._require_sandbox_client().write_file(
                sandbox_path_str(workspace_path),
                bytes(payload),
            )
        except Exception as error:
            raise write_error(error, path=workspace_path) from error

    async def running(self) -> bool:
        if not self._running or self._sandbox_client is None:
            return False
        try:
            sandbox = await self._sandbox_client.get()
        except Exception:
            return False
        self._running = sandbox.state == "Running"
        return self._running

    async def persist_workspace(self) -> io.IOBase:
        raise NotImplementedError(_SNAPSHOTS_UNSUPPORTED_MESSAGE)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise NotImplementedError(_SNAPSHOTS_UNSUPPORTED_MESSAGE)

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        sandbox_client = self._require_sandbox_client()
        try:
            sandbox = await sandbox_client.get()
            matching = next((item for item in sandbox.ports if item.port == port), None)
            if matching is None:
                matching = await sandbox_client.add_port(
                    port,
                    anonymous=self.state.exposed_port_anonymous,
                )
        except Exception as error:
            mapped = port_error(
                error,
                sandbox_id=self.state.sandbox_id,
                port=port,
            )
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context=mapped.context,
                cause=error,
                retryable=mapped.retryable,
            ) from error

        if not matching.url:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={
                    "backend": "aca_sandboxes",
                    "sandbox_id": self.state.sandbox_id,
                    "reason": "aca_returned_no_url",
                },
            )
        return _port_endpoint_from_url(matching.url, requested_port=port)


class ACASandboxesClient(BaseSandboxClient[ACASandboxesClientOptions | None]):
    """Client for ACA Sandboxes."""

    backend_id = "aca_sandboxes"
    supports_default_options = True

    def __init__(
        self,
        *,
        region: str,
        subscription_id: str,
        resource_group: str,
        sandbox_group: str,
        credential: AsyncTokenCredential | None = None,
        group_client: SandboxGroupClient | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self._region = region
        self._subscription_id = subscription_id
        self._resource_group = resource_group
        self._sandbox_group = sandbox_group
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies
        self._owns_group_client = group_client is None
        self._owns_credential = group_client is None and credential is None
        self._credential = credential
        if group_client is None:
            self._credential = credential or DefaultAzureCredential()
            group_client = SandboxGroupClient(
                endpoint_for_region(region),
                self._credential,
                subscription_id=subscription_id,
                resource_group=resource_group,
                sandbox_group=sandbox_group,
            )
        self._group_client = group_client

    async def close(self) -> None:
        if self._owns_group_client:
            await self._group_client.close()
        if self._owns_credential and self._credential is not None:
            await self._credential.close()

    async def __aenter__(self) -> ACASandboxesClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @staticmethod
    def _assert_manifest_supported(manifest: Manifest) -> None:
        if manifest.mount_targets():
            raise MountConfigError(
                message=_MOUNTS_UNSUPPORTED_MESSAGE,
                context={"backend": "aca_sandboxes", "feature": "mounts", "release": "v1"},
            )

    @staticmethod
    def _assert_snapshot_supported(snapshot: SnapshotBase) -> None:
        if not isinstance(snapshot, NoopSnapshot):
            raise NotImplementedError(_SNAPSHOTS_UNSUPPORTED_MESSAGE)

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: ACASandboxesClientOptions | None = None,
    ) -> SandboxSession:
        resolved_options = options or ACASandboxesClientOptions()
        resolved_manifest = manifest or Manifest()
        self._assert_manifest_supported(resolved_manifest)
        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        self._assert_snapshot_supported(snapshot_instance)
        manifest_environment = await resolved_manifest.environment.resolve()
        environment = {
            **(resolved_options.environment or {}),
            **manifest_environment,
        }
        create_kwargs: dict[str, Any] = {
            "disk": resolved_options.disk,
            "auto_suspend_seconds": resolved_options.auto_suspend_seconds,
            "auto_suspend_mode": resolved_options.auto_suspend_mode,
            "polling_interval": math.ceil(resolved_options.polling_interval_seconds),
            "polling_timeout": math.ceil(resolved_options.polling_timeout_seconds),
        }
        if resolved_options.cpu is not None:
            create_kwargs["cpu"] = resolved_options.cpu
        if resolved_options.memory is not None:
            create_kwargs["memory"] = resolved_options.memory
        if resolved_options.disk_size is not None:
            create_kwargs["disk_size"] = resolved_options.disk_size
        if resolved_options.labels is not None:
            create_kwargs["labels"] = resolved_options.labels
        if environment:
            create_kwargs["environment"] = environment
        if resolved_options.exposed_ports:
            create_kwargs["ports"] = [
                AddPortRequest(
                    port=port,
                    auth=(
                        PortAuthConfig(anonymous=True)
                        if resolved_options.exposed_port_anonymous
                        else None
                    ),
                )
                for port in resolved_options.exposed_ports
            ]

        try:
            poller = await self._group_client.begin_create_sandbox(**create_kwargs)
            sandbox_client = await poller.result()
        except Exception as error:
            raise create_error(error) from error

        state = ACASandboxesSessionState(
            session_id=session_id,
            snapshot=snapshot_instance,
            manifest=resolved_manifest,
            exposed_ports=resolved_options.exposed_ports,
            sandbox_id=sandbox_client.sandbox_id,
            subscription_id=self._subscription_id,
            resource_group=self._resource_group,
            sandbox_group=self._sandbox_group,
            region=self._region,
            disk=resolved_options.disk,
            disk_size=resolved_options.disk_size,
            auto_suspend_seconds=resolved_options.auto_suspend_seconds,
            auto_suspend_mode=resolved_options.auto_suspend_mode,
            exposed_port_anonymous=resolved_options.exposed_port_anonymous,
            ensure_running_timeout_seconds=resolved_options.ensure_running_timeout_seconds,
            labels=resolved_options.labels,
            environment=resolved_options.environment,
        )
        inner = ACASandboxesSession(state=state, sandbox_client=sandbox_client)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, ACASandboxesSession):
            raise TypeError("ACASandboxesClient.delete expects an ACASandboxesSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        try:
            poller = await self._group_client.begin_delete_sandbox(inner.state.sandbox_id)
            await poller.result()
        except Exception as error:
            raise delete_error(error, sandbox_id=inner.state.sandbox_id) from error
        return session

    def _assert_state_scope(self, state: ACASandboxesSessionState) -> None:
        expected = (
            self._subscription_id,
            self._resource_group,
            self._sandbox_group,
            self._region,
        )
        actual = (
            state.subscription_id,
            state.resource_group,
            state.sandbox_group,
            state.region,
        )
        if actual != expected:
            raise ValueError(
                "ACA session state scope does not match ACASandboxesClient "
                "subscription, resource group, sandbox group, and region."
            )

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, ACASandboxesSessionState):
            raise TypeError("ACASandboxesClient.resume expects ACASandboxesSessionState")
        self._assert_state_scope(state)
        self._assert_manifest_supported(state.manifest)
        self._assert_snapshot_supported(state.snapshot)
        sandbox_client = self._group_client.get_sandbox_client(state.sandbox_id)
        sandbox_state: str | None = None
        stopped_reason: str | None = None
        try:
            sandbox = await sandbox_client.get()
            sandbox_state = sandbox.state
            if sandbox.state_details is not None:
                stopped_reason = sandbox.state_details.stopped_reason
            if stopped_reason == "Disabled":
                raise RuntimeError("ACA sandbox is administratively disabled.")
            await sandbox_client.ensure_running(
                timeout=math.ceil(state.ensure_running_timeout_seconds)
            )
        except Exception as error:
            if isinstance(error, RuntimeError):
                try:
                    refreshed = await sandbox_client.get()
                    sandbox_state = refreshed.state
                    stopped_reason = (
                        refreshed.state_details.stopped_reason
                        if refreshed.state_details is not None
                        else None
                    )
                except Exception:
                    pass
            await sandbox_client.close()
            raise resume_error(
                error,
                sandbox_id=state.sandbox_id,
                timeout_s=state.ensure_running_timeout_seconds,
                state=sandbox_state,
                stopped_reason=stopped_reason,
            ) from error

        inner = ACASandboxesSession(state=state, sandbox_client=sandbox_client)
        inner._set_start_state_preserved(True, system=True)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return cast(SandboxSessionState, ACASandboxesSessionState.model_validate(payload))


__all__ = [
    "ACASandboxesClient",
    "ACASandboxesClientOptions",
    "ACASandboxesSession",
    "ACASandboxesSessionState",
]
