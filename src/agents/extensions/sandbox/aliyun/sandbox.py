"""
Aliyun (AgentRun) sandbox implementation.

This module provides an Aliyun-backed sandbox client/session implementation backed by
`agentrun-sdk`'s sandbox classes.

Structure mirrors `agents.extensions.sandbox.vercel.sandbox` so that callers can use the
same lifecycle (`create` / `resume` / `delete` / `SandboxSession` context manager)
regardless of backend.

The `agentrun-sdk` dependency is optional, so package-level exports should guard imports of
this module. Within this module, AgentRun SDK imports are normal so users with the extra
installed get full type navigation.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import re
import shlex
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from agentrun.sandbox.client import SandboxClient
from agentrun.sandbox.code_interpreter_sandbox import CodeInterpreterSandbox
from agentrun.sandbox.model import NASConfig, OSSMountConfig, PolarFsConfig
from agentrun.utils.config import Config

from ....sandbox.errors import (
    ConfigurationError,
    ErrorCode,
    ExecNonZeroError,
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
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tarfile
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

logger = logging.getLogger(__name__)


DEFAULT_ALIYUN_WORKSPACE_ROOT = "/home/user"
DEFAULT_ALIYUN_REGION = "cn-hangzhou"
DEFAULT_ALIYUN_TEMPLATE_NAME = "sandbox-code-interpreter"
DEFAULT_ALIYUN_SANDBOX_TIMEOUT_S = 1800
DEFAULT_ALIYUN_WAIT_FOR_RUNNING_TIMEOUT_S = 45.0
# Poll interval while waiting for a freshly created sandbox to report healthy.
ALIYUN_HEALTH_POLL_INTERVAL_S = 1.0
# Default per-command timeout when a caller does not pass one. Matches the SDK's
# own `cmd_async` default; callers may request more and it is forwarded as-is.
DEFAULT_ALIYUN_EXEC_TIMEOUT_S = 30
# Transient gateway 502s on /processes/cmd — retry twice with backoff before surfacing.
ALIYUN_GATEWAY_502_RETRIES = 2
ALIYUN_GATEWAY_502_BACKOFF_BASE_S = 0.5
# /filesystem/upload responds 413 above this size.
ALIYUN_FILESYSTEM_UPLOAD_MAX_BYTES = 100 * 1024 * 1024

# AgentRun's cmd API has no env parameter, so env is inlined as a `KEY=val ...`
# prefix on each command. An unsafe key would be shell injection, so only allow
# POSIX-ish identifier names; values are `shlex.quote`d.
_VALID_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _resolve_manifest_root(manifest: Manifest | None) -> Manifest:
    """Default manifest root to AgentRun's `/home/user` when unset."""
    if manifest is None:
        return Manifest(root=DEFAULT_ALIYUN_WORKSPACE_ROOT)
    if manifest.root == Manifest.model_fields["root"].default:
        return manifest.model_copy(update={"root": DEFAULT_ALIYUN_WORKSPACE_ROOT})
    return manifest


def _build_config(
    *,
    access_key_id: str | None,
    access_key_secret: str | None,
    account_id: str | None,
    api_key: str | None,
    region: str,
) -> Config:
    """Build an agentrun Config from option-provided credentials.

    `agentrun.utils.config.Config` falls back to environment variables and the
    Alibaba Cloud credential providers when keyword arguments are `None`, so we
    pass values through verbatim.
    """
    headers: dict[str, str] = {}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    return Config(
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        account_id=account_id,
        region_id=region,
        headers=headers or None,
    )


def _is_not_found_error(exc: BaseException) -> bool:
    """Recognize AgentRun's 'file missing' signals across the SDK surface.

    Preferred: the SDK's ``HTTPError`` carries an explicit ``status_code`` (404).
    Fallback: substring sniff for cases where the SDK wraps the error differently
    or returns the raw gateway message.
    """
    status_code = getattr(exc, "status_code", None)
    if status_code == 404:
        return True
    msg = str(exc).lower()
    return "not found" in msg or "no such file" in msg or "does not exist" in msg


def _is_retryable_gateway(exc: BaseException) -> bool:
    """True when AgentRun's data-plane gateway returned HTTP 502 Bad Gateway."""
    retry_codes = (502,)
    status_code = getattr(exc, "status_code", None)
    if status_code in retry_codes:
        return True
    msg = str(exc).lower()
    return "http 502" in msg or "502: bad gateway" in msg


def _health_is_ready(health: Any) -> bool:
    """Interpret an AgentRun health payload as ready / not-ready.

    The documented ``check_health`` response is a dict with a ``status`` field
    (``"ok"`` when ready); we also accept a few synonyms defensively. A non-dict,
    non-``None`` return is treated as ready so an SDK that changes the shape does
    not wedge startup.
    """
    if isinstance(health, dict):
        return str(health.get("status", "") or "").lower() in {"ok", "healthy", "ready", "running"}
    return health is not None


class AliyunSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Aliyun (AgentRun) sandbox backend.

    Credentials default to `None`; when unset they fall through to whatever the
    underlying `agentrun-sdk` resolves from environment variables / Alibaba Cloud
    credential providers (e.g. `ALIBABA_CLOUD_ACCESS_KEY_ID`).

    Optional mount configs (``oss_mount_config`` / ``nas_config`` / ``polar_fs_config``)
    are passed straight through to ``SandboxClient.create_sandbox_async``. The caller
    is responsible for shaping the per-session bucket path / mount point — this class
    does not know about tenancy or workspace layout.
    """

    type: Literal["aliyun"] = "aliyun"
    access_key_id: str | None = None
    access_key_secret: str | None = None
    account_id: str | None = None
    api_key: str | None = None
    region: str | None = None
    template_name: str | None = None
    sandbox_idle_timeout_seconds: int | None = DEFAULT_ALIYUN_SANDBOX_TIMEOUT_S
    env: dict[str, str] | None = None
    exposed_ports: tuple[int, ...] = ()
    oss_mount_config: OSSMountConfig | None = None
    nas_config: NASConfig | None = None
    polar_fs_config: PolarFsConfig | None = None

    def __init__(
        self,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        account_id: str | None = None,
        api_key: str | None = None,
        region: str | None = None,
        template_name: str | None = None,
        sandbox_idle_timeout_seconds: int | None = DEFAULT_ALIYUN_SANDBOX_TIMEOUT_S,
        env: dict[str, str] | None = None,
        exposed_ports: tuple[int, ...] = (),
        oss_mount_config: OSSMountConfig | None = None,
        nas_config: NASConfig | None = None,
        polar_fs_config: PolarFsConfig | None = None,
        *,
        type: Literal["aliyun"] = "aliyun",
    ) -> None:
        super().__init__(
            type=type,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            account_id=account_id,
            api_key=api_key,
            region=region,
            template_name=template_name,
            sandbox_idle_timeout_seconds=sandbox_idle_timeout_seconds,
            env=env,
            exposed_ports=exposed_ports,
            oss_mount_config=oss_mount_config,
            nas_config=nas_config,
            polar_fs_config=polar_fs_config,
        )


class AliyunSandboxSessionState(SandboxSessionState):
    """Serializable state for an Aliyun-backed session.

    Credentials are intentionally not persisted here; they are stored on the
    :class:`AliyunSandboxClient` and re-injected as an agentrun ``Config`` on the
    :class:`AliyunSandboxSession` at create/resume time so that serialized session
    state never carries access keys or API tokens.
    """

    type: Literal["aliyun"] = "aliyun"
    sandbox_id: str = ""
    region: str = DEFAULT_ALIYUN_REGION
    template_name: str | None = None
    sandbox_idle_timeout_seconds: int | None = None
    env: dict[str, str] | None = None
    oss_mount_config: OSSMountConfig | None = None
    nas_config: NASConfig | None = None
    polar_fs_config: PolarFsConfig | None = None


class AliyunSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by an AgentRun sandbox."""

    state: AliyunSandboxSessionState
    _sandbox: Any | None
    _client: Any | None
    _config: Any | None
    _env_prefix: str | None

    def __init__(
        self,
        *,
        state: AliyunSandboxSessionState,
        sandbox: Any | None = None,
        client: Any | None = None,
        config: Any | None = None,
    ) -> None:
        self.state = state
        self._sandbox = sandbox
        self._client = client
        self._config = config
        # Cached `KEY=val ...` shell prefix built from options + manifest env, applied
        # to every command (AgentRun's cmd API has no env parameter). Resolved lazily
        # on first use; `None` means "not resolved yet".
        self._env_prefix = None

    @classmethod
    def from_state(
        cls,
        state: AliyunSandboxSessionState,
        *,
        sandbox: Any | None = None,
        client: Any | None = None,
        config: Any | None = None,
    ) -> AliyunSandboxSession:
        return cls(state=state, sandbox=sandbox, client=client, config=config)

    def supports_pty(self) -> bool:
        return False

    def _reject_user_arg(self, *, op: Literal["exec", "read", "write"], user: str | User) -> None:
        user_name = user.name if isinstance(user, User) else user
        raise ConfigurationError(
            message=(
                "AliyunSandboxSession does not support sandbox-local users; "
                f"`{op}` must be called without `user`"
            ),
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op=op,
            context={"backend": "aliyun", "user": user_name},
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
        """Wrap the shared tar validator to surface a `ValueError` for legacy callers."""
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
                # The archive is handed to a remote `tar xf`, so reject symlinks that
                # point outside the workspace root — otherwise the restored workspace
                # could contain links user commands follow off-root (as other hosted
                # backends do before remote extraction).
                validate_tarfile(tar, allow_external_symlink_targets=False)
        except UnsafeTarMemberError as exc:
            raise ValueError(str(exc)) from exc
        except (tarfile.TarError, OSError) as exc:
            raise ValueError("invalid tar stream") from exc

    # ------------------------------------------------------------------
    # AgentRun lifecycle
    # ------------------------------------------------------------------
    async def _prepare_backend_workspace(self) -> None:
        root = PurePosixPath(self.state.manifest.root)
        try:
            sandbox = await self._ensure_sandbox()
            # Run mkdir from `/` because `manifest.root` might not exist yet — this
            # call is what creates it. If we let `_run_command` fall back to the
            # workspace root as cwd, the AgentRun gateway rejects the command before
            # mkdir ever runs.
            exit_code, stdout, stderr = await self._run_command(
                sandbox, "mkdir", ["-p", "--", root.as_posix()], cwd="/"
            )
        except WorkspaceStartError:
            raise
        except Exception as exc:
            raise WorkspaceStartError(path=posix_path_as_path(root), cause=exc) from exc

        if exit_code != 0:
            raise WorkspaceStartError(
                path=posix_path_as_path(root),
                context={
                    "exit_code": exit_code,
                    "stdout": stdout,
                    "stderr": stderr,
                },
            )

    async def _env_command_prefix(self) -> str:
        """Build the `KEY=val ...` prefix that injects the configured environment.

        AgentRun's cmd API has no env parameter, so — like other backends inject env
        per exec — we prepend the resolved environment to every command. Options env
        is merged with the manifest environment (manifest wins on collisions), unsafe
        key names are dropped, and values are `shlex.quote`d. The result is cached on
        the session since the environment is static for its lifetime. Because env
        travels inline with each command, it needs no workspace file, so it is immune
        to snapshot restore, `.profile` login-shell semantics, and shell=False execs.
        """
        if self._env_prefix is not None:
            return self._env_prefix
        manifest_env = cast(dict[str, str | None], await self.state.manifest.environment.resolve())
        merged = {
            key: value
            for key, value in {**(self.state.env or {}), **manifest_env}.items()
            if value is not None
        }
        parts: list[str] = []
        for key, value in merged.items():
            if not _VALID_ENV_NAME.match(key):
                logger.warning("[AliyunSandboxSession] skipping env var with unsafe name %r", key)
                continue
            parts.append(f"{key}={shlex.quote(value)}")
        self._env_prefix = (" ".join(parts) + " ") if parts else ""
        return self._env_prefix

    async def _ensure_sandbox(self) -> Any:
        """Lazily provision the underlying AgentRun sandbox.

        Mirrors `VercelSandboxSession._ensure_sandbox`: if we already have a live
        backend handle, reuse it; otherwise create one using the state's template /
        idle-timeout settings.
        """
        sandbox = self._sandbox
        if sandbox is not None:
            return sandbox

        if self._config is None:
            self._config = _build_config(
                access_key_id=None,
                access_key_secret=None,
                account_id=None,
                api_key=None,
                region=self.state.region,
            )
        if self._client is None:
            self._client = SandboxClient(config=self._config)

        template_name = self.state.template_name or DEFAULT_ALIYUN_TEMPLATE_NAME
        idle_timeout = (
            self.state.sandbox_idle_timeout_seconds
            if self.state.sandbox_idle_timeout_seconds is not None
            else DEFAULT_ALIYUN_SANDBOX_TIMEOUT_S
        )
        try:
            base_sandbox = await self._client.create_sandbox_async(
                template_name=template_name,
                sandbox_idle_timeout_seconds=idle_timeout,
                oss_mount_config=self.state.oss_mount_config,
                nas_config=self.state.nas_config,
                polar_fs_config=self.state.polar_fs_config,
            )
        except Exception as exc:
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(self.state.manifest.root)),
                cause=exc,
            ) from exc

        sandbox = CodeInterpreterSandbox.model_validate(base_sandbox.model_dump(by_alias=False))
        sandbox._config = self._config

        self._sandbox = sandbox
        self.state.sandbox_id = sandbox.sandbox_id
        logger.info(
            "AliyunSandboxSession created sandbox: template=%s sandbox_id=%s",
            template_name,
            sandbox.sandbox_id,
        )
        # A freshly created sandbox may still be booting; the SDK's own
        # CodeInterpreterSandbox context manager polls health for this reason.
        # We bypass that context manager, so wait explicitly before the first
        # exec (e.g. the workspace mkdir) can hit a not-yet-ready sandbox. If it
        # never becomes healthy, delete the just-created sandbox before surfacing
        # the error — otherwise `create()` raises without handing back a session
        # and the remote sandbox would leak until its idle timeout.
        try:
            await self._wait_until_healthy(sandbox)
        except Exception:
            await self._stop_attached_sandbox()
            raise
        return sandbox

    async def _wait_until_healthy(self, sandbox: Any) -> None:
        """Poll the AgentRun health endpoint until the sandbox is ready.

        Mirrors the SDK's ``CodeInterpreterSandbox.__enter__``: check roughly once
        per second until the sandbox reports healthy, up to
        ``DEFAULT_ALIYUN_WAIT_FOR_RUNNING_TIMEOUT_S``. Raises ``WorkspaceStartError``
        on timeout so callers get a clean start failure instead of an opaque error
        from the first exec against a still-booting sandbox.
        """
        max_attempts = max(1, int(DEFAULT_ALIYUN_WAIT_FOR_RUNNING_TIMEOUT_S))
        last_error: BaseException | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                health = await sandbox.check_health_async()
            except Exception as exc:  # noqa: BLE001 — booting sandbox may reject probes.
                last_error = exc
            else:
                if _health_is_ready(health):
                    return
                last_error = None
            if attempt < max_attempts:
                await asyncio.sleep(ALIYUN_HEALTH_POLL_INTERVAL_S)
        raise WorkspaceStartError(
            path=posix_path_as_path(coerce_posix_path(self.state.manifest.root)),
            context={
                "backend": "aliyun",
                "reason": "sandbox_not_healthy",
                "sandbox_id": self.state.sandbox_id,
            },
            cause=last_error,
        )

    async def _run_command(
        self,
        sandbox: Any,
        command: str,
        args: list[str],
        *,
        cwd: str | None = None,
        timeout_s: float | None = None,
    ) -> tuple[int, str, str]:
        """Run a single command in the sandbox; return (exit_code, stdout, stderr).

        Thin wrapper so the rest of the session class doesn't need to know how the
        AgentRun SDK shapes its command responses. The configured environment is
        inlined as a `KEY=val ...` prefix; that prefix is never logged so secret
        values stay out of the logs.
        """
        argv = [command, *args]
        shell_cmd = shlex.join(argv)
        effective_cwd = cwd or self.state.manifest.root
        # Forward the caller's timeout as-is (defaulting to the SDK default) rather
        # than silently shortening it: the outer `wait_for` in `_exec_internal` uses
        # the same value, so a longer command a caller allowed time for isn't killed
        # early. If a specific AgentRun gateway enforces a shorter ceiling, that
        # surfaces as a provider error instead of a silent cap.
        if timeout_s is None:
            timeout_int = DEFAULT_ALIYUN_EXEC_TIMEOUT_S
        else:
            # Round up: the SDK wants an int, and truncating a budget like 1.9 -> 1
            # would let the provider kill the command before the outer wait_for.
            timeout_int = max(1, math.ceil(timeout_s))

        # Log the bare command only — the env prefix may contain secret values.
        logger.info(
            "AliyunSandboxSession._run_command sandbox_id=%s cwd=%s timeout=%ss cmd=%r",
            self.state.sandbox_id,
            effective_cwd,
            timeout_int,
            shell_cmd[:500],
        )
        # The resolve-workspace-path helper is on the critical sandbox-startup path,
        # and its caller treats exit0+empty-stdout as a fatal ExecTransportError.
        # AgentRun occasionally drops stdout on an otherwise-successful exec, so retry
        # that specific case within the same attempt budget. Gateway 502s are retried
        # with exponential backoff before surfacing as ExecTransportError.
        is_resolve_helper = "resolve-workspace-path" in command
        max_attempts = 1 + ALIYUN_GATEWAY_502_RETRIES

        # Inline the configured environment as a shell prefix (AgentRun's cmd API has
        # no env parameter). Applies to every command — shell=True and shell=False —
        # with no workspace file, so it survives snapshot restore and needs no login
        # shell. Empty when no env is configured.
        command_to_run = f"{await self._env_command_prefix()}{shell_cmd}"

        exit_code, stdout, stderr = 0, "", ""
        for attempt in range(1, max_attempts + 1):
            try:
                result = await sandbox.process.cmd_async(
                    command=command_to_run,
                    cwd=effective_cwd,
                    timeout=timeout_int,
                )
            except Exception as exc:
                if _is_retryable_gateway(exc) and attempt < max_attempts:
                    backoff_s = ALIYUN_GATEWAY_502_BACKOFF_BASE_S * (2 ** (attempt - 1))
                    logger.warning(
                        "AliyunSandboxSession._run_command HTTP 502 "
                        "(attempt %d/%d); retrying in %.1fs sandbox_id=%s: %s",
                        attempt,
                        max_attempts,
                        backoff_s,
                        self.state.sandbox_id,
                        exc,
                    )
                    await asyncio.sleep(backoff_s)
                    continue
                raise
            logger.info(
                "AliyunSandboxSession._run_command sandbox_id=%s returned (cwd=%s)",
                self.state.sandbox_id,
                effective_cwd,
            )

            if isinstance(result, dict):
                inner = result.get("result", result)
                stdout = str(inner.get("stdout", "") or "")
                stderr = str(inner.get("stderr", "") or "")
                # Doc-mandated field name is `exitCode`; keep `exit_code` as a defensive
                # fallback in case the SDK or older gateway versions normalize it.
                exit_code = int(inner.get("exitCode", inner.get("exit_code", 0)) or 0)
            else:
                stdout = str(result or "")
                stderr = ""
                exit_code = 0

            if not (is_resolve_helper and exit_code == 0 and not stdout.strip()):
                break
            if attempt < max_attempts:
                logger.warning(
                    "AliyunSandboxSession._run_command resolve-workspace-path returned "
                    "exit0 with empty stdout (attempt %d/%d); retrying sandbox_id=%s",
                    attempt,
                    max_attempts,
                    self.state.sandbox_id,
                )
                await asyncio.sleep(0.2 * attempt)

        return exit_code, stdout, stderr

    async def _close_sandbox_client(self) -> None:
        # The agentrun-sdk's SandboxClient does not expose an explicit aclose hook —
        # its HTTP client is short-lived per request. Nothing to do.
        return

    async def running(self) -> bool:
        sandbox = self._sandbox
        if sandbox is None:
            return False
        try:
            # AgentRun exposes a cheap GET /sandboxes/{id}/health probe; prefer it
            # over a no-op shell command, which would consume a /processes/cmd slot
            # and pay the data-plane gateway round-trip.
            health = await sandbox.check_health_async()
        except Exception:  # noqa: BLE001 — treat any probe failure as not-running.
            return False
        return _health_is_ready(health)

    async def shutdown(self) -> None:
        await self._stop_attached_sandbox()

    async def _stop_attached_sandbox(self) -> None:
        sandbox = self._sandbox
        client = self._client
        sandbox_id = self.state.sandbox_id or (sandbox.sandbox_id if sandbox is not None else None)
        if sandbox is None:
            return
        try:
            if client is not None and sandbox_id:
                await client.delete_sandbox_async(sandbox_id=sandbox_id)
                logger.info("AliyunSandboxSession deleted sandbox: %s", sandbox_id)
            elif hasattr(sandbox, "delete_async"):
                await sandbox.delete_async()
                logger.info("AliyunSandboxSession deleted sandbox via handle: %s", sandbox_id)
        except Exception:  # noqa: BLE001 — teardown is best-effort.
            logger.exception("AliyunSandboxSession failed to delete sandbox %s", sandbox_id)
        finally:
            await self._close_sandbox_client()
            self._sandbox = None
            self._client = None

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------
    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        sandbox = await self._ensure_sandbox()
        normalized = [str(part) for part in command]
        if not normalized:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)

        try:
            exit_code, stdout, stderr = await asyncio.wait_for(
                self._run_command(
                    sandbox,
                    normalized[0],
                    normalized[1:],
                    cwd=self.state.manifest.root,
                    timeout_s=timeout,
                ),
                timeout=timeout,
            )
            return ExecResult(
                stdout=stdout.encode("utf-8"),
                stderr=stderr.encode("utf-8"),
                exit_code=exit_code,
            )
        except TimeoutError as exc:
            raise ExecTimeoutError(command=normalized, timeout_s=timeout, cause=exc) from exc
        except ExecTimeoutError:
            raise
        except Exception as exc:
            logger.exception(
                "AliyunSandboxSession _exec_internal failed sandbox_id=%s cmd=%s",
                self.state.sandbox_id,
                normalized,
            )
            raise ExecTransportError(
                command=normalized,
                context={"backend": "aliyun", "sandbox_id": self.state.sandbox_id},
                cause=exc,
            ) from exc

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        # AgentRun does not currently expose tunneled ports.
        raise ExposedPortUnavailableError(
            port=port,
            exposed_ports=self.state.exposed_ports,
            reason="backend_unavailable",
            context={"backend": "aliyun", "sandbox_id": self.state.sandbox_id},
        )

    # ------------------------------------------------------------------
    # File IO
    # ------------------------------------------------------------------
    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            self._reject_user_arg(op="read", user=user)

        normalized_path = await self._validate_path_access(path)
        sandbox = await self._ensure_sandbox()
        try:
            payload = await self._sandbox_read_file(sandbox, normalized_path)
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=normalized_path, cause=exc) from exc
        if payload is None:
            raise WorkspaceReadNotFoundError(path=normalized_path)
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
        try:
            await self._sandbox_write_file(
                await self._ensure_sandbox(),
                normalized_path,
                bytes(payload),
            )
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=exc) from exc

    async def append(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        """Append bytes to a file in the workspace, creating it if absent.

        Unlike :meth:`write` — which uploads a fresh file and therefore replaces
        whatever was there — ``append`` preserves the existing contents. It reads
        the current bytes (empty when the file does not exist yet), concatenates
        the new payload, and uploads the combined result. This lets a file such
        as ``outputs/evidence-map.csv`` accumulate rows across turns/conversations
        that each start with a fresh in-memory context but share the same
        OSS-backed workspace.

        This is a read-modify-write, not an atomic O_APPEND: concurrent appends
        to the same path can lose data. The per-thread sandbox pool serializes a
        thread's turns, so that is safe here; callers fanning out parallel
        appends to one path must coordinate externally.
        """
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

        sandbox = await self._ensure_sandbox()
        try:
            existing = await self._sandbox_read_file(sandbox, normalized_path)
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=normalized_path, cause=exc) from exc

        combined = (existing or b"") + bytes(payload)
        try:
            await self._sandbox_write_file(sandbox, normalized_path, combined)
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=exc) from exc

    async def _sandbox_read_file(self, sandbox: Any, path: Path) -> bytes | None:
        """Read a file off the sandbox via ``file_system.download_async``.

        The SDK's ``file.read`` API returns text and is lossy for binary payloads;
        ``file_system.download`` writes the raw bytes to a local path, so we round
        through a temp file to give callers an authoritative byte stream.
        """
        remote = sandbox_path_str(path)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="aliyun-read-")
        os.close(tmp_fd)
        try:
            try:
                await sandbox.file_system.download_async(path=remote, save_path=tmp_path)
            except FileNotFoundError:
                return None
            except Exception as exc:
                if _is_not_found_error(exc):
                    return None
                raise
            with open(tmp_path, "rb") as fh:
                return fh.read()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def _sandbox_write_file(self, sandbox: Any, path: Path, data: bytes) -> None:
        """Write bytes into the sandbox via ``file_system.upload_async``.

        The text-based ``file.write`` API truncates / mishandles non-utf8 payloads,
        so we always go through ``file_system.upload`` with a local temp file.
        """
        if len(data) > ALIYUN_FILESYSTEM_UPLOAD_MAX_BYTES:
            # /filesystem/upload returns 413 above 100MB; fail fast with a clear
            # message instead of letting the gateway error bubble up opaquely.
            raise WorkspaceArchiveWriteError(
                path=path,
                context={
                    "backend": "aliyun",
                    "reason": "upload_exceeds_100mb",
                    "size_bytes": len(data),
                    "limit_bytes": ALIYUN_FILESYSTEM_UPLOAD_MAX_BYTES,
                },
            )

        remote = sandbox_path_str(path)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix="aliyun-write-")
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(data)
            await sandbox.file_system.upload_async(
                local_file_path=tmp_path,
                target_file_path=remote,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Workspace persistence (tar-based; uses SDK's binary download/upload)
    # ------------------------------------------------------------------
    async def persist_workspace(self) -> io.IOBase:
        root = self._workspace_root_path()
        sandbox = await self._ensure_sandbox()
        archive_path = posix_path_as_path(
            coerce_posix_path(f"/tmp/openai-agents-aliyun-{self.state.session_id.hex}.tar")
        )
        excludes = [
            f"--exclude=./{rel_path.as_posix()}"
            for rel_path in sorted(
                self._persist_workspace_skip_relpaths(),
                key=lambda item: item.as_posix(),
            )
        ]
        tar_command = ("tar", "cf", archive_path.as_posix(), *excludes, ".")
        try:
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    cause=ExecNonZeroError(
                        result,
                        command=tar_command,
                        context={"backend": "aliyun", "sandbox_id": self.state.sandbox_id},
                    ),
                )
            archive = await self._sandbox_read_file(sandbox, archive_path)
            if archive is None:
                raise WorkspaceReadNotFoundError(path=archive_path)
            return io.BytesIO(archive)
        except WorkspaceReadNotFoundError:
            raise
        except WorkspaceArchiveReadError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=root, cause=exc) from exc
        finally:
            try:
                await self._run_command(
                    sandbox,
                    "rm",
                    [archive_path.as_posix()],
                    cwd=self.state.manifest.root,
                )
            except Exception:  # noqa: BLE001 — cleanup is best-effort.
                pass

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes | bytearray):
            raise WorkspaceWriteTypeError(
                path=self._workspace_root_path(),
                actual_type=type(raw).__name__,
            )

        raw_bytes = bytes(raw)
        root = self._workspace_root_path()
        sandbox = await self._ensure_sandbox()
        archive_path = posix_path_as_path(
            coerce_posix_path(f"/tmp/openai-agents-aliyun-{self.state.session_id.hex}.tar")
        )
        tar_command = ("tar", "xf", archive_path.as_posix(), "-C", root.as_posix())
        try:
            self._validate_tar_bytes(raw_bytes)
            await self.mkdir(root, parents=True)
            await self._sandbox_write_file(sandbox, archive_path, raw_bytes)
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    cause=ExecNonZeroError(
                        result,
                        command=tar_command,
                        context={"backend": "aliyun", "sandbox_id": self.state.sandbox_id},
                    ),
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=root, cause=exc) from exc
        finally:
            try:
                await self._run_command(
                    sandbox,
                    "rm",
                    [archive_path.as_posix()],
                    cwd=self.state.manifest.root,
                )
            except Exception:  # noqa: BLE001 — cleanup is best-effort.
                pass


class AliyunSandboxSessionWrapper(SandboxSession):
    """SDK wrapper that also exposes Aliyun-specific workspace helpers."""

    async def append(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        await self._inner.append(path, data, user=user)  # type: ignore[attr-defined]


class AliyunSandboxClient(BaseSandboxClient[AliyunSandboxClientOptions]):
    """Aliyun-backed sandbox client. Wraps `agentrun.sandbox.client.SandboxClient`."""

    backend_id = "aliyun"
    _instrumentation: Instrumentation
    _access_key_id: str | None
    _access_key_secret: str | None
    _account_id: str | None
    _api_key: str | None
    _region: str | None

    def __init__(
        self,
        *,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        account_id: str | None = None,
        api_key: str | None = None,
        region: str | None = None,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._account_id = account_id
        self._api_key = api_key
        self._region = region
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    def _resolve_credential(
        self,
        from_options: str | None,
        from_client: str | None,
    ) -> str | None:
        return from_options if from_options is not None else from_client

    def _wrap_session(
        self,
        inner: BaseSandboxSession,
        *,
        instrumentation: Instrumentation | None = None,
    ) -> SandboxSession:
        return AliyunSandboxSessionWrapper(
            inner,
            instrumentation=instrumentation,
            dependencies=self._resolve_dependencies(),
        )

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: AliyunSandboxClientOptions,
    ) -> SandboxSession:
        resolved_manifest = _resolve_manifest_root(manifest)
        region = self._resolve_credential(options.region, self._region) or DEFAULT_ALIYUN_REGION
        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = AliyunSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            sandbox_id="",
            region=region,
            template_name=options.template_name,
            sandbox_idle_timeout_seconds=options.sandbox_idle_timeout_seconds,
            env=dict(options.env or {}) or None,
            exposed_ports=options.exposed_ports,
            oss_mount_config=options.oss_mount_config,
            nas_config=options.nas_config,
            polar_fs_config=options.polar_fs_config,
        )
        # Resolve option-over-client credentials for THIS session's Config only. We
        # deliberately do not cache them back onto the client: the client may be
        # reused across tenants/accounts via per-call options, and mutating shared
        # state would let a later resume() rebuild Config under the wrong account.
        # resume() therefore relies on client-level credentials (or the SDK's env /
        # credential providers), since serialized state never carries secrets.
        config = _build_config(
            access_key_id=self._resolve_credential(options.access_key_id, self._access_key_id),
            access_key_secret=self._resolve_credential(
                options.access_key_secret, self._access_key_secret
            ),
            account_id=self._resolve_credential(options.account_id, self._account_id),
            api_key=self._resolve_credential(options.api_key, self._api_key),
            region=region,
        )
        inner = AliyunSandboxSession.from_state(state, config=config)
        # Eagerly bring up the underlying remote sandbox so callers get an
        # error here instead of on the first exec call.
        await inner._ensure_sandbox()
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, AliyunSandboxSession):
            raise TypeError("AliyunSandboxClient.delete expects an AliyunSandboxSession")
        try:
            await inner.shutdown()
        except Exception:  # noqa: BLE001 — delete is best-effort teardown.
            logger.exception("[AliyunSandboxClient.delete] shutdown failed")
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, AliyunSandboxSessionState):
            raise TypeError("AliyunSandboxClient.resume expects an AliyunSandboxSessionState")

        config = _build_config(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            account_id=self._account_id,
            api_key=self._api_key,
            region=state.region,
        )
        sandbox: Any | None = None
        client: Any | None = None
        reconnected = False
        if state.sandbox_id:
            try:
                sandbox, client = await self._reattach_sandbox(state.sandbox_id, config)
                reconnected = sandbox is not None
            except Exception:  # noqa: BLE001 — fall back to a fresh sandbox on any error.
                sandbox = None
                client = None

        inner = AliyunSandboxSession.from_state(
            state, sandbox=sandbox, client=client, config=config
        )
        if sandbox is None:
            # AgentRun sandboxes are not always re-addressable by id once the
            # client process has exited, so resume provisions a fresh sandbox and
            # relies on the snapshot/manifest pipeline to repopulate the workspace.
            state.workspace_root_ready = False
            await inner._ensure_sandbox()
        inner._set_start_state_preserved(reconnected)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def _reattach_sandbox(
        self, sandbox_id: str, config: Config
    ) -> tuple[Any | None, Any | None]:
        """Try to GET an existing AgentRun sandbox by id.

        Returns ``(sandbox, client)`` on success and ``(None, None)`` when the
        sandbox is gone (404), unhealthy, or the underlying SDK call fails. Errors
        are swallowed so ``resume`` can fall back to creating a fresh sandbox and
        rehydrating from the snapshot.
        """
        client = SandboxClient(config=config)
        try:
            base_sandbox = await client.get_sandbox_async(sandbox_id=sandbox_id)
        except Exception as exc:
            if _is_not_found_error(exc):
                return None, None
            raise

        sandbox = CodeInterpreterSandbox.model_validate(base_sandbox.model_dump(by_alias=False))
        sandbox._config = config
        # `get_sandbox_async` can return a record for an expired/stopped instance
        # instead of a 404. Reusing it would make the next `start()` fail on the
        # workspace probe/mkdir; verify it is actually healthy, else fall back to a
        # fresh sandbox so the snapshot restore path runs.
        try:
            healthy = _health_is_ready(await sandbox.check_health_async())
        except Exception:  # noqa: BLE001 — an unreachable sandbox is not reusable.
            healthy = False
        if not healthy:
            logger.info(
                "AliyunSandboxClient reattach found unhealthy sandbox %s; provisioning fresh",
                sandbox_id,
            )
            return None, None
        logger.info("AliyunSandboxClient reattached sandbox: sandbox_id=%s", sandbox_id)
        return sandbox, client

    def serialize_session_state(self, state: SandboxSessionState) -> dict[str, object]:
        # The agentrun-sdk's mount config models (OSSMountConfig / NASConfig /
        # PolarFsConfig) serialize to camelCase (`mountPoints`) but only validate
        # from snake_case (`mount_points`) because `validate_by_alias=False`. Dump
        # the state with `by_alias=False` so the persisted JSON round-trips through
        # `deserialize_session_state` cleanly. The SDK still emits the camelCase
        # wire form when it actually sends to the data plane.
        return state.model_dump(mode="json", by_alias=False)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return AliyunSandboxSessionState.model_validate(payload)


__all__ = [
    "AliyunSandboxClient",
    "AliyunSandboxClientOptions",
    "AliyunSandboxSession",
    "AliyunSandboxSessionState",
    "DEFAULT_ALIYUN_WORKSPACE_ROOT",
]
