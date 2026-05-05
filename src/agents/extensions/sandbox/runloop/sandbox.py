"""
Runloop sandbox (https://runloop.ai) implementation.

This module provides a Runloop-backed sandbox client/session implementation backed by
`runloop_api_client.sdk.AsyncRunloopSDK`.

The `runloop_api_client` dependency is optional, so package-level exports should guard imports of
this module. Within this module, Runloop SDK imports are lazy so users without the extra can still
import the package.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import posixpath
import shlex
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, Field
from runloop_api_client.types import (
    AfterIdle as _RunloopSdkAfterIdle,
    LaunchParameters as _RunloopSdkLaunchParameters,
)
from runloop_api_client.types.shared.launch_parameters import (
    UserParameters as _RunloopSdkUserParameters,
)

from ....sandbox.entries import Mount
from ....sandbox.errors import (
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
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER, RuntimeHelperScript
from ....sandbox.session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

if TYPE_CHECKING:
    from runloop_api_client.sdk.async_execution_result import (
        AsyncExecutionResult as RunloopAsyncExecutionResult,
    )
    from runloop_api_client.sdk.async_snapshot import AsyncSnapshot as RunloopAsyncSnapshot
    from runloop_api_client.types.devbox_view import DevboxView as RunloopDevboxView

DEFAULT_RUNLOOP_WORKSPACE_ROOT = "/home/user"
DEFAULT_RUNLOOP_ROOT_WORKSPACE_ROOT = "/root"
_RUNLOOP_DEFAULT_HOME = PurePosixPath("/home/user")
_RUNLOOP_ROOT_HOME = PurePosixPath("/root")
_RUNLOOP_SANDBOX_SNAPSHOT_MAGIC = b"RUNLOOP_SANDBOX_SNAPSHOT_V1\n"

logger = logging.getLogger(__name__)

RunloopAfterIdle = _RunloopSdkAfterIdle
RunloopLaunchParameters = _RunloopSdkLaunchParameters
RunloopUserParameters = _RunloopSdkUserParameters


@dataclass(frozen=True)
class _RunloopSdkImports:
    async_sdk: type[Any]
    api_connection_error: type[BaseException]
    api_response_validation_error: type[BaseException]
    api_status_error: type[BaseException]
    api_timeout_error: type[BaseException]
    not_found_error: type[BaseException]
    polling_config: type[Any] | None
    polling_timeout: type[BaseException] | None
    runloop_error: type[BaseException]


_RUNLOOP_SDK_IMPORTS: _RunloopSdkImports | None = None


def _import_runloop_sdk() -> _RunloopSdkImports:
    global _RUNLOOP_SDK_IMPORTS
    if _RUNLOOP_SDK_IMPORTS is not None:
        return _RUNLOOP_SDK_IMPORTS

    try:
        from runloop_api_client import (
            APIConnectionError,
            APIResponseValidationError,
            APIStatusError,
            APITimeoutError,
            NotFoundError,
            RunloopError,
        )
        from runloop_api_client.sdk import AsyncRunloopSDK
    except ImportError as e:
        raise ImportError(
            "RunloopSandboxClient requires the optional `runloop_api_client` dependency.\n"
            "Install the Runloop extra before using this sandbox backend."
        ) from e

    polling_config: type[Any] | None = None
    polling_timeout: type[BaseException] | None = None
    try:
        from runloop_api_client.lib.polling import (
            PollingConfig as RunloopPollingConfig,
            PollingTimeout as RunloopPollingTimeout,
        )
    except ImportError:
        pass
    else:
        polling_config = RunloopPollingConfig
        polling_timeout = RunloopPollingTimeout

    _RUNLOOP_SDK_IMPORTS = _RunloopSdkImports(
        async_sdk=AsyncRunloopSDK,
        api_connection_error=APIConnectionError,
        api_response_validation_error=APIResponseValidationError,
        api_status_error=APIStatusError,
        api_timeout_error=APITimeoutError,
        not_found_error=NotFoundError,
        polling_config=polling_config,
        polling_timeout=polling_timeout,
        runloop_error=RunloopError,
    )
    return _RUNLOOP_SDK_IMPORTS


def _encode_runloop_snapshot_ref(*, snapshot_id: str) -> bytes:
    body = json.dumps({"snapshot_id": snapshot_id}, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return _RUNLOOP_SANDBOX_SNAPSHOT_MAGIC + body


def _decode_runloop_snapshot_ref(raw: bytes) -> str | None:
    if not raw.startswith(_RUNLOOP_SANDBOX_SNAPSHOT_MAGIC):
        return None
    body = raw[len(_RUNLOOP_SANDBOX_SNAPSHOT_MAGIC) :]
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    snapshot_id = obj.get("snapshot_id") if isinstance(obj, dict) else None
    return snapshot_id if isinstance(snapshot_id, str) and snapshot_id else None


def _runloop_json_safe_body(body: object) -> tuple[str, object] | None:
    if isinstance(body, str | int | float | bool) or body is None:
        return ("provider_body", body)
    if isinstance(body, dict | list):
        try:
            json.dumps(body)
        except TypeError:
            return ("provider_body_repr", repr(body))
        return ("provider_body", body)
    return ("provider_body_repr", repr(body))


def _runloop_error_context(
    exc: BaseException,
    *,
    backend_detail: str | None = None,
) -> dict[str, object]:
    context: dict[str, object] = {
        "backend": "runloop",
        "cause_type": type(exc).__name__,
    }
    if backend_detail is not None:
        context["detail"] = backend_detail

    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        context["provider_message"] = message
    else:
        provider_message = str(exc)
        if provider_message:
            context["provider_message"] = provider_message

    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if not isinstance(status_code, int):
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            status_code = response_status
    if isinstance(status_code, int):
        context["http_status"] = status_code

    request = getattr(exc, "request", None)
    request_url = getattr(request, "url", None)
    if request_url is not None:
        context["request_url"] = str(request_url)
    request_method = getattr(request, "method", None)
    if isinstance(request_method, str) and request_method:
        context["request_method"] = request_method

    if hasattr(exc, "body"):
        safe_body = _runloop_json_safe_body(getattr(exc, "body", None))
        if safe_body is not None:
            context[safe_body[0]] = safe_body[1]

    return context


def _is_runloop_timeout(exc: BaseException) -> bool:
    polling_timeout = _import_runloop_sdk().polling_timeout
    if polling_timeout is not None and isinstance(exc, polling_timeout):
        return True
    if isinstance(exc, _import_runloop_sdk().api_timeout_error):
        return True
    if isinstance(exc, _import_runloop_sdk().api_status_error):
        status_code = getattr(exc, "status_code", None)
        response = getattr(exc, "response", None)
        if not isinstance(status_code, int):
            response_status = getattr(response, "status_code", None)
            if isinstance(response_status, int):
                status_code = response_status
        return status_code == 408
    return False


def _runloop_status_code(exc: BaseException) -> int | None:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if not isinstance(status_code, int):
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            status_code = response_status
    return status_code if isinstance(status_code, int) else None


def _runloop_error_message(exc: BaseException) -> str | None:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = body.get("message") or body.get("error")
        if isinstance(message, str) and message:
            return message

    message = getattr(exc, "message", None)
    if isinstance(message, str) and message:
        return message

    if exc.args:
        first = exc.args[0]
        if isinstance(first, str) and first:
            return first

    return None


def _runloop_provider_error_types() -> tuple[type[BaseException], ...]:
    sdk_imports = _import_runloop_sdk()
    return (
        sdk_imports.api_connection_error,
        sdk_imports.api_response_validation_error,
        sdk_imports.api_status_error,
        sdk_imports.runloop_error,
    )


def _is_runloop_not_found(exc: BaseException) -> bool:
    return isinstance(exc, _import_runloop_sdk().not_found_error)


def _is_runloop_conflict(exc: BaseException) -> bool:
    if not isinstance(exc, _import_runloop_sdk().api_status_error):
        return False

    status_code = _runloop_status_code(exc)
    if status_code == 409:
        return True

    message = _runloop_error_message(exc)
    if status_code == 400 and isinstance(message, str):
        return "already exists" in message.lower()

    return False


def _runloop_polling_config(*, timeout_s: float | None) -> object | None:
    if timeout_s is None:
        return None
    polling_config = _import_runloop_sdk().polling_config
    if polling_config is None:
        return None
    return cast(object, polling_config(timeout_seconds=max(float(timeout_s), 0.001)))


def _is_runloop_provider_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        _runloop_provider_error_types(),
    )


class RunloopTimeouts(BaseModel):
    """Timeout configuration for Runloop sandbox operations."""

    model_config = {"frozen": True}

    exec_timeout_unbounded_s: float = Field(default=24 * 60 * 60, ge=1)
    create_s: float = Field(default=300.0, ge=1)
    keepalive_s: float = Field(default=10.0, ge=1)
    cleanup_s: float = Field(default=30.0, ge=1)
    fast_op_s: float = Field(default=30.0, ge=1)
    file_upload_s: float = Field(default=1800.0, ge=1)
    file_download_s: float = Field(default=1800.0, ge=1)
    snapshot_s: float = Field(default=300.0, ge=1)
    suspend_s: float = Field(default=120.0, ge=1)
    resume_s: float = Field(default=300.0, ge=1)


class RunloopTunnelConfig(BaseModel):
    """Runloop public tunnel configuration."""

    model_config = {"frozen": True}

    auth_mode: Literal["open", "authenticated"] | None = None
    http_keep_alive: bool | None = None
    wake_on_http: bool | None = None


class RunloopGatewaySpec(BaseModel):
    """Runloop agent gateway binding."""

    model_config = {"frozen": True}

    gateway: str = Field(min_length=1)
    secret: str = Field(min_length=1)


class RunloopMcpSpec(BaseModel):
    """Runloop MCP gateway binding."""

    model_config = {"frozen": True}

    mcp_config: str = Field(min_length=1)
    secret: str = Field(min_length=1)


def _normalize_runloop_user_parameters(
    user_parameters: RunloopUserParameters | dict[str, object] | None,
) -> RunloopUserParameters | None:
    if isinstance(user_parameters, RunloopUserParameters):
        return user_parameters
    if user_parameters is None:
        return None
    if isinstance(user_parameters, BaseModel):
        return RunloopUserParameters.model_validate(user_parameters.model_dump(mode="json"))
    return RunloopUserParameters.model_validate(user_parameters)


def _normalize_runloop_launch_parameters(
    launch_parameters: RunloopLaunchParameters | dict[str, object] | None,
) -> RunloopLaunchParameters | None:
    if isinstance(launch_parameters, RunloopLaunchParameters):
        return launch_parameters
    if launch_parameters is None:
        return None
    if isinstance(launch_parameters, BaseModel):
        return RunloopLaunchParameters.model_validate(launch_parameters.model_dump(mode="json"))
    return RunloopLaunchParameters.model_validate(launch_parameters)


def _normalize_runloop_tunnel_config(
    tunnel: RunloopTunnelConfig | dict[str, object] | None,
) -> RunloopTunnelConfig | None:
    if isinstance(tunnel, RunloopTunnelConfig):
        return tunnel
    if tunnel is None:
        return None
    if isinstance(tunnel, BaseModel):
        return RunloopTunnelConfig.model_validate(tunnel.model_dump(mode="json"))
    return RunloopTunnelConfig.model_validate(tunnel)


class RunloopSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Runloop sandbox."""

    type: Literal["runloop"] = "runloop"
    blueprint_id: str | None = None
    blueprint_name: str | None = None
    env_vars: dict[str, str] | None = None
    pause_on_exit: bool = False
    name: str | None = None
    timeouts: RunloopTimeouts | dict[str, object] | None = None
    exposed_ports: tuple[int, ...] = ()
    user_parameters: RunloopUserParameters | dict[str, object] | None = None
    launch_parameters: RunloopLaunchParameters | dict[str, object] | None = None
    tunnel: RunloopTunnelConfig | dict[str, object] | None = None
    gateways: dict[str, RunloopGatewaySpec] | None = None
    mcp: dict[str, RunloopMcpSpec] | None = None
    metadata: dict[str, str] | None = None
    managed_secrets: dict[str, str] | None = None

    def __init__(
        self,
        blueprint_id: str | None = None,
        blueprint_name: str | None = None,
        env_vars: dict[str, str] | None = None,
        pause_on_exit: bool = False,
        name: str | None = None,
        timeouts: RunloopTimeouts | dict[str, object] | None = None,
        exposed_ports: tuple[int, ...] = (),
        user_parameters: RunloopUserParameters | dict[str, object] | None = None,
        launch_parameters: RunloopLaunchParameters | dict[str, object] | None = None,
        tunnel: RunloopTunnelConfig | dict[str, object] | None = None,
        gateways: dict[str, RunloopGatewaySpec] | None = None,
        mcp: dict[str, RunloopMcpSpec] | None = None,
        metadata: dict[str, str] | None = None,
        managed_secrets: dict[str, str] | None = None,
        *,
        type: Literal["runloop"] = "runloop",
    ) -> None:
        super().__init__(
            type=type,
            blueprint_id=blueprint_id,
            blueprint_name=blueprint_name,
            env_vars=env_vars,
            pause_on_exit=pause_on_exit,
            name=name,
            timeouts=timeouts,
            exposed_ports=exposed_ports,
            user_parameters=user_parameters,
            launch_parameters=launch_parameters,
            tunnel=tunnel,
            gateways=gateways,
            mcp=mcp,
            metadata=metadata,
            managed_secrets=managed_secrets,
        )


class RunloopSandboxSessionState(SandboxSessionState):
    """Serializable state for a Runloop-backed session."""

    type: Literal["runloop"] = "runloop"
    devbox_id: str
    blueprint_id: str | None = None
    blueprint_name: str | None = None
    base_env_vars: dict[str, str] = Field(default_factory=dict)
    pause_on_exit: bool = False
    name: str | None = None
    timeouts: RunloopTimeouts = Field(default_factory=RunloopTimeouts)
    user_parameters: RunloopUserParameters | None = None
    launch_parameters: RunloopLaunchParameters | None = None
    tunnel: RunloopTunnelConfig | None = None
    gateways: dict[str, RunloopGatewaySpec] = Field(default_factory=dict)
    mcp: dict[str, RunloopMcpSpec] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class RunloopPlatformBlueprintsClient:
    _sdk: Any

    async def list(self, **params: object) -> object:
        return await self._sdk.blueprint.list(**params)

    async def list_public(self, **params: object) -> object:
        return await self._sdk.api.blueprints.list_public(**params)

    def get(self, blueprint_id: str) -> Any:
        return self._sdk.blueprint.from_id(blueprint_id)

    async def logs(self, blueprint_id: str, **params: object) -> object:
        return await self._sdk.api.blueprints.logs(blueprint_id, **params)

    async def create(self, **params: object) -> object:
        return await self._sdk.blueprint.create(**params)

    async def await_build_complete(self, blueprint_id: str, **params: object) -> object:
        return await self._sdk.api.blueprints.await_build_complete(blueprint_id, **params)

    async def delete(self, blueprint_id: str, **params: object) -> object:
        return await self.get(blueprint_id).delete(**params)


@dataclass(frozen=True)
class RunloopPlatformBenchmarksClient:
    _sdk: Any

    async def list(self, **params: object) -> object:
        return await self._sdk.benchmark.list(**params)

    async def list_public(self, **params: object) -> object:
        return await self._sdk.api.benchmarks.list_public(**params)

    def get(self, benchmark_id: str) -> Any:
        return self._sdk.benchmark.from_id(benchmark_id)

    async def create(self, **params: object) -> object:
        return await self._sdk.benchmark.create(**params)

    async def update(self, benchmark_id: str, **params: object) -> object:
        return await self.get(benchmark_id).update(**params)

    async def definitions(self, benchmark_id: str, **params: object) -> object:
        return await self._sdk.api.benchmarks.definitions(benchmark_id, **params)

    async def start_run(self, benchmark_id: str, **params: object) -> object:
        return await self.get(benchmark_id).start_run(**params)

    async def update_scenarios(
        self,
        benchmark_id: str,
        *,
        scenarios_to_add: tuple[str, ...] | Sequence[str] | None = None,
        scenarios_to_remove: tuple[str, ...] | Sequence[str] | None = None,
        **params: object,
    ) -> object:
        return await self._sdk.api.benchmarks.update_scenarios(
            benchmark_id,
            scenarios_to_add=scenarios_to_add,
            scenarios_to_remove=scenarios_to_remove,
            **params,
        )


@dataclass(frozen=True)
class RunloopPlatformSecretsClient:
    _sdk: Any

    async def create(self, *, name: str, value: str, **params: object) -> object:
        return await self._sdk.secret.create(name=name, value=value, **params)

    async def list(self, **params: object) -> object:
        return await self._sdk.secret.list(**params)

    async def get(self, name: str, **params: object) -> object:
        return await self._sdk.api.secrets.retrieve(name, **params)

    async def update(self, *, name: str, value: str, **params: object) -> object:
        return await self._sdk.secret.update(name, value=value, **params)

    async def delete(self, name: str, **params: object) -> object:
        return await self._sdk.secret.delete(name, **params)


@dataclass(frozen=True)
class RunloopPlatformNetworkPoliciesClient:
    _sdk: Any

    async def create(self, **params: object) -> object:
        return await self._sdk.network_policy.create(**params)

    async def list(self, **params: object) -> object:
        return await self._sdk.network_policy.list(**params)

    def get(self, network_policy_id: str) -> Any:
        return self._sdk.network_policy.from_id(network_policy_id)

    async def update(self, network_policy_id: str, **params: object) -> object:
        return await self.get(network_policy_id).update(**params)

    async def delete(self, network_policy_id: str, **params: object) -> object:
        return await self.get(network_policy_id).delete(**params)


@dataclass(frozen=True)
class RunloopPlatformAxonsClient:
    _sdk: Any

    async def create(self, **params: object) -> object:
        return await self._sdk.axon.create(**params)

    async def list(self, **params: object) -> object:
        return await self._sdk.axon.list(**params)

    def get(self, axon_id: str) -> Any:
        return self._sdk.axon.from_id(axon_id)

    async def publish(self, axon_id: str, **params: object) -> object:
        return await self.get(axon_id).publish(**params)

    async def query_sql(self, axon_id: str, **params: object) -> object:
        return await self.get(axon_id).sql.query(**params)

    async def batch_sql(self, axon_id: str, **params: object) -> object:
        return await self.get(axon_id).sql.batch(**params)


@dataclass(frozen=True)
class RunloopPlatformClient:
    """Thin facade over the Runloop SDK's non-devbox platform resources."""

    _sdk: Any

    @property
    def blueprints(self) -> RunloopPlatformBlueprintsClient:
        return RunloopPlatformBlueprintsClient(self._sdk)

    @property
    def benchmarks(self) -> RunloopPlatformBenchmarksClient:
        return RunloopPlatformBenchmarksClient(self._sdk)

    @property
    def secrets(self) -> RunloopPlatformSecretsClient:
        return RunloopPlatformSecretsClient(self._sdk)

    @property
    def network_policies(self) -> RunloopPlatformNetworkPoliciesClient:
        return RunloopPlatformNetworkPoliciesClient(self._sdk)

    @property
    def axons(self) -> RunloopPlatformAxonsClient:
        return RunloopPlatformAxonsClient(self._sdk)


class RunloopSandboxSession(BaseSandboxSession):
    """Runloop-backed sandbox session implementation."""

    state: RunloopSandboxSessionState
    _sdk: Any
    _devbox: Any
    _skip_start: bool

    def __init__(self, *, state: RunloopSandboxSessionState, sdk: Any, devbox: Any) -> None:
        self.state = state
        self._sdk = sdk
        self._devbox = devbox
        self._skip_start = False

    @classmethod
    def from_state(
        cls,
        state: RunloopSandboxSessionState,
        *,
        sdk: Any,
        devbox: Any,
    ) -> RunloopSandboxSession:
        return cls(state=state, sdk=sdk, devbox=devbox)

    @property
    def devbox_id(self) -> str:
        return self.state.devbox_id

    @property
    def runloop_home(self) -> PurePosixPath:
        return _effective_runloop_home(self.state.user_parameters)

    async def _resolved_envs(self) -> dict[str, str]:
        manifest_envs = await self.state.manifest.environment.resolve()
        return {**self.state.base_env_vars, **manifest_envs}

    def _coerce_exec_timeout(self, timeout_s: float | None) -> float:
        if timeout_s is None:
            return float(self.state.timeouts.exec_timeout_unbounded_s)
        if timeout_s <= 0:
            return 0.001
        return float(timeout_s)

    async def start(self) -> None:
        """Resume a reconnected Runloop devbox without replaying full setup when possible.

        `resume()` marks `_skip_start` when it successfully reconnects to a suspended devbox.
        In that path, Runloop reuses the live machine and only reapplies snapshot or ephemeral
        manifest state if the cached workspace fingerprint no longer matches.
        """
        if self._skip_start:
            if await self.state.snapshot.restorable(dependencies=self.dependencies):
                is_running = await self.running()
                fingerprints_match = await self._can_skip_snapshot_restore_on_resume(
                    is_running=is_running
                )
                if fingerprints_match:
                    await self._reapply_ephemeral_manifest_on_resume()
                else:
                    await self._restore_snapshot_into_workspace_on_resume()
                    if self.should_provision_manifest_accounts_on_resume():
                        await self.provision_manifest_accounts()
                    await self._reapply_ephemeral_manifest_on_resume()
            else:
                await self._reapply_ephemeral_manifest_on_resume()
            return
        await super().start()

    async def shutdown(self) -> None:
        """Suspend or delete the underlying Runloop devbox as the final session cleanup step.

        `pause_on_exit=True` maps to Runloop suspension so the same devbox can be resumed later.
        Otherwise the session shuts the devbox down and treats it as disposable.
        """
        try:
            if self.state.pause_on_exit:
                await self._devbox.suspend(timeout=self.state.timeouts.suspend_s)
                await self._devbox.await_suspended()
            else:
                await self._devbox.shutdown(timeout=self.state.timeouts.cleanup_s)
        except Exception:
            pass

    def supports_pty(self) -> bool:
        return False

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    async def _wrap_command_in_workspace_context(self, command: str) -> str:
        root_q = shlex.quote(self.state.manifest.root)
        envs = await self._resolved_envs()
        if not envs:
            return f"cd {root_q} && {command}"

        env_assignments = " ".join(
            shlex.quote(f"{key}={value}") for key, value in sorted(envs.items())
        )
        return f"cd {root_q} && env -- {env_assignments} {command}"

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        cmd_str = await self._wrap_command_in_workspace_context(shlex.join(str(c) for c in command))
        return await self._run_exec_command(
            cmd_str,
            command=command,
            timeout=timeout,
        )

    async def _run_exec_command(
        self,
        cmd_str: str,
        *,
        command: tuple[str | Path, ...],
        timeout: float | None,
    ) -> ExecResult:
        caller_timeout = self._coerce_exec_timeout(timeout)
        request_timeout = min(caller_timeout, self.state.timeouts.fast_op_s)
        polling_config = _runloop_polling_config(timeout_s=caller_timeout)

        try:
            result: RunloopAsyncExecutionResult = await asyncio.wait_for(
                self._devbox.cmd.exec(
                    cmd_str,
                    timeout=request_timeout,
                    polling_config=polling_config,
                ),
                timeout=caller_timeout,
            )
            stdout = (await result.stdout()).encode("utf-8", errors="replace")
            stderr = (await result.stderr()).encode("utf-8", errors="replace")
            exit_code = int(result.exit_code or 0)
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        except asyncio.TimeoutError as e:
            raise ExecTimeoutError(
                command=command,
                timeout_s=timeout,
                context=_runloop_error_context(e, backend_detail="exec_timeout"),
                cause=e,
            ) from e
        except Exception as e:
            if _is_runloop_timeout(e):
                raise ExecTimeoutError(
                    command=command,
                    timeout_s=timeout,
                    context=_runloop_error_context(e, backend_detail="exec_timeout"),
                    cause=e,
                ) from e
            if _is_runloop_provider_error(e):
                raise ExecTransportError(
                    command=command,
                    context=_runloop_error_context(e, backend_detail="exec_failed"),
                    cause=e,
                ) from e
            raise ExecTransportError(command=command, cause=e) from e

    async def _ensure_tunnel_url(self, port: int) -> str:
        try:
            url = await self._devbox.get_tunnel_url(port, timeout=self.state.timeouts.fast_op_s)
        except Exception as e:
            if _is_runloop_provider_error(e):
                raise ExposedPortUnavailableError(
                    port=port,
                    exposed_ports=self.state.exposed_ports,
                    reason="backend_unavailable",
                    context=_runloop_error_context(e, backend_detail="get_tunnel_url_failed"),
                    cause=e,
                ) from e
            raise
        if isinstance(url, str) and url:
            return url

        try:
            await self._devbox.net.enable_tunnel(
                auth_mode="open",
                http_keep_alive=True,
                wake_on_http=False,
                timeout=self.state.timeouts.fast_op_s,
            )
        except Exception as e:
            if _is_runloop_provider_error(e):
                raise ExposedPortUnavailableError(
                    port=port,
                    exposed_ports=self.state.exposed_ports,
                    reason="backend_unavailable",
                    context=_runloop_error_context(e, backend_detail="enable_tunnel_failed"),
                    cause=e,
                ) from e
            raise
        try:
            url = await self._devbox.get_tunnel_url(port, timeout=self.state.timeouts.fast_op_s)
        except Exception as e:
            if _is_runloop_provider_error(e):
                context = _runloop_error_context(e, backend_detail="get_tunnel_url_failed")
                context["phase"] = "post_enable"
                raise ExposedPortUnavailableError(
                    port=port,
                    exposed_ports=self.state.exposed_ports,
                    reason="backend_unavailable",
                    context=context,
                    cause=e,
                ) from e
            raise
        if not isinstance(url, str) or not url:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "runloop", "detail": "missing_tunnel_url"},
            )
        return url

    async def resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        """Resolve an exposed Runloop port through the provider-managed tunnel endpoint.

        Runloop may not have a tunnel enabled for a devbox yet, so exposed-port resolution can
        trigger tunnel creation before returning the public host, port, and TLS settings.
        """

        return await super().resolve_exposed_port(port)

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            url = await self._ensure_tunnel_url(port)
            split = urlsplit(url)
            host = split.hostname
            if host is None:
                raise ValueError("missing hostname")
            port_value = split.port or (443 if split.scheme == "https" else 80)
            return ExposedPortEndpoint(host=host, port=port_value, tls=split.scheme == "https")
        except ExposedPortUnavailableError:
            raise
        except Exception as e:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": "runloop", "detail": "invalid_tunnel_url"},
                cause=e,
            ) from e

    async def read(self, path: Path | str, *, user: str | User | None = None) -> io.IOBase:
        """Read a file via Runloop's binary file API."""
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            await self._check_read_with_exec(path, user=user)

        normalized_path = await self._validate_path_access(path)
        try:
            payload = await self._devbox.file.download(
                path=sandbox_path_str(normalized_path),
                timeout=self.state.timeouts.file_download_s,
            )
            return io.BytesIO(bytes(payload))
        except Exception as e:
            if _is_runloop_not_found(e):
                raise WorkspaceReadNotFoundError(
                    path=error_path,
                    context=_runloop_error_context(e, backend_detail="file_download_failed"),
                    cause=e,
                ) from e
            if _is_runloop_provider_error(e):
                raise WorkspaceArchiveReadError(
                    path=error_path,
                    context=_runloop_error_context(e, backend_detail="file_download_failed"),
                    cause=e,
                ) from e
            raise WorkspaceArchiveReadError(path=error_path, cause=e) from e

    async def write(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        """Write a file through Runloop's upload API using manifest-root workspace paths."""
        error_path = posix_path_as_path(coerce_posix_path(path))
        if user is not None:
            await self._check_write_with_exec(path, user=user)

        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=error_path, actual_type=type(payload).__name__)

        workspace_path = await self._validate_path_access(path, for_write=True)
        await self.mkdir(workspace_path.parent, parents=True)
        try:
            await self._devbox.file.upload(
                path=sandbox_path_str(workspace_path),
                file=bytes(payload),
                timeout=self.state.timeouts.file_upload_s,
            )
        except Exception as e:
            if _is_runloop_provider_error(e):
                raise WorkspaceArchiveWriteError(
                    path=workspace_path,
                    context=_runloop_error_context(e, backend_detail="file_upload_failed"),
                    cause=e,
                ) from e
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        """Report whether the current Runloop devbox is still in the `running` backend state.

        Resume logic relies on this backend status check before deciding whether a suspended devbox
        can be reused directly or whether snapshot restore must rebuild the workspace elsewhere.
        """
        try:
            info: RunloopDevboxView = await self._devbox.get_info(
                timeout=self.state.timeouts.keepalive_s
            )
            return cast(str, info.status) == "running"
        except Exception:
            return False

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        """Create directories via raw exec so workspace-root creation does not depend on `cd`."""

        if user is not None:
            path = await self._check_mkdir_with_exec(path, parents=parents, user=user)
        else:
            path = await self._validate_path_access(path, for_write=True)
        cmd = ["mkdir"]
        if parents:
            cmd.append("-p")
        cmd.extend(["--", sandbox_path_str(path)])
        result = await self._run_exec_command(
            shlex.join(cmd),
            command=tuple(cmd),
            timeout=self.state.timeouts.fast_op_s,
        )
        if not result.ok():
            raise WorkspaceArchiveWriteError(
                path=path,
                context={
                    "reason": "mkdir_failed",
                    "exit_code": result.exit_code,
                    "stderr": result.stderr.decode("utf-8", "replace"),
                },
            )

    async def _backup_plain_skip_paths(self, plain_skip: set[Path]) -> bytes | None:
        if not plain_skip:
            return None

        root = sandbox_path_str(self.state.manifest.root)
        root_q = shlex.quote(root)
        checks = "\n".join(
            (
                f"if [ -e {shlex.quote(rel.as_posix())} ]; then "
                f'set -- "$@" {shlex.quote(rel.as_posix())}; fi'
            )
            for rel in sorted(plain_skip, key=lambda p: p.as_posix())
        )
        command = (
            f"cd {root_q}\n"
            "set --\n"
            f"{checks}\n"
            'if [ "$#" -eq 0 ]; then exit 0; fi\n'
            'tar -cf - "$@" | base64 -w0\n'
        )
        result = await self.exec(command, shell=True, timeout=self.state.timeouts.snapshot_s)
        if not result.ok():
            raise WorkspaceArchiveReadError(
                path=self._workspace_root_path(),
                context={
                    "reason": "ephemeral_backup_failed",
                    "exit_code": result.exit_code,
                    "stderr": result.stderr.decode("utf-8", "replace"),
                },
            )
        encoded = result.stdout.decode("utf-8", "replace").strip()
        if not encoded:
            return None
        try:
            return io.BytesIO(base64.b64decode(encoded.encode("utf-8"), validate=True)).read()
        except Exception as e:
            raise WorkspaceArchiveReadError(
                path=self._workspace_root_path(),
                context={"reason": "ephemeral_backup_invalid_base64"},
                cause=e,
            ) from e

    async def _remove_plain_skip_paths(self, plain_skip: set[Path]) -> None:
        if not plain_skip:
            return
        root = self._workspace_root_path()
        command = ["rm", "-rf", "--"] + [(root / rel).as_posix() for rel in sorted(plain_skip)]
        result = await self.exec(*command, shell=False, timeout=self.state.timeouts.cleanup_s)
        if not result.ok():
            raise WorkspaceArchiveReadError(
                path=root,
                context={
                    "reason": "ephemeral_remove_failed",
                    "exit_code": result.exit_code,
                    "stderr": result.stderr.decode("utf-8", "replace"),
                },
            )

    async def _restore_plain_skip_paths(self, backup: bytes | None) -> None:
        if not backup:
            return
        root = self._workspace_root_path()
        temp_path = root / f".sandbox-runloop-restore-{self.state.session_id.hex}.tar"
        await self.write(temp_path, io.BytesIO(backup))
        try:
            result = await self.exec(
                "mkdir",
                "-p",
                root.as_posix(),
                shell=False,
                timeout=self.state.timeouts.cleanup_s,
            )
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "reason": "ephemeral_restore_mkdir_failed",
                        "exit_code": result.exit_code,
                    },
                )
            result = await self.exec(
                "tar",
                "-xf",
                sandbox_path_str(temp_path),
                "-C",
                root.as_posix(),
                shell=False,
                timeout=self.state.timeouts.snapshot_s,
            )
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "reason": "ephemeral_restore_failed",
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode("utf-8", "replace"),
                    },
                )
        finally:
            try:
                await self.exec("rm", "-f", "--", sandbox_path_str(temp_path), shell=False)
            except Exception:
                pass

    async def persist_workspace(self) -> io.IOBase:
        """Persist the workspace with a native Runloop disk snapshot.

        Before snapshotting, the session temporarily removes ephemeral skip paths and tears down
        ephemeral mounts so the saved disk image contains only durable workspace state, then it
        restores those local-only artifacts afterward.
        """
        root = self._workspace_root_path()
        skip = self._persist_workspace_skip_relpaths()
        mount_targets = self.state.manifest.ephemeral_mount_targets()
        mount_skip_rel_paths: set[Path] = set()
        for _mount_entry, mount_path in mount_targets:
            try:
                mount_skip_rel_paths.add(mount_path.relative_to(root))
            except ValueError:
                continue
        plain_skip = skip - mount_skip_rel_paths

        backup: bytes | None = None
        unmounted_mounts: list[tuple[Mount, Path]] = []
        snapshot_error: WorkspaceArchiveReadError | None = None
        snapshot_id: str | None = None

        try:
            backup = await self._backup_plain_skip_paths(plain_skip)
            await self._remove_plain_skip_paths(plain_skip)

            for mount_entry, mount_path in mount_targets:
                await mount_entry.mount_strategy.teardown_for_snapshot(
                    mount_entry,
                    self,
                    mount_path,
                )
                unmounted_mounts.append((mount_entry, mount_path))

            snapshot: RunloopAsyncSnapshot = await self._devbox.snapshot_disk(
                name=f"sandbox-{self.state.session_id.hex[:12]}",
                metadata={"openai_agents_session_id": self.state.session_id.hex},
                timeout=self.state.timeouts.snapshot_s,
            )
            snapshot_id = snapshot.id
            if not snapshot_id:
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "reason": "snapshot_unexpected_return",
                        "type": type(snapshot).__name__,
                    },
                )
        except WorkspaceArchiveReadError as e:
            snapshot_error = e
        except Exception as e:
            snapshot_error = WorkspaceArchiveReadError(
                path=root,
                context={"reason": "snapshot_failed"},
                cause=e,
            )
        finally:
            remount_error: WorkspaceArchiveReadError | None = None
            for mount_entry, mount_path in reversed(unmounted_mounts):
                try:
                    await mount_entry.mount_strategy.restore_after_snapshot(
                        mount_entry, self, mount_path
                    )
                except Exception as e:
                    current_error = WorkspaceArchiveReadError(path=root, cause=e)
                    if remount_error is None:
                        remount_error = current_error
                    else:
                        additional = remount_error.context.setdefault(
                            "additional_remount_errors", []
                        )
                        assert isinstance(additional, list)
                        additional.append(
                            {
                                "message": current_error.message,
                                "cause_type": type(e).__name__,
                                "cause": str(e),
                            }
                        )
            try:
                await self._restore_plain_skip_paths(backup)
            except Exception as e:
                restore_error = WorkspaceArchiveReadError(path=root, cause=e)
                if remount_error is None:
                    remount_error = restore_error
                else:
                    additional = remount_error.context.setdefault("additional_restore_errors", [])
                    assert isinstance(additional, list)
                    additional.append(
                        {
                            "message": restore_error.message,
                            "cause_type": type(e).__name__,
                            "cause": str(e),
                        }
                    )

            if remount_error is not None:
                if snapshot_error is not None:
                    remount_error.context["snapshot_error_before_restore_corruption"] = {
                        "message": snapshot_error.message
                    }
                raise remount_error

        if snapshot_error is not None:
            raise snapshot_error

        assert snapshot_id is not None
        return io.BytesIO(_encode_runloop_snapshot_ref(snapshot_id=snapshot_id))

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        """Replace the current devbox from a Runloop snapshot reference or tar archive.

        Runloop restore creates a new devbox from the saved disk snapshot and treats that snapshot
        filesystem as authoritative, including any tools or files that originally came from the
        source blueprint, so restore does not reselect a blueprint. Non-native payloads fall back
        to tar hydration so cross-provider snapshots and file snapshots keep working.
        """
        root = self._workspace_root_path()
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(path=root, actual_type=type(raw).__name__)

        snapshot_id = _decode_runloop_snapshot_ref(bytes(raw))
        if snapshot_id is None:
            await self._hydrate_workspace_via_tar(bytes(raw))
            return

        try:
            try:
                await self._devbox.shutdown(timeout=self.state.timeouts.cleanup_s)
            except Exception:
                pass
            envs = await self._resolved_envs()
            create_kwargs = _runloop_create_kwargs(
                blueprint_id=None,
                blueprint_name=None,
                env_vars=envs,
                name=self.state.name,
                user_parameters=self.state.user_parameters,
                launch_parameters=self.state.launch_parameters,
                tunnel=self.state.tunnel,
                gateways=self.state.gateways,
                mcp=self.state.mcp,
                metadata=self.state.metadata,
                secrets=self.state.secret_refs,
            )
            devbox = await self._sdk.devbox.create_from_snapshot(
                snapshot_id,
                timeout=self.state.timeouts.resume_s,
                **create_kwargs,
            )
            self._devbox = devbox
            self.state.devbox_id = devbox.id
        except Exception as e:
            context: dict[str, object] = {
                "reason": "snapshot_restore_failed",
                "snapshot_id": snapshot_id,
            }
            if _is_runloop_provider_error(e):
                context.update(_runloop_error_context(e, backend_detail="snapshot_restore_failed"))
            raise WorkspaceArchiveWriteError(
                path=root,
                context=context,
                cause=e,
            ) from e

    async def _restore_snapshot_into_workspace_on_resume(self) -> None:
        """Restore snapshots on resume, preserving Runloop's native disk-snapshot fast path."""

        root = self._workspace_root_path()
        workspace_archive = await self.state.snapshot.restore(dependencies=self.dependencies)
        try:
            raw = workspace_archive.read()
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not isinstance(raw, bytes | bytearray):
                raise WorkspaceWriteTypeError(path=root, actual_type=type(raw).__name__)

            payload = bytes(raw)
            if _decode_runloop_snapshot_ref(payload) is None:
                # Most providers restore tar snapshots by clearing the workspace first, then
                # extracting into an empty root. Runloop differs only for its native snapshot
                # refs, which already replace the entire devbox disk and therefore should not
                # pre-clear the workspace root on resume.
                await self._clear_workspace_root_on_resume()
            await self.hydrate_workspace(io.BytesIO(payload))
        finally:
            try:
                workspace_archive.close()
            except Exception:
                pass

    async def _hydrate_workspace_via_tar(self, payload: bytes) -> None:
        root = self._workspace_root_path()
        archive_path = root / f".sandbox-runloop-hydrate-{self.state.session_id.hex}.tar"

        try:
            validate_tar_bytes(payload)
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root,
                context={
                    "reason": "unsafe_or_invalid_tar",
                    "member": e.member,
                    "detail": str(e),
                },
                cause=e,
            ) from e

        try:
            await self.mkdir(root, parents=True)
            await self.write(archive_path, io.BytesIO(payload))
            result = await self.exec(
                "tar",
                "-C",
                root.as_posix(),
                "-xf",
                archive_path.as_posix(),
                shell=False,
                timeout=self.state.timeouts.snapshot_s,
            )
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "reason": "tar_extract_failed",
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e
        finally:
            try:
                await self.exec(
                    "rm",
                    "-f",
                    "--",
                    archive_path.as_posix(),
                    shell=False,
                    timeout=self.state.timeouts.cleanup_s,
                )
            except Exception:
                pass


def _runloop_create_kwargs(
    *,
    blueprint_id: str | None,
    blueprint_name: str | None,
    env_vars: dict[str, str] | None,
    name: str | None,
    user_parameters: RunloopUserParameters | None,
    launch_parameters: RunloopLaunchParameters | None,
    tunnel: RunloopTunnelConfig | None,
    gateways: dict[str, RunloopGatewaySpec],
    mcp: dict[str, RunloopMcpSpec],
    metadata: dict[str, str],
    secrets: dict[str, str],
) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if blueprint_id is not None:
        kwargs["blueprint_id"] = blueprint_id
    if blueprint_name is not None:
        kwargs["blueprint_name"] = blueprint_name
    if env_vars:
        kwargs["environment_variables"] = env_vars
    if name:
        kwargs["name"] = name
    launch_parameters_payload = _runloop_launch_parameters_payload(
        launch_parameters=launch_parameters,
        user_parameters=user_parameters,
    )
    if launch_parameters_payload is not None:
        kwargs["launch_parameters"] = launch_parameters_payload
    if tunnel is not None:
        kwargs["tunnel"] = tunnel.model_dump(mode="json", exclude_none=True)
    if gateways:
        kwargs["gateways"] = {
            key: value.model_dump(mode="json", exclude_none=True) for key, value in gateways.items()
        }
    if mcp:
        kwargs["mcp"] = {
            key: value.model_dump(mode="json", exclude_none=True) for key, value in mcp.items()
        }
    if metadata:
        kwargs["metadata"] = metadata
    if secrets:
        kwargs["secrets"] = secrets
    return kwargs


def _runloop_launch_parameters_payload(
    *,
    launch_parameters: RunloopLaunchParameters | None,
    user_parameters: RunloopUserParameters | None,
) -> dict[str, object] | None:
    payload = (
        launch_parameters.to_dict(mode="json", exclude_none=True, exclude_defaults=True)
        if launch_parameters is not None
        else {}
    )
    if user_parameters is not None:
        payload["user_parameters"] = user_parameters.to_dict(mode="json", exclude_none=True)
    return payload or None


async def _upsert_runloop_managed_secrets(
    sdk: Any,
    *,
    managed_secrets: dict[str, str] | None,
    timeout_s: float,
) -> dict[str, str]:
    if not managed_secrets:
        return {}

    secret_refs: dict[str, str] = {}
    for env_var, secret_value in sorted(managed_secrets.items()):
        try:
            await sdk.secret.create(name=env_var, value=secret_value, timeout=timeout_s)
        except Exception as e:
            if _is_runloop_conflict(e):
                await sdk.secret.update(env_var, value=secret_value, timeout=timeout_s)
            else:
                raise
        secret_refs[env_var] = env_var
    return secret_refs


def _effective_runloop_home(user_parameters: RunloopUserParameters | None) -> PurePosixPath:
    if user_parameters is None:
        return _RUNLOOP_DEFAULT_HOME
    if user_parameters.username == "root" and user_parameters.uid == 0:
        return _RUNLOOP_ROOT_HOME
    return PurePosixPath("/home") / user_parameters.username


def _default_runloop_manifest_root(user_parameters: RunloopUserParameters | None) -> str:
    return str(_effective_runloop_home(user_parameters))


def _validate_runloop_manifest_root(
    manifest: Manifest, *, user_parameters: RunloopUserParameters | None
) -> None:
    root = PurePosixPath(posixpath.normpath(manifest.root))
    runloop_home = _effective_runloop_home(user_parameters)
    try:
        root.relative_to(runloop_home)
    except ValueError as e:
        raise ValueError(
            "RunloopSandboxClient requires manifest.root to be the effective Runloop home "
            f"({runloop_home}) or a subdirectory of it."
        ) from e


class RunloopSandboxClient(BaseSandboxClient[RunloopSandboxClientOptions | None]):
    """Runloop sandbox client managing devbox lifecycle via AsyncRunloopSDK."""

    backend_id = "runloop"
    supports_default_options = True
    _instrumentation: Instrumentation
    _platform: RunloopPlatformClient

    def __init__(
        self,
        *,
        bearer_token: str | None = None,
        base_url: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._sdk = _import_runloop_sdk().async_sdk(bearer_token=bearer_token, base_url=base_url)
        self._platform = RunloopPlatformClient(self._sdk)
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    @property
    def platform(self) -> RunloopPlatformClient:
        return self._platform

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: RunloopSandboxClientOptions | None,
    ) -> SandboxSession:
        """Create a Runloop devbox and bind it to a manifest rooted under the active home.

        Runloop defaults to the `user` account at `/home/user`, but explicit user parameters can
        switch the active home, including root launch at `/root`. Client creation validates the
        manifest root against that effective home, merges environment variables, and applies any
        configured blueprint selection or user profile when provisioning the devbox. The returned
        session follows the shared sandbox lifecycle and must be started before direct operations.
        """
        resolved_options = options or RunloopSandboxClientOptions()
        if (
            resolved_options.blueprint_id is not None
            and resolved_options.blueprint_name is not None
        ):
            raise ValueError(
                "RunloopSandboxClientOptions cannot set both blueprint_id and blueprint_name"
            )

        user_parameters = _normalize_runloop_user_parameters(resolved_options.user_parameters)
        manifest = manifest or Manifest(root=_default_runloop_manifest_root(user_parameters))
        _validate_runloop_manifest_root(manifest, user_parameters=user_parameters)

        timeouts_in = resolved_options.timeouts
        if isinstance(timeouts_in, RunloopTimeouts):
            timeouts = timeouts_in
        elif timeouts_in is None:
            timeouts = RunloopTimeouts()
        else:
            timeouts = RunloopTimeouts.model_validate(timeouts_in)

        secret_refs = await _upsert_runloop_managed_secrets(
            self._sdk,
            managed_secrets=resolved_options.managed_secrets,
            timeout_s=timeouts.fast_op_s,
        )
        launch_parameters = _normalize_runloop_launch_parameters(resolved_options.launch_parameters)
        tunnel = _normalize_runloop_tunnel_config(resolved_options.tunnel)
        base_envs = dict(resolved_options.env_vars or {})
        manifest_envs = await manifest.environment.resolve()
        envs = {**base_envs, **manifest_envs} or None

        create_kwargs = _runloop_create_kwargs(
            blueprint_id=resolved_options.blueprint_id,
            blueprint_name=resolved_options.blueprint_name,
            env_vars=envs,
            name=resolved_options.name,
            user_parameters=user_parameters,
            launch_parameters=launch_parameters,
            tunnel=tunnel,
            gateways=dict(resolved_options.gateways or {}),
            mcp=dict(resolved_options.mcp or {}),
            metadata=dict(resolved_options.metadata or {}),
            secrets=secret_refs,
        )
        devbox = await self._sdk.devbox.create(timeout=timeouts.create_s, **create_kwargs)

        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = RunloopSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            devbox_id=devbox.id,
            blueprint_id=resolved_options.blueprint_id,
            blueprint_name=resolved_options.blueprint_name,
            base_env_vars=base_envs,
            pause_on_exit=resolved_options.pause_on_exit,
            name=resolved_options.name,
            timeouts=timeouts,
            exposed_ports=resolved_options.exposed_ports,
            user_parameters=user_parameters,
            launch_parameters=launch_parameters,
            tunnel=tunnel,
            gateways=dict(resolved_options.gateways or {}),
            mcp=dict(resolved_options.mcp or {}),
            metadata=dict(resolved_options.metadata or {}),
            secret_refs=secret_refs,
        )
        inner = RunloopSandboxSession.from_state(state, sdk=self._sdk, devbox=devbox)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def close(self) -> None:
        """Close the shared AsyncRunloopSDK client used for devbox operations."""
        await self._sdk.aclose()

    async def __aenter__(self) -> RunloopSandboxClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def delete(self, session: SandboxSession) -> SandboxSession:
        """Best-effort release the Runloop devbox when callers delete the session."""
        inner = session._inner
        if not isinstance(inner, RunloopSandboxSession):
            raise TypeError("RunloopSandboxClient.delete expects a RunloopSandboxSession")
        try:
            await inner.shutdown()
        except Exception:
            pass
        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        """Resume a persisted Runloop session by reconnecting or reprovisioning a devbox.

        The client first tries to reconnect to the stored devbox id, including after an unclean
        process/client shutdown where the devbox is still running and `shutdown()` was never
        called. If reconnect fails, it creates a fresh devbox with the stored blueprint and
        environment settings.
        """
        if not isinstance(state, RunloopSandboxSessionState):
            raise TypeError("RunloopSandboxClient.resume expects a RunloopSandboxSessionState")

        devbox = None
        reconnected = False
        try:
            devbox = self._sdk.devbox.from_id(state.devbox_id)
            info: RunloopDevboxView = await devbox.get_info(timeout=state.timeouts.keepalive_s)
            status = info.status
            resume_polling_config = _runloop_polling_config(timeout_s=state.timeouts.resume_s)
            if status == "suspended":
                await devbox.resume(timeout=state.timeouts.resume_s)
                await devbox.await_running(polling_config=resume_polling_config)
            elif status == "resuming":
                await devbox.await_running(polling_config=resume_polling_config)
            elif status != "running":
                raise RuntimeError(f"unexpected_status:{status}")
            reconnected = True
        except Exception:
            devbox = None

        if devbox is None:
            manifest_envs = await state.manifest.environment.resolve()
            envs = {**state.base_env_vars, **manifest_envs} or None
            create_kwargs = _runloop_create_kwargs(
                blueprint_id=state.blueprint_id,
                blueprint_name=state.blueprint_name,
                env_vars=envs,
                name=state.name,
                user_parameters=state.user_parameters,
                launch_parameters=state.launch_parameters,
                tunnel=state.tunnel,
                gateways=state.gateways,
                mcp=state.mcp,
                metadata=state.metadata,
                secrets=state.secret_refs,
            )
            devbox = await self._sdk.devbox.create(timeout=state.timeouts.create_s, **create_kwargs)
            state.devbox_id = devbox.id

        inner = RunloopSandboxSession.from_state(state, sdk=self._sdk, devbox=devbox)
        inner._skip_start = state.pause_on_exit and reconnected
        inner._set_start_state_preserved(reconnected, system=reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return RunloopSandboxSessionState.model_validate(payload)
