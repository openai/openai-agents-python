"""
Aliyun AgentRun sandbox (https://www.alibabacloud.com/help/en/agentrun) implementation.

This module provides an Aliyun-backed sandbox client/session implementation backed by
the `agentrun-sdk` package (specifically
`agentrun.sandbox.code_interpreter_sandbox.CodeInterpreterSandbox`).

The `agentrun-sdk` dependency is optional, so package-level exports should guard imports of
this module. Within this module, AgentRun SDK imports are normal so users with the extra
installed get full type navigation.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shlex
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from agentrun.sandbox.client import SandboxClient
from agentrun.sandbox.code_interpreter_sandbox import CodeInterpreterSandbox
from agentrun.utils.config import Config

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
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, ExposedPortEndpoint, User
from ....sandbox.util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_has_status_code,
    retry_async,
)
from ....sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
from ....sandbox.workspace_paths import coerce_posix_path, posix_path_as_path, sandbox_path_str

logger = logging.getLogger(__name__)


DEFAULT_ALIYUN_WORKSPACE_ROOT = "/home/user"
DEFAULT_ALIYUN_EXEC_TIMEOUT_S = 120
DEFAULT_ALIYUN_REGION = "cn-hangzhou"
DEFAULT_ALIYUN_TEMPLATE_NAME = "code-interpreter"
DEFAULT_ALIYUN_SANDBOX_IDLE_TIMEOUT_S = 3600
DEFAULT_ALIYUN_WAIT_FOR_RUNNING_TIMEOUT_S = 45.0

_DEFAULT_MANIFEST_ROOT = cast(str, Manifest.model_fields["root"].default)
_AGENTS_ENV_FILE = "$HOME/.openai_agents_env"
_AGENTS_ENV_MARKER = "# >>> openai-agents env >>>"


def _resolve_manifest_root(manifest: Manifest | None) -> Manifest:
    """Default manifest root to AgentRun's `/home/user` when unset."""
    if manifest is None:
        return Manifest(root=DEFAULT_ALIYUN_WORKSPACE_ROOT)
    if manifest.root == _DEFAULT_MANIFEST_ROOT:
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


def _is_transient_create_error(exc: BaseException) -> bool:
    return exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)


@retry_async(retry_if=lambda exc, **_kwargs: _is_transient_create_error(exc))
async def _create_sandbox_with_retry(
    *,
    config: Config,
    template_name: str,
    sandbox_idle_timeout_seconds: int,
) -> CodeInterpreterSandbox:
    sandbox_client = SandboxClient(config=config)
    sandbox = await asyncio.to_thread(
        sandbox_client.create_sandbox,
        template_name=template_name,
        sandbox_idle_timeout_seconds=sandbox_idle_timeout_seconds,
    )
    return cast(CodeInterpreterSandbox, sandbox)


async def _run_shell(
    sandbox: CodeInterpreterSandbox,
    *,
    command: str,
    cwd: str,
    timeout: int,
) -> dict[str, Any]:
    """Run a shell command via `sandbox.process.cmd` and normalize the result.

    Returns a dict with keys `success`, `stdout`, `stderr`, `exit_code`, `error`
    so callers can branch uniformly on transient failures vs. command failures.
    """
    try:
        raw = await asyncio.to_thread(
            sandbox.process.cmd,
            command=command,
            cwd=cwd,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — surface as a normalized error dict
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
            "error": str(exc),
        }

    if isinstance(raw, dict):
        inner = raw.get("result", raw)
        stdout = str(inner.get("stdout", "") or "")
        stderr = str(inner.get("stderr", "") or "")
        exit_code = int(inner.get("exitCode", inner.get("exit_code", 0)) or 0)
    else:
        stdout = str(raw)
        stderr = ""
        exit_code = 0

    return {
        "success": exit_code == 0,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "error": None,
    }


async def _upload_bytes(
    sandbox: CodeInterpreterSandbox,
    *,
    content: bytes,
    remote_path: str,
) -> bool:
    """Upload raw bytes to `remote_path` via the AgentRun file-system API."""
    fd, tmp_path = tempfile.mkstemp(prefix="openai-agents-aliyun-upload-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        await asyncio.to_thread(
            sandbox.file_system.upload,
            local_file_path=tmp_path,
            target_file_path=remote_path,
        )
        return True
    except Exception:  # noqa: BLE001 — surface as False, caller wraps in SDK error
        logger.exception("[AliyunSandboxSession] file upload failed for %s", remote_path)
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _download_bytes(
    sandbox: CodeInterpreterSandbox,
    *,
    remote_path: str,
) -> bytes | None:
    """Download `remote_path` via the AgentRun file-system API."""
    fd, tmp_path = tempfile.mkstemp(prefix="openai-agents-aliyun-download-")
    os.close(fd)
    try:
        await asyncio.to_thread(
            sandbox.file_system.download,
            path=remote_path,
            save_path=tmp_path,
        )
        with open(tmp_path, "rb") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001 — surface as None, caller wraps in SDK error
        logger.exception("[AliyunSandboxSession] file download failed for %s", remote_path)
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


class AliyunSandboxClientOptions(BaseSandboxClientOptions):
    """Client options for the Aliyun AgentRun sandbox backend.

    Credentials default to `None`; when unset they fall through to whatever the
    underlying `agentrun-sdk` resolves from environment variables / Alibaba Cloud
    credential providers (e.g. `ALIBABA_CLOUD_ACCESS_KEY_ID`).
    """

    type: Literal["aliyun"] = "aliyun"
    access_key_id: str | None = None
    access_key_secret: str | None = None
    account_id: str | None = None
    api_key: str | None = None
    region: str = DEFAULT_ALIYUN_REGION
    template_name: str = DEFAULT_ALIYUN_TEMPLATE_NAME
    sandbox_idle_timeout_seconds: int = DEFAULT_ALIYUN_SANDBOX_IDLE_TIMEOUT_S
    default_cwd: str | None = None
    env: dict[str, str] | None = None
    exec_timeout_s: int = DEFAULT_ALIYUN_EXEC_TIMEOUT_S

    def __init__(
        self,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        account_id: str | None = None,
        api_key: str | None = None,
        region: str = DEFAULT_ALIYUN_REGION,
        template_name: str = DEFAULT_ALIYUN_TEMPLATE_NAME,
        sandbox_idle_timeout_seconds: int = DEFAULT_ALIYUN_SANDBOX_IDLE_TIMEOUT_S,
        default_cwd: str | None = None,
        env: dict[str, str] | None = None,
        exec_timeout_s: int = DEFAULT_ALIYUN_EXEC_TIMEOUT_S,
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
            default_cwd=default_cwd,
            env=env,
            exec_timeout_s=exec_timeout_s,
        )


class AliyunSandboxSessionState(SandboxSessionState):
    """Serializable state for an Aliyun-backed session.

    Credentials are intentionally not persisted here; they are stored on the
    :class:`AliyunSandboxSession` instance and re-injected by the
    :class:`AliyunSandboxClient` on resume so that serialized session state
    never carries access keys or API tokens.
    """

    type: Literal["aliyun"] = "aliyun"
    sandbox_id: str = ""
    region: str = DEFAULT_ALIYUN_REGION
    template_name: str = DEFAULT_ALIYUN_TEMPLATE_NAME
    sandbox_idle_timeout_seconds: int = DEFAULT_ALIYUN_SANDBOX_IDLE_TIMEOUT_S
    default_cwd: str | None = None
    env: dict[str, str] | None = None
    exec_timeout_s: int = DEFAULT_ALIYUN_EXEC_TIMEOUT_S


class AliyunSandboxSession(BaseSandboxSession):
    """SandboxSession implementation backed by an AgentRun `CodeInterpreterSandbox`."""

    state: AliyunSandboxSessionState
    _sandbox: CodeInterpreterSandbox | None
    _sandbox_client: SandboxClient | None
    _owned: bool
    _access_key_id: str | None
    _access_key_secret: str | None
    _account_id: str | None
    _api_key: str | None

    def __init__(
        self,
        *,
        state: AliyunSandboxSessionState,
        sandbox: CodeInterpreterSandbox | None = None,
        sandbox_client: SandboxClient | None = None,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        account_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.state = state
        self._sandbox = sandbox
        self._sandbox_client = sandbox_client
        # Credentials live on the session instance (not in state) so they
        # never get serialized into the persisted SandboxSessionState payload.
        self._access_key_id = access_key_id
        self._access_key_secret = access_key_secret
        self._account_id = account_id
        self._api_key = api_key
        # We own the remote sandbox iff we created it ourselves; sessions
        # constructed with an externally-provided sandbox leave teardown to the
        # caller.
        self._owned = sandbox is None

    @classmethod
    def from_state(
        cls,
        state: AliyunSandboxSessionState,
        *,
        sandbox: CodeInterpreterSandbox | None = None,
        sandbox_client: SandboxClient | None = None,
        access_key_id: str | None = None,
        access_key_secret: str | None = None,
        account_id: str | None = None,
        api_key: str | None = None,
    ) -> AliyunSandboxSession:
        return cls(
            state=state,
            sandbox=sandbox,
            sandbox_client=sandbox_client,
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            account_id=account_id,
            api_key=api_key,
        )

    # ------------------------------------------------------------------
    # Capability flags
    # ------------------------------------------------------------------
    def supports_pty(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # User and path helpers (no per-user execution)
    # ------------------------------------------------------------------
    def _reject_user_arg(
        self,
        *,
        op: Literal["exec", "read", "write"],
        user: str | User,
    ) -> None:
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
            validate_tar_bytes(raw)
        except UnsafeTarMemberError as exc:
            raise ValueError(str(exc)) from exc

    # ------------------------------------------------------------------
    # AgentRun lifecycle
    # ------------------------------------------------------------------
    def _build_config(self) -> Config:
        return _build_config(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            account_id=self._account_id,
            api_key=self._api_key,
            region=self.state.region,
        )

    async def _ensure_sandbox(self) -> CodeInterpreterSandbox:
        if self._sandbox is not None:
            return self._sandbox

        config = self._build_config()
        try:
            sandbox = await _create_sandbox_with_retry(
                config=config,
                template_name=self.state.template_name,
                sandbox_idle_timeout_seconds=self.state.sandbox_idle_timeout_seconds,
            )
        except Exception as exc:
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(self.state.manifest.root)),
                cause=exc,
            ) from exc

        if sandbox is None:
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(self.state.manifest.root)),
                context={"backend": "aliyun", "reason": "create_sandbox_returned_none"},
            )

        self._sandbox = sandbox
        self._sandbox_client = SandboxClient(config=config)
        self._owned = True
        sandbox_id = getattr(sandbox, "sandbox_id", None)
        if isinstance(sandbox_id, str) and sandbox_id:
            self.state.sandbox_id = sandbox_id
        return sandbox

    async def _prepare_backend_workspace(self) -> None:
        sandbox = await self._ensure_sandbox()
        root = PurePosixPath(self.state.manifest.root)
        cmd = f"mkdir -p {shlex.quote(root.as_posix())}"
        try:
            result = await _run_shell(sandbox, command=cmd, cwd="/", timeout=15)
        except Exception as exc:
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(root.as_posix())),
                cause=exc,
            ) from exc

        if not result.get("success"):
            raise WorkspaceStartError(
                path=posix_path_as_path(coerce_posix_path(root.as_posix())),
                context={
                    "exit_code": result.get("exit_code"),
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                },
            )

        if self.state.env:
            await self._inject_user_env(self.state.env)

    async def _inject_user_env(self, env: dict[str, str]) -> None:
        """Persist `env` into the sandbox shell so that `sh -lc` execs pick it up.

        Writes `~/.openai_agents_env` with `export` lines and sources it from
        `~/.profile` via a marker section so we don't append duplicates on
        repeated calls.
        """
        sandbox = await self._ensure_sandbox()
        env_lines = "\n".join(
            f"export {k}={shlex.quote(v)}" for k, v in env.items() if v is not None
        )
        if not env_lines:
            return
        script = (
            f"cat > {_AGENTS_ENV_FILE} <<'OPENAI_AGENTS_ENV_EOF'\n"
            f"{env_lines}\n"
            "OPENAI_AGENTS_ENV_EOF\n"
            f"chmod 600 {_AGENTS_ENV_FILE}\n"
            f"if ! grep -q '{_AGENTS_ENV_MARKER}' \"$HOME/.profile\" 2>/dev/null; then\n"
            f"  printf '\\n%s\\n. %s\\n# <<< openai-agents env <<<\\n' "
            f"'{_AGENTS_ENV_MARKER}' '{_AGENTS_ENV_FILE}' >> \"$HOME/.profile\"\n"
            "fi\n"
        )
        try:
            await _run_shell(sandbox, command=script, cwd="/", timeout=15)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[AliyunSandboxSession] failed to inject user env vars; continuing without them"
            )

    async def running(self) -> bool:
        sandbox = self._sandbox
        if sandbox is None:
            return False
        try:
            result = await _run_shell(sandbox, command="true", cwd="/", timeout=5)
        except Exception:  # noqa: BLE001
            return False
        return bool(result.get("success"))

    async def shutdown(self) -> None:
        await self._stop_attached_sandbox()

    async def _stop_attached_sandbox(self) -> None:
        sandbox = self._sandbox
        sandbox_client = self._sandbox_client
        if sandbox is None or not self._owned:
            return
        sandbox_id = getattr(sandbox, "sandbox_id", None) or self.state.sandbox_id
        try:
            if sandbox_client is not None and sandbox_id:
                await asyncio.to_thread(sandbox_client.delete_sandbox, sandbox_id)
        except Exception:  # noqa: BLE001
            logger.exception("[AliyunSandboxSession] error during delete_sandbox()")
        finally:
            self._sandbox = None
            self._sandbox_client = None

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

        # `_prepare_exec_command` already produced a `sh -lc <joined>` invocation
        # when shell=True. Either way we need a single string for AgentRun's
        # process.cmd API.
        shell_cmd = shlex.join(normalized)
        cwd = self.state.manifest.root
        effective_timeout = (
            int(timeout)
            if timeout is not None
            else self.state.exec_timeout_s or DEFAULT_ALIYUN_EXEC_TIMEOUT_S
        )

        try:
            result = await asyncio.wait_for(
                _run_shell(sandbox, command=shell_cmd, cwd=cwd, timeout=effective_timeout),
                timeout=(effective_timeout + 5) if effective_timeout is not None else None,
            )
        except asyncio.TimeoutError as exc:
            raise ExecTimeoutError(
                command=normalized,
                timeout_s=timeout,
                cause=exc,
            ) from exc
        except Exception as exc:
            raise ExecTransportError(
                command=normalized,
                context={"backend": "aliyun", "sandbox_id": self.state.sandbox_id},
                cause=exc,
            ) from exc

        if result.get("error") and not result.get("success"):
            err = result["error"]
            err_str = str(err).lower()
            if "timeout" in err_str or "timed out" in err_str:
                raise ExecTimeoutError(
                    command=normalized,
                    timeout_s=timeout,
                    context={"provider_error": str(err)},
                )
            raise ExecTransportError(
                command=normalized,
                context={
                    "backend": "aliyun",
                    "sandbox_id": self.state.sandbox_id,
                    "provider_error": str(err),
                },
            )

        stdout = (result.get("stdout") or "").encode("utf-8")
        stderr = (result.get("stderr") or "").encode("utf-8")
        exit_code = int(result.get("exit_code", 0) or 0)
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

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
        target = sandbox_path_str(normalized_path)
        try:
            payload = await _download_bytes(sandbox, remote_path=target)
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=normalized_path, cause=exc) from exc
        if payload is None:
            # Distinguish missing file vs. transport failure: probe via a
            # cheap `test -e` exec — if the file truly doesn't exist, surface
            # the "not found" error so SDK callers can react appropriately.
            probe = await _run_shell(
                sandbox,
                command=f"test -e {shlex.quote(target)}",
                cwd="/",
                timeout=15,
            )
            if probe.get("exit_code") != 0:
                raise WorkspaceReadNotFoundError(path=normalized_path)
            raise WorkspaceArchiveReadError(
                path=normalized_path,
                context={"backend": "aliyun", "reason": "download_returned_none"},
            )
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

        sandbox = await self._ensure_sandbox()
        try:
            ok = await _upload_bytes(
                sandbox,
                content=bytes(payload),
                remote_path=sandbox_path_str(normalized_path),
            )
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=normalized_path, cause=exc) from exc
        if not ok:
            raise WorkspaceArchiveWriteError(
                path=normalized_path,
                context={"backend": "aliyun"},
            )

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
            f"--exclude=./{rel.as_posix()}"
            for rel in sorted(
                self._persist_workspace_skip_relpaths(),
                key=lambda item: item.as_posix(),
            )
        ]
        tar_command = [
            "tar",
            "cf",
            archive_path.as_posix(),
            *excludes,
            "-C",
            self.state.manifest.root,
            ".",
        ]
        try:
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=root,
                    context={
                        "exit_code": result.exit_code,
                        "stdout": result.stdout.decode("utf-8", errors="replace"),
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                )

            archive_bytes = await _download_bytes(sandbox, remote_path=archive_path.as_posix())
            if archive_bytes is None:
                raise WorkspaceArchiveReadError(
                    path=archive_path,
                    context={"backend": "aliyun", "reason": "download_returned_none"},
                )
            return io.BytesIO(archive_bytes)
        except WorkspaceArchiveReadError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveReadError(path=root, cause=exc) from exc
        finally:
            try:
                await _run_shell(
                    sandbox,
                    command=f"rm -f {shlex.quote(archive_path.as_posix())}",
                    cwd="/",
                    timeout=15,
                )
            except Exception:  # noqa: BLE001
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
        try:
            self._validate_tar_bytes(raw_bytes)
            await self.mkdir(root, parents=True)
            ok = await _upload_bytes(
                sandbox,
                content=raw_bytes,
                remote_path=archive_path.as_posix(),
            )
            if not ok:
                raise WorkspaceArchiveWriteError(
                    path=archive_path,
                    context={"backend": "aliyun"},
                )
            tar_command = ["tar", "xf", archive_path.as_posix(), "-C", root.as_posix()]
            result = await self.exec(*tar_command, shell=False)
            if not result.ok():
                raise WorkspaceArchiveWriteError(
                    path=root,
                    context={
                        "exit_code": result.exit_code,
                        "stdout": result.stdout.decode("utf-8", errors="replace"),
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                )
        except WorkspaceArchiveWriteError:
            raise
        except Exception as exc:
            raise WorkspaceArchiveWriteError(path=root, cause=exc) from exc
        finally:
            try:
                await _run_shell(
                    sandbox,
                    command=f"rm -f {shlex.quote(archive_path.as_posix())}",
                    cwd="/",
                    timeout=15,
                )
            except Exception:  # noqa: BLE001
                pass


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

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: AliyunSandboxClientOptions,
    ) -> SandboxSession:
        resolved_manifest = _resolve_manifest_root(manifest)
        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = AliyunSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            sandbox_id="",
            region=options.region or self._region or DEFAULT_ALIYUN_REGION,
            template_name=options.template_name,
            sandbox_idle_timeout_seconds=options.sandbox_idle_timeout_seconds,
            default_cwd=options.default_cwd,
            env=dict(options.env or {}) or None,
            exec_timeout_s=options.exec_timeout_s,
        )
        inner = AliyunSandboxSession.from_state(
            state,
            access_key_id=self._resolve_credential(options.access_key_id, self._access_key_id),
            access_key_secret=self._resolve_credential(
                options.access_key_secret, self._access_key_secret
            ),
            account_id=self._resolve_credential(options.account_id, self._account_id),
            api_key=self._resolve_credential(options.api_key, self._api_key),
        )
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
        except Exception:  # noqa: BLE001
            logger.exception("[AliyunSandboxClient.delete] shutdown failed")
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, AliyunSandboxSessionState):
            raise TypeError("AliyunSandboxClient.resume expects an AliyunSandboxSessionState")

        # AgentRun sandboxes are not reliably re-addressable by id once the
        # client process has exited, so resume always provisions a fresh
        # sandbox and relies on the snapshot/manifest pipeline to repopulate
        # the workspace. Credentials are re-injected from the client (the
        # serialized state intentionally does not carry them).
        state.workspace_root_ready = False
        inner = AliyunSandboxSession.from_state(
            state,
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            account_id=self._account_id,
            api_key=self._api_key,
        )
        await inner._ensure_sandbox()
        inner._set_start_state_preserved(False)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return AliyunSandboxSessionState.model_validate(payload)


__all__ = [
    "AliyunSandboxClient",
    "AliyunSandboxClientOptions",
    "AliyunSandboxSession",
    "AliyunSandboxSessionState",
    "DEFAULT_ALIYUN_WORKSPACE_ROOT",
]
