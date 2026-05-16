"""Unit tests for the Aliyun AgentRun sandbox backend.

The `agentrun-sdk` is mocked via `sys.modules` injection so these tests do not
need the real package installed and never touch Alibaba Cloud.
"""

from __future__ import annotations

import importlib
import io
import sys
import tarfile
import types
import uuid
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import MagicMock

import pytest

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
from agents.sandbox.manifest import Manifest
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import User

# --------------------------------------------------------------------------- #
# Fake agentrun-sdk                                                           #
# --------------------------------------------------------------------------- #


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
        self.side_effect: Any = None

    def cmd(self, *, command: str, cwd: str | None = None, timeout: int = 30) -> Any:
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        if self.side_effect is not None:
            result = self.side_effect(command=command, cwd=cwd, timeout=timeout)
            return result
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

    def upload(self, *, local_file_path: str, target_file_path: str) -> None:
        self.upload_calls.append((local_file_path, target_file_path))
        if self.upload_exception is not None:
            raise self.upload_exception
        with open(local_file_path, "rb") as fh:
            self.files[target_file_path] = fh.read()

    def download(self, *, path: str, save_path: str) -> None:
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


class _FakeSandboxClient:
    """Stand-in for `agentrun.sandbox.client.SandboxClient`."""

    create_calls: list[dict[str, Any]] = []
    delete_calls: list[str] = []
    create_failures: list[BaseException] = []
    next_sandbox: _FakeCodeInterpreterSandbox | None = None

    def __init__(self, *, config: Any) -> None:
        self.config = config

    @classmethod
    def reset(cls) -> None:
        cls.create_calls = []
        cls.delete_calls = []
        cls.create_failures = []
        cls.next_sandbox = None

    def create_sandbox(
        self,
        *,
        template_name: str,
        sandbox_idle_timeout_seconds: int,
    ) -> _FakeCodeInterpreterSandbox:
        type(self).create_calls.append(
            {
                "template_name": template_name,
                "sandbox_idle_timeout_seconds": sandbox_idle_timeout_seconds,
            }
        )
        if type(self).create_failures:
            raise type(self).create_failures.pop(0)
        return type(self).next_sandbox or _FakeCodeInterpreterSandbox()

    def delete_sandbox(self, sandbox_id: str) -> None:
        type(self).delete_calls.append(sandbox_id)


class _FakeConfig:
    """Stand-in for `agentrun.utils.config.Config`."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _load_aliyun_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Inject a fake `agentrun` package and reload the wrapper module."""
    _FakeSandboxClient.reset()

    fake_agentrun = types.ModuleType("agentrun")
    fake_sandbox_pkg = cast(Any, types.ModuleType("agentrun.sandbox"))
    fake_client_mod = cast(Any, types.ModuleType("agentrun.sandbox.client"))
    fake_client_mod.SandboxClient = _FakeSandboxClient
    fake_cis_mod = cast(Any, types.ModuleType("agentrun.sandbox.code_interpreter_sandbox"))
    fake_cis_mod.CodeInterpreterSandbox = _FakeCodeInterpreterSandbox
    fake_utils_pkg = cast(Any, types.ModuleType("agentrun.utils"))
    fake_config_mod = cast(Any, types.ModuleType("agentrun.utils.config"))
    fake_config_mod.Config = _FakeConfig

    monkeypatch.setitem(sys.modules, "agentrun", fake_agentrun)
    monkeypatch.setitem(sys.modules, "agentrun.sandbox", fake_sandbox_pkg)
    monkeypatch.setitem(sys.modules, "agentrun.sandbox.client", fake_client_mod)
    monkeypatch.setitem(sys.modules, "agentrun.sandbox.code_interpreter_sandbox", fake_cis_mod)
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
        "exec_timeout_s": 30,
    }
    base.update(overrides)
    return aliyun_sandbox.AliyunSandboxSessionState(**base)


def _make_session(
    aliyun_sandbox: Any,
    *,
    state: Any | None = None,
    sandbox: _FakeCodeInterpreterSandbox | None = None,
    sandbox_client: _FakeSandboxClient | None = None,
    bypass_validate: bool = True,
    access_key_id: str | None = None,
    access_key_secret: str | None = None,
    account_id: str | None = None,
    api_key: str | None = None,
) -> Any:
    """Build an `AliyunSandboxSession` wired up with fake AgentRun primitives."""
    state = state or _make_state(aliyun_sandbox)
    sandbox = sandbox if sandbox is not None else _FakeCodeInterpreterSandbox()
    sandbox_client = sandbox_client or _FakeSandboxClient(config=_FakeConfig())
    session = aliyun_sandbox.AliyunSandboxSession.from_state(
        state,
        sandbox=sandbox,
        sandbox_client=sandbox_client,
        access_key_id=access_key_id,
        access_key_secret=access_key_secret,
        account_id=account_id,
        api_key=api_key,
    )
    # Mark as owned so shutdown actually exercises the delete path.
    session._owned = True
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
        default_cwd="/tmp",
        env={"FOO": "bar"},
        exec_timeout_s=99,
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
    import asyncio as _asyncio

    aliyun_sandbox = _load_aliyun_module(monkeypatch)

    async def _slow_run_shell(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        await _asyncio.sleep(10)
        return {"success": True, "stdout": "", "stderr": "", "exit_code": 0, "error": None}

    monkeypatch.setattr(aliyun_sandbox, "_run_shell", _slow_run_shell)
    session = _make_session(aliyun_sandbox)
    # Negative `timeout` makes the wrapper's internal wait_for budget go to
    # `timeout + 5 = 0` -> fires immediately, raising ExecTimeoutError.
    with pytest.raises(ExecTimeoutError):
        await session._exec_internal("sleep", "10", timeout=-5)


async def test_exec_transport_error_wraps_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.exception = RuntimeError("boom")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    # _run_shell catches the exception and returns it as result["error"];
    # the wrapper then surfaces ExecTransportError (no "timeout" substring).
    with pytest.raises(ExecTransportError) as excinfo:
        await session._exec_internal("echo", "hi")
    assert excinfo.value.context["backend"] == "aliyun"


async def test_exec_provider_timeout_in_result(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.exception = TimeoutError("command timed out after 30s")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(ExecTimeoutError) as excinfo:
        await session._exec_internal("echo", "hi")
    provider_err = excinfo.value.context.get("provider_error", "")
    assert "timed out" in str(provider_err).lower()


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
    assert excinfo.value.context.get("backend") == "aliyun"


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
    # test -e returns 1 → not found
    sandbox.process.result = {"stdout": "", "stderr": "", "exitCode": 1}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read(Path("/home/user/missing.bin"))


async def test_read_download_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.file_system.download_exception = RuntimeError("network")
    # test -e returns 0 (file "exists") so the wrapper takes the
    # archive-read-error branch instead of not-found.
    sandbox.process.result = {"stdout": "", "stderr": "", "exitCode": 0}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    with pytest.raises(WorkspaceArchiveReadError):
        await session.read(Path("/home/user/file.bin"))


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


# --------------------------------------------------------------------------- #
# G. Lifecycle                                                                #
# --------------------------------------------------------------------------- #


async def test_running_true(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.result = {"stdout": "", "stderr": "", "exitCode": 0}
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    assert await session.running() is True


async def test_running_no_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    state = _make_state(aliyun_sandbox)
    session = aliyun_sandbox.AliyunSandboxSession.from_state(state)  # no sandbox injected
    assert await session.running() is False


async def test_running_shell_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.exception = RuntimeError("dead")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    # _run_shell catches and returns success=False; running() then returns False.
    assert await session.running() is False


async def test_shutdown_deletes_when_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox(sandbox_id="owned-sandbox")
    sandbox_client = _FakeSandboxClient(config=_FakeConfig())
    session = _make_session(
        aliyun_sandbox,
        sandbox=sandbox,
        sandbox_client=sandbox_client,
    )
    session._owned = True
    await session.shutdown()
    assert "owned-sandbox" in _FakeSandboxClient.delete_calls
    assert session._sandbox is None


async def test_shutdown_skips_when_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox(sandbox_id="external")
    sandbox_client = _FakeSandboxClient(config=_FakeConfig())
    session = _make_session(
        aliyun_sandbox,
        sandbox=sandbox,
        sandbox_client=sandbox_client,
    )
    session._owned = False
    await session.shutdown()
    assert _FakeSandboxClient.delete_calls == []


async def test_shutdown_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox(sandbox_id="owned-sandbox")
    sandbox_client = _FakeSandboxClient(config=_FakeConfig())
    session = _make_session(
        aliyun_sandbox,
        sandbox=sandbox,
        sandbox_client=sandbox_client,
    )
    session._owned = True
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
        template_name="t1",
        sandbox_idle_timeout_seconds=60,
        default_cwd="/home/user",
        env={"X": "y"},
        exec_timeout_s=77,
    )
    session = await client.create(options=options)
    inner = session._inner
    st = inner.state
    # Credentials live on the session instance, not in state.
    assert inner._access_key_id == "opt-ak"
    assert inner._access_key_secret == "opt-sk"
    assert inner._api_key == "opt-key"
    # Non-credential options pass through to the serializable state.
    assert st.template_name == "t1"
    assert st.sandbox_idle_timeout_seconds == 60
    assert st.default_cwd == "/home/user"
    assert st.env == {"X": "y"}
    assert st.exec_timeout_s == 77
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
    assert inner._access_key_id == "root-ak"
    assert inner._access_key_secret == "root-sk"


def test_session_state_serialization_omits_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serialized session state must not leak credentials to disk."""
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    state = _make_state(aliyun_sandbox)
    payload = state.model_dump(mode="json")
    for forbidden in ("access_key_id", "access_key_secret", "account_id", "api_key"):
        assert forbidden not in payload, f"{forbidden!r} unexpectedly present in serialized state"


async def test_client_create_raises_on_create_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    # Three failures will exhaust the default retry_async budget (3 attempts).
    _FakeSandboxClient.create_failures = [
        RuntimeError("first"),
        RuntimeError("second"),
        RuntimeError("third"),
    ]
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


async def test_client_resume_marks_workspace_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox()
    client = aliyun_sandbox.AliyunSandboxClient(access_key_id="resumed-ak")
    state = _make_state(aliyun_sandbox)
    state.workspace_root_ready = True
    session = await client.resume(state)
    inner = session._inner
    assert inner.state.workspace_root_ready is False
    assert inner._start_workspace_state_preserved is False
    # Resume must re-inject credentials from the client onto the session.
    assert inner._access_key_id == "resumed-ak"


def test_client_deserialize_session_state_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    state = _make_state(
        aliyun_sandbox,
        template_name="t",
        exec_timeout_s=42,
    )
    payload = state.model_dump(mode="json")
    client = aliyun_sandbox.AliyunSandboxClient()
    restored = client.deserialize_session_state(payload)
    assert isinstance(restored, aliyun_sandbox.AliyunSandboxSessionState)
    assert restored.template_name == "t"
    assert restored.exec_timeout_s == 42


# --------------------------------------------------------------------------- #
# J. retry_async on _create_sandbox_with_retry                                #
# --------------------------------------------------------------------------- #


async def test_create_sandbox_retries_on_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)

    class _FakeHTTPError(Exception):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    _FakeSandboxClient.create_failures = [_FakeHTTPError(502)]
    _FakeSandboxClient.next_sandbox = _FakeCodeInterpreterSandbox(sandbox_id="recovered")

    client = aliyun_sandbox.AliyunSandboxClient()
    session = await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())
    # First call raised 502, second call succeeded → retry_async retried once.
    assert len(_FakeSandboxClient.create_calls) == 2
    assert session._inner.state.sandbox_id == "recovered"


async def test_create_sandbox_does_not_retry_non_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)

    class _FakeHTTPError(Exception):
        def __init__(self, status_code: int) -> None:
            super().__init__(f"HTTP {status_code}")
            self.status_code = status_code

    _FakeSandboxClient.create_failures = [_FakeHTTPError(400)]
    client = aliyun_sandbox.AliyunSandboxClient()
    with pytest.raises(WorkspaceStartError):
        await client.create(options=aliyun_sandbox.AliyunSandboxClientOptions())
    # Only one attempt; 400 is not transient.
    assert len(_FakeSandboxClient.create_calls) == 1


# --------------------------------------------------------------------------- #
# K. Env injection                                                            #
# --------------------------------------------------------------------------- #


async def test_inject_user_env_writes_export_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    session = _make_session(
        aliyun_sandbox,
        state=_make_state(aliyun_sandbox, env={"FOO": "bar", "WITH SPACE": "v 1"}),
        sandbox=sandbox,
    )
    await session._inject_user_env(session.state.env)
    sent = sandbox.process.calls[-1]["command"]
    assert "export FOO=bar" in sent
    assert "'v 1'" in sent  # shlex.quote on values with spaces


async def test_inject_user_env_swallows_internal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    aliyun_sandbox = _load_aliyun_module(monkeypatch)
    sandbox = _FakeCodeInterpreterSandbox()
    sandbox.process.exception = RuntimeError("boom")
    session = _make_session(aliyun_sandbox, sandbox=sandbox)
    # _run_shell catches exceptions internally — _inject_user_env should not
    # propagate even if execute_shell fails for some other reason.
    await session._inject_user_env({"X": "y"})
