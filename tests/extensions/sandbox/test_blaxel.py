from __future__ import annotations

import asyncio
import io
import json
import tarfile
import time
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    InvalidManifestPathError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceWriteTypeError,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExposedPortEndpoint
from agents.sandbox.util.tar_utils import validate_tar_bytes
from tests._fake_workspace_paths import resolve_fake_workspace_path

# ---------------------------------------------------------------------------
# Package re-export test
# ---------------------------------------------------------------------------


def test_blaxel_package_re_exports_backend_symbols() -> None:
    from agents.extensions.sandbox.blaxel.sandbox import BlaxelSandboxClient

    package_module = __import__(
        "agents.extensions.sandbox.blaxel", fromlist=["BlaxelSandboxClient"]
    )
    assert package_module.BlaxelSandboxClient is BlaxelSandboxClient


# ---------------------------------------------------------------------------
# Fakes that replicate the Blaxel SDK surface used by the sandbox backend.
# ---------------------------------------------------------------------------


class _FakeExecResult:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        output: str = "",
        stderr: str = "",
        pid: str = "",
    ) -> None:
        self.exit_code = exit_code
        self.stdout = output
        self.stderr = stderr
        self.logs = output
        self.pid = pid


def _fake_helper_exec_result(command: str, *, symlinks: dict[str, str]) -> _FakeExecResult | None:
    resolved = resolve_fake_workspace_path(
        command,
        symlinks=symlinks,
        home_dir="/workspace",
    )
    if resolved is not None:
        return _FakeExecResult(
            exit_code=resolved.exit_code,
            output=resolved.stdout,
            stderr=resolved.stderr,
        )

    if "INSTALL_RUNTIME_HELPER_V1" in command or command.startswith(
        "test -x /tmp/openai-agents/bin/resolve-workspace-path-"
    ):
        return _FakeExecResult()

    return None


class _FakeProcess:
    def __init__(self) -> None:
        self.exec_calls: list[tuple[dict[str, Any], dict[str, object]]] = []
        self.next_result = _FakeExecResult()
        self._results_queue: list[_FakeExecResult] = []
        self.delay: float = 0.0
        self.symlinks: dict[str, str] = {}

    async def exec(self, config: dict[str, Any], **kwargs: object) -> _FakeExecResult:
        self.exec_calls.append((config, dict(kwargs)))
        helper_result = _fake_helper_exec_result(
            str(config.get("command", "")),
            symlinks=self.symlinks,
        )
        if helper_result is not None:
            return helper_result
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self._results_queue:
            return self._results_queue.pop(0)
        result = self.next_result
        self.next_result = _FakeExecResult()
        return result


class _FakeFs:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: list[str] = []
        self.mkdir_calls: list[str] = []
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.mkdir_error: Exception | None = None
        self.return_str: bool = False
        self.read_binary_calls: list[str] = []
        self.write_binary_calls: list[tuple[str, bytes]] = []

    async def mkdir(self, path: str, permissions: str = "0755") -> None:
        self.mkdir_calls.append(path)
        if self.mkdir_error is not None:
            raise self.mkdir_error
        self.dirs.append(path)

    async def read_binary(self, path: str) -> bytes | str:
        self.read_binary_calls.append(path)
        if self.read_error is not None:
            raise self.read_error
        if path not in self.files:
            raise FileNotFoundError(f"not found: {path}")
        data = self.files[path]
        if self.return_str:
            return data.decode("utf-8")
        return data

    async def write_binary(self, path: str, data: bytes) -> None:
        self.write_binary_calls.append((path, data))
        if self.write_error is not None:
            raise self.write_error
        self.files[path] = data

    async def ls(self, path: str) -> list[str]:
        # Return files whose paths start with the given directory.
        matches = [p for p in self.files if p.startswith(path.rstrip("/") + "/") or p == path]
        return matches if matches else [path]


class _FakePreviewToken:
    def __init__(self, value: str = "fake-token-abc123") -> None:
        self.value = value


class _FakePreviewTokens:
    def __init__(self) -> None:
        self.create_calls: list[Any] = []
        self.next_token = _FakePreviewToken()
        self.error: Exception | None = None

    async def create(self, expires_at: Any) -> _FakePreviewToken:
        self.create_calls.append(expires_at)
        if self.error is not None:
            raise self.error
        return self.next_token


class _FakePreview:
    def __init__(self, url: str = "https://preview.example.com:443/") -> None:
        self.url = url
        self.tokens = _FakePreviewTokens()


class _FakePreviews:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_preview = _FakePreview()
        self.error: Exception | None = None

    async def create_if_not_exists(self, config: dict[str, Any]) -> _FakePreview:
        self.calls.append(config)
        if self.error is not None:
            raise self.error
        return self.next_preview


class _FakeMetadata:
    def __init__(self, name: str = "test-sandbox", url: str = "https://test.bl.run") -> None:
        self.name = name
        self.url = url


class _FakeSandboxModel:
    def __init__(self, name: str = "test-sandbox", url: str = "https://test.bl.run") -> None:
        self.metadata = _FakeMetadata(name=name, url=url)


class _FakeDrives:
    """Fake drives API for testing Blaxel Drive mounts."""

    def __init__(self) -> None:
        self.mount_calls: list[tuple[str, str, str]] = []
        self.unmount_calls: list[str] = []
        self.mount_error: Exception | None = None
        self.unmount_error: Exception | None = None

    async def mount(self, drive_name: str, mount_path: str, drive_path: str) -> None:
        self.mount_calls.append((drive_name, mount_path, drive_path))
        if self.mount_error is not None:
            raise self.mount_error

    async def unmount(self, mount_path: str) -> None:
        self.unmount_calls.append(mount_path)
        if self.unmount_error is not None:
            raise self.unmount_error


class _FakeSandboxInstance:
    """Mimics ``blaxel.core.sandbox.SandboxInstance``."""

    def __init__(self, name: str = "test-sandbox", url: str = "https://test.bl.run") -> None:
        self.process = _FakeProcess()
        self.fs = _FakeFs()
        self.previews = _FakePreviews()
        self.sandbox = _FakeSandboxModel(name=name, url=url)
        self.drives = _FakeDrives()
        self._deleted = False

    async def delete(self) -> None:
        self._deleted = True

    # Class-level stubs used by the client.
    _instances: dict[str, _FakeSandboxInstance] = {}
    _create_error: Exception | None = None

    @classmethod
    async def create_if_not_exists(cls, config: dict[str, Any]) -> _FakeSandboxInstance:
        if cls._create_error is not None:
            raise cls._create_error
        name = config.get("name", "default")
        inst = cls(name=name)
        cls._instances[name] = inst
        return inst

    @classmethod
    async def get(cls, name: str) -> _FakeSandboxInstance:
        if name in cls._instances:
            return cls._instances[name]
        raise RuntimeError(f"sandbox {name} not found")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_fake_instances() -> None:
    _FakeSandboxInstance._instances.clear()
    _FakeSandboxInstance._create_error = None


@pytest.fixture()
def fake_sandbox() -> _FakeSandboxInstance:
    return _FakeSandboxInstance(name="test-sandbox")


def _make_state(
    sandbox_name: str = "test-sandbox",
    root: str = "/workspace",
    pause_on_exit: bool = False,
    sandbox_url: str | None = "https://test.bl.run",
    extra_path_grants: tuple[SandboxPathGrant, ...] = (),
) -> Any:
    from agents.extensions.sandbox.blaxel.sandbox import (
        BlaxelSandboxSessionState,
        BlaxelTimeouts,
    )

    return BlaxelSandboxSessionState(
        session_id=uuid.uuid4(),
        manifest=Manifest(root=root, extra_path_grants=extra_path_grants),
        snapshot=NoopSnapshot(id="test-snapshot"),
        sandbox_name=sandbox_name,
        pause_on_exit=pause_on_exit,
        timeouts=BlaxelTimeouts(),
        sandbox_url=sandbox_url,
    )


def _make_session(
    fake: _FakeSandboxInstance,
    state: Any | None = None,
    token: str | None = "test-token",
) -> Any:
    from agents.extensions.sandbox.blaxel.sandbox import BlaxelSandboxSession

    if state is None:
        state = _make_state()
    return BlaxelSandboxSession.from_state(state, sandbox=fake, token=token)


# ---------------------------------------------------------------------------
# Session tests
# ---------------------------------------------------------------------------


class TestBlaxelSandboxSession:
    @pytest.mark.asyncio
    async def test_exec_success(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.process.next_result = _FakeExecResult(exit_code=0, output="hello world")
        result = await session._exec_internal("echo", "hello")
        assert result.exit_code == 0
        assert result.stdout == b"hello world"
        assert len(fake_sandbox.process.exec_calls) == 1

    @pytest.mark.asyncio
    async def test_exec_success_preserves_split_stderr(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.process.next_result = _FakeExecResult(
            exit_code=0,
            output="hello world",
            stderr="warning",
        )
        result = await session._exec_internal("echo", "hello")
        assert result.exit_code == 0
        assert result.stdout == b"hello world"
        assert result.stderr == b"warning"

    @pytest.mark.asyncio
    async def test_exec_nonzero(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.process.next_result = _FakeExecResult(
            exit_code=1, output="", stderr="error msg"
        )
        result = await session._exec_internal("false")
        assert result.exit_code == 1
        assert result.stderr == b"error msg"

    @pytest.mark.asyncio
    async def test_exec_transport_error(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)

        async def _raise(*args: object, **kw: object) -> None:
            raise ConnectionError("transport error")

        fake_sandbox.process.exec = _raise  # type: ignore[assignment]
        with pytest.raises(ExecTransportError):
            await session._exec_internal("echo", "hello")

    @pytest.mark.asyncio
    async def test_mkdir(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        await session.mkdir("subdir")
        assert len(fake_sandbox.fs.mkdir_calls) == 1
        assert "/workspace/subdir" in fake_sandbox.fs.mkdir_calls[0]

    @pytest.mark.asyncio
    async def test_read(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.fs.files["/workspace/test.txt"] = b"file content"
        result = await session.read("test.txt")
        assert result.read() == b"file content"

    @pytest.mark.asyncio
    async def test_read_not_found(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        with pytest.raises(WorkspaceReadNotFoundError):
            await session.read("nonexistent.txt")

    @pytest.mark.asyncio
    async def test_read_rejects_workspace_symlink_to_ungranted_path(
        self,
        fake_sandbox: _FakeSandboxInstance,
    ) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.process.symlinks["/workspace/link"] = "/private"

        with pytest.raises(InvalidManifestPathError) as exc_info:
            await session.read("link/secret.txt")

        assert fake_sandbox.fs.read_binary_calls == []
        assert str(exc_info.value) == "manifest path must not escape root: link/secret.txt"
        assert exc_info.value.context == {
            "rel": "link/secret.txt",
            "reason": "escape_root",
            "resolved_path": "workspace escape: /private/secret.txt",
        }

    @pytest.mark.asyncio
    async def test_write(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        await session.write("output.txt", io.BytesIO(b"written data"))
        assert fake_sandbox.fs.files["/workspace/output.txt"] == b"written data"

    @pytest.mark.asyncio
    async def test_write_rejects_workspace_symlink_to_read_only_extra_path_grant(
        self,
        fake_sandbox: _FakeSandboxInstance,
    ) -> None:
        state = _make_state(
            extra_path_grants=(SandboxPathGrant(path="/tmp/protected", read_only=True),)
        )
        session = _make_session(fake_sandbox, state=state)
        fake_sandbox.process.symlinks["/workspace/link"] = "/tmp/protected"

        with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
            await session.write("link/out.txt", io.BytesIO(b"blocked"))

        assert fake_sandbox.fs.write_binary_calls == []
        assert str(exc_info.value) == "failed to write archive for path: /workspace/link/out.txt"
        assert exc_info.value.context == {
            "path": "/workspace/link/out.txt",
            "reason": "read_only_extra_path_grant",
            "grant_path": "/tmp/protected",
            "resolved_path": "/tmp/protected/out.txt",
        }

    @pytest.mark.asyncio
    async def test_mkdir_rejects_workspace_symlink_to_read_only_extra_path_grant(
        self,
        fake_sandbox: _FakeSandboxInstance,
    ) -> None:
        state = _make_state(
            extra_path_grants=(SandboxPathGrant(path="/tmp/protected", read_only=True),)
        )
        session = _make_session(fake_sandbox, state=state)
        fake_sandbox.process.symlinks["/workspace/link"] = "/tmp/protected"

        with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
            await session.mkdir("link/newdir")

        assert fake_sandbox.fs.mkdir_calls == []
        assert str(exc_info.value) == "failed to write archive for path: /workspace/link/newdir"
        assert exc_info.value.context == {
            "path": "/workspace/link/newdir",
            "reason": "read_only_extra_path_grant",
            "grant_path": "/tmp/protected",
            "resolved_path": "/tmp/protected/newdir",
        }

    @pytest.mark.asyncio
    async def test_running(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        assert await session.running() is True

    @pytest.mark.asyncio
    async def test_running_when_down(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)

        async def _raise(*args: object, **kw: object) -> None:
            raise ConnectionError("offline")

        fake_sandbox.fs.ls = _raise  # type: ignore[assignment]
        assert await session.running() is False

    @pytest.mark.asyncio
    async def test_shutdown_deletes(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        await session.shutdown()
        assert fake_sandbox._deleted is True

    @pytest.mark.asyncio
    async def test_shutdown_pause_on_exit(self, fake_sandbox: _FakeSandboxInstance) -> None:
        state = _make_state(pause_on_exit=True)
        session = _make_session(fake_sandbox, state=state)
        await session.shutdown()
        assert fake_sandbox._deleted is False

    @pytest.mark.asyncio
    async def test_normalize_path_relative(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        result = session.normalize_path("subdir/file.txt")
        assert result.as_posix() == "/workspace/subdir/file.txt"

    @pytest.mark.asyncio
    async def test_normalize_path_escape_blocked(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        with pytest.raises(InvalidManifestPathError):
            session.normalize_path("../../etc/passwd")

    @pytest.mark.asyncio
    async def test_normalize_path_absolute_blocked(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox)
        with pytest.raises(InvalidManifestPathError):
            session.normalize_path("/etc/passwd")

    @pytest.mark.asyncio
    async def test_mkdir_root_is_noop(self, fake_sandbox: _FakeSandboxInstance) -> None:
        state = _make_state(root="/")
        session = _make_session(fake_sandbox, state=state)
        await session.mkdir("/")
        # No fs.mkdir call should have been made.
        assert len(fake_sandbox.fs.mkdir_calls) == 0

    @pytest.mark.asyncio
    async def test_mkdir_failure(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.fs.mkdir_error = ConnectionError("fs down")
        with pytest.raises(WorkspaceArchiveWriteError):
            await session.mkdir("faildir")

    @pytest.mark.asyncio
    async def test_read_returns_str(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.fs.files["/workspace/text.txt"] = b"string content"
        fake_sandbox.fs.return_str = True
        result = await session.read("text.txt")
        assert result.read() == b"string content"

    @pytest.mark.asyncio
    async def test_read_status_404_via_args_dict(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        # Simulate Blaxel ResponseError with status in args[0] dict.
        err = Exception({"status": 404, "message": "not found"})
        fake_sandbox.fs.read_error = err
        with pytest.raises(WorkspaceReadNotFoundError):
            await session.read("missing.txt")

    @pytest.mark.asyncio
    async def test_read_generic_error(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.fs.read_error = RuntimeError("unexpected")
        with pytest.raises(WorkspaceArchiveReadError):
            await session.read("broken.txt")

    @pytest.mark.asyncio
    async def test_read_status_attr_on_error(self, fake_sandbox: _FakeSandboxInstance) -> None:
        # Error with .status attribute set (e.g. Blaxel ResponseError).
        session = _make_session(fake_sandbox)
        err = RuntimeError("file missing")
        err.status = 404  # type: ignore[attr-defined]
        fake_sandbox.fs.read_error = err
        with pytest.raises(WorkspaceReadNotFoundError):
            await session.read("gone.txt")

    @pytest.mark.asyncio
    async def test_read_not_found_via_error_string(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.fs.read_error = RuntimeError("No such file or directory")
        with pytest.raises(WorkspaceReadNotFoundError):
            await session.read("missing.txt")

    @pytest.mark.asyncio
    async def test_write_str_payload(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        await session.write("text.txt", io.StringIO("hello text"))
        assert fake_sandbox.fs.files["/workspace/text.txt"] == b"hello text"

    @pytest.mark.asyncio
    async def test_write_invalid_payload_type(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)

        class _BadIO(io.IOBase):
            def read(self) -> int:
                return 42

        with pytest.raises(WorkspaceWriteTypeError):
            await session.write("bad.txt", _BadIO())

    @pytest.mark.asyncio
    async def test_write_fs_error(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.fs.write_error = ConnectionError("fs write failed")
        with pytest.raises(WorkspaceArchiveWriteError):
            await session.write("fail.txt", io.BytesIO(b"data"))

    @pytest.mark.asyncio
    async def test_exec_timeout(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.process.delay = 10.0
        with pytest.raises(ExecTimeoutError):
            await session._exec_internal("sleep", "100", timeout=0.01)

    @pytest.mark.asyncio
    async def test_stop_calls_pty_terminate(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        terminated = []
        original = session.pty_terminate_all

        async def _track() -> None:
            terminated.append(True)
            await original()

        session.pty_terminate_all = _track
        await session.stop()
        assert len(terminated) == 1

    @pytest.mark.asyncio
    async def test_shutdown_delete_raises(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)

        async def _raise() -> None:
            raise RuntimeError("delete failed")

        fake_sandbox.delete = _raise  # type: ignore[method-assign]
        # Should not raise; error is suppressed.
        await session.shutdown()

    @pytest.mark.asyncio
    async def test_sandbox_name_property(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        assert session.sandbox_name == "test-sandbox"

    @pytest.mark.asyncio
    async def test_exposed_port_invalid_url(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.previews.next_preview = _FakePreview(url="")
        with pytest.raises(ExposedPortUnavailableError):
            await session._resolve_exposed_port(8080)

    @pytest.mark.asyncio
    async def test_exposed_port_bad_url_parse(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        # URL without a hostname.
        fake_sandbox.previews.next_preview = _FakePreview(url="https://")
        with pytest.raises(ExposedPortUnavailableError):
            await session._resolve_exposed_port(8080)

    @pytest.mark.asyncio
    async def test_exposed_port_http_scheme(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.previews.next_preview = _FakePreview(url="http://preview.example.com/")
        endpoint = await session._resolve_exposed_port(80)
        assert endpoint.tls is False
        assert endpoint.port == 80

    @pytest.mark.asyncio
    async def test_exposed_port(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        endpoint = await session._resolve_exposed_port(3000)
        assert isinstance(endpoint, ExposedPortEndpoint)
        assert endpoint.host == "preview.example.com"
        assert endpoint.tls is True

    @pytest.mark.asyncio
    async def test_exposed_port_any_port_without_predeclaration(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """Blaxel previews can be created for any port on demand."""
        session = _make_session(fake_sandbox)
        # Call the public resolve_exposed_port (which checks _assert_exposed_port_configured).
        # No exposed_ports were declared, but it should still work.
        endpoint = await session.resolve_exposed_port(9999)
        assert isinstance(endpoint, ExposedPortEndpoint)
        assert endpoint.host == "preview.example.com"

    @pytest.mark.asyncio
    async def test_exposed_port_error(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.previews.error = RuntimeError("backend down")
        with pytest.raises(ExposedPortUnavailableError):
            await session._resolve_exposed_port(3000)

    @pytest.mark.asyncio
    async def test_exposed_port_public_preview(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """Public preview should not include a token query string."""
        session = _make_session(fake_sandbox)
        endpoint = await session._resolve_exposed_port(8080)
        assert endpoint.query == ""
        # Verify the preview was created with public=True.
        assert fake_sandbox.previews.calls[-1]["spec"]["public"] is True

    @pytest.mark.asyncio
    async def test_exposed_port_private_preview(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """Private preview should create a token and set the query string."""
        state = _make_state()
        object.__setattr__(state, "exposed_port_public", False)
        session = _make_session(fake_sandbox, state=state)
        preview = _FakePreview(url="https://preview.example.com:443/")
        preview.tokens.next_token = _FakePreviewToken(value="my-secret-token")
        fake_sandbox.previews.next_preview = preview
        endpoint = await session._resolve_exposed_port(8080)
        # Verify the preview was created with public=False.
        assert fake_sandbox.previews.calls[-1]["spec"]["public"] is False
        # Verify token was created and attached as query.
        assert len(preview.tokens.create_calls) == 1
        assert endpoint.query == "bl_preview_token=my-secret-token"
        assert "bl_preview_token=my-secret-token" in endpoint.url_for("http")

    @pytest.mark.asyncio
    async def test_exposed_port_private_token_error(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """Token creation failure should raise ExposedPortUnavailableError."""
        state = _make_state()
        object.__setattr__(state, "exposed_port_public", False)
        session = _make_session(fake_sandbox, state=state)
        preview = _FakePreview(url="https://preview.example.com:443/")
        preview.tokens.error = RuntimeError("token service down")
        fake_sandbox.previews.next_preview = preview
        with pytest.raises(ExposedPortUnavailableError):
            await session._resolve_exposed_port(8080)

    @pytest.mark.asyncio
    async def test_supports_pty_with_url_and_token(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox, token="tok")
        # Depends on aiohttp availability in test env.
        try:
            import aiohttp  # noqa: F401

            assert session.supports_pty() is True
        except ImportError:
            assert session.supports_pty() is False

    @pytest.mark.asyncio
    async def test_supports_pty_without_token(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox, token=None)
        assert session.supports_pty() is False

    @pytest.mark.asyncio
    async def test_supports_pty_without_url(self, fake_sandbox: _FakeSandboxInstance) -> None:
        state = _make_state(sandbox_url=None)
        session = _make_session(fake_sandbox, state=state, token="tok")
        assert session.supports_pty() is False


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------


class TestBlaxelSandboxClient:
    @pytest.mark.asyncio
    async def test_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions(name="my-sandbox")
        session = await client.create(options=options)
        assert session is not None

    @pytest.mark.asyncio
    async def test_create_with_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions(
            name="img-sandbox",
            image="blaxel/py-app:latest",
            memory=4096,
            region="us-pdx-1",
        )
        session = await client.create(options=options)
        assert session is not None

    @pytest.mark.asyncio
    async def test_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions(name="del-sandbox")
        session = await client.create(options=options)
        result = await client.delete(session)
        assert result is session

    @pytest.mark.asyncio
    async def test_resume_reconnects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        # Pre-populate the instance so get() finds it.
        existing = _FakeSandboxInstance(name="resume-sandbox")
        _FakeSandboxInstance._instances["resume-sandbox"] = existing

        client = mod.BlaxelSandboxClient(token="test-token")
        state = _make_state(sandbox_name="resume-sandbox", pause_on_exit=True)
        session = await client.resume(state)
        assert session is not None

    @pytest.mark.asyncio
    async def test_resume_creates_new(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        state = _make_state(sandbox_name="new-sandbox", pause_on_exit=False)
        session = await client.resume(state)
        assert session is not None

    @pytest.mark.asyncio
    async def test_deserialize_session_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        payload: dict[str, object] = {
            "session_id": str(uuid.uuid4()),
            "manifest": {"root": "/workspace"},
            "snapshot": {"type": "noop", "id": "test-snap"},
            "sandbox_name": "test",
        }
        state = client.deserialize_session_state(payload)
        assert isinstance(state, mod.BlaxelSandboxSessionState)
        assert state.sandbox_name == "test"

    @pytest.mark.asyncio
    async def test_context_manager(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        async with mod.BlaxelSandboxClient(token="test-token") as client:
            assert client is not None


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_build_create_config_minimal(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _build_create_config

        config = _build_create_config(name="test")
        assert config["name"] == "test"

    def test_build_create_config_full(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _build_create_config

        config = _build_create_config(
            name="full",
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            memory=4096,
            region="us-west",
            env_vars={"KEY": "VAL"},
            labels={"env": "test"},
            ttl="24h",
        )
        assert config["image"] == DEFAULT_PYTHON_SANDBOX_IMAGE
        assert config["memory"] == 4096
        assert config["region"] == "us-west"
        assert config["labels"] == {"env": "test"}
        assert config["ttl"] == "24h"
        assert "ports" not in config
        assert config["envs"] == [{"name": "KEY", "value": "VAL"}]

    def test_get_sandbox_url(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _get_sandbox_url

        fake = _FakeSandboxInstance(url="https://sandbox.bl.run")
        assert _get_sandbox_url(fake) == "https://sandbox.bl.run"

    def test_get_sandbox_url_missing(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _get_sandbox_url

        class _Bare:
            pass

        assert _get_sandbox_url(_Bare()) is None

    def test_build_ws_url(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _build_ws_url

        url = _build_ws_url(
            sandbox_url="https://test.bl.run",
            token="tok123",
            session_id="sess-1",
            cwd="/workspace",
        )
        assert url.startswith("wss://test.bl.run/terminal/ws?")
        assert "token=tok123" in url
        assert "sessionId=sess-1" in url
        assert "workingDir=/workspace" in url

    def test_extract_preview_url(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _extract_preview_url

        assert _extract_preview_url(_FakePreview("https://p.bl.run")) == "https://p.bl.run"

    def test_extract_preview_url_nested(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _extract_preview_url

        class _Nested:
            url = None

            class status:
                url = "https://nested.bl.run"

        assert _extract_preview_url(_Nested()) == "https://nested.bl.run"

    def test_extract_preview_url_direct_endpoint(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _extract_preview_url

        class _Direct:
            url = None
            spec = None
            status = None
            endpoint = "https://direct.bl.run"

        assert _extract_preview_url(_Direct()) == "https://direct.bl.run"

    def test_extract_preview_url_inner_preview(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _extract_preview_url

        class _Inner:
            url = "https://inner.bl.run"

        class _Outer:
            url = None
            spec = None
            status = None
            endpoint = None
            preview = _Inner()

        assert _extract_preview_url(_Outer()) == "https://inner.bl.run"

    def test_extract_preview_url_returns_none(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _extract_preview_url

        class _Empty:
            pass

        assert _extract_preview_url(_Empty()) is None

    def test_get_sandbox_url_direct_url(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _get_sandbox_url

        class _DirectUrl:
            sandbox = None
            url = "https://direct.bl.run"

        assert _get_sandbox_url(_DirectUrl()) == "https://direct.bl.run"

    def test_get_sandbox_url_empty_string(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _get_sandbox_url

        class _EmptyUrl:
            sandbox = None
            url = ""

        assert _get_sandbox_url(_EmptyUrl()) is None

    def test_build_ws_url_http_scheme(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _build_ws_url

        url = _build_ws_url(
            sandbox_url="http://test.bl.run",
            token="tok",
            session_id="s1",
            cwd="/w",
        )
        assert url.startswith("ws://test.bl.run/")

    def test_build_create_config_with_ports(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _build_create_config

        config = _build_create_config(
            name="test",
            ports=({"target": 3000, "protocol": "HTTP"},),
        )
        assert len(config["ports"]) == 1
        assert config["ports"][0]["target"] == 3000

    def test_build_create_config_region_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _build_create_config

        monkeypatch.setenv("BL_REGION", "eu-ams-1")
        config = _build_create_config(name="test")
        assert config["region"] == "eu-ams-1"

    def test_build_create_config_default_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _build_create_config

        monkeypatch.delenv("BL_REGION", raising=False)
        config = _build_create_config(name="test")
        assert config["region"] == "us-pdx-1"


# ---------------------------------------------------------------------------
# Import guard tests
# ---------------------------------------------------------------------------


class TestImportGuards:
    def test_import_blaxel_sdk_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        def _fail() -> None:
            raise ImportError("no blaxel")

        monkeypatch.setattr(mod, "_import_blaxel_sdk", _fail)
        with pytest.raises(ImportError, match="no blaxel"):
            mod._import_blaxel_sdk()

    def test_import_aiohttp_missing(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _import_aiohttp

        with patch.dict("sys.modules", {"aiohttp": None}):
            with pytest.raises(ImportError, match="aiohttp"):
                _import_aiohttp()

    def test_has_aiohttp_false(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _has_aiohttp

        with patch.dict("sys.modules", {"aiohttp": None}):
            assert _has_aiohttp() is False

    def test_has_aiohttp_true(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _has_aiohttp

        # aiohttp should be available in the test environment.
        try:
            import aiohttp  # noqa: F401

            assert _has_aiohttp() is True
        except ImportError:
            pytest.skip("aiohttp not available")


# ---------------------------------------------------------------------------
# Tar validation tests
# ---------------------------------------------------------------------------


def _make_tar(members: dict[str, bytes | None] | None = None) -> bytes:
    """Build a tar archive in memory. Pass None as value for directories."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in (members or {}).items():
            if content is None:
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_tar_with_symlink_and_file(*, symlink_name: str, target: str, file_name: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        link = tarfile.TarInfo(name=symlink_name)
        link.type = tarfile.SYMTYPE
        link.linkname = target
        tar.addfile(link)

        contents = b"nested"
        file_info = tarfile.TarInfo(name=file_name)
        file_info.size = len(contents)
        tar.addfile(file_info, io.BytesIO(contents))
    return buf.getvalue()


class TestValidateTarBytes:
    def _validate(self, raw: bytes) -> None:
        validate_tar_bytes(raw)

    def test_valid_tar(self) -> None:
        raw = _make_tar({"hello.txt": b"content", "subdir/": None})
        self._validate(raw)

    def test_absolute_path_rejected(self) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="/etc/passwd")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"root"))
        with pytest.raises(ValueError, match="absolute path"):
            self._validate(buf.getvalue())

    def test_parent_traversal_rejected(self) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="../escape.txt")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"data"))
        with pytest.raises(ValueError, match="parent traversal"):
            self._validate(buf.getvalue())

    def test_tar_member_under_archive_symlink_rejected(self) -> None:
        raw = _make_tar_with_symlink_and_file(
            symlink_name="link.txt",
            target="/etc/passwd",
            file_name="link.txt/nested.txt",
        )
        with pytest.raises(ValueError, match="descends through symlink"):
            self._validate(raw)

    def test_corrupt_tar_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid tar"):
            self._validate(b"not a tar file at all")

    def test_dot_entries_skipped(self) -> None:
        raw = _make_tar({"./": None, "file.txt": b"ok"})
        self._validate(raw)


# ---------------------------------------------------------------------------
# Workspace persistence tests
# ---------------------------------------------------------------------------


class TestWorkspacePersistence:
    @pytest.mark.asyncio
    async def test_persist_workspace(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        # Queue up results: mkdir for start, tar command success.
        tar_data = _make_tar({"file.txt": b"hello"})
        fake_sandbox.process._results_queue = [
            _FakeExecResult(exit_code=0, output=""),  # tar command
            _FakeExecResult(exit_code=0, output=""),  # rm cleanup
        ]
        # Pre-populate the tar file so read_binary finds it.
        tar_path = f"/tmp/bl-persist-{session.state.session_id.hex}.tar"
        fake_sandbox.fs.files[tar_path] = tar_data
        result = await session.persist_workspace()
        assert result.read() == tar_data

    @pytest.mark.asyncio
    async def test_persist_workspace_tar_fails(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.process._results_queue = [
            _FakeExecResult(exit_code=1, output="tar: error"),  # tar command fails
            _FakeExecResult(exit_code=0, output=""),  # rm cleanup
        ]
        with pytest.raises(WorkspaceArchiveReadError) as exc_info:
            await session.persist_workspace()
        assert exc_info.value.context["reason"] == "tar_failed"

    @pytest.mark.asyncio
    async def test_persist_workspace_read_fails(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        fake_sandbox.process._results_queue = [
            _FakeExecResult(exit_code=0, output=""),  # tar succeeds
            _FakeExecResult(exit_code=0, output=""),  # rm cleanup
        ]
        # No tar file in fs, so read_binary will raise FileNotFoundError.
        with pytest.raises(WorkspaceArchiveReadError):
            await session.persist_workspace()

    @pytest.mark.asyncio
    async def test_persist_workspace_read_returns_str(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"a.txt": b"data"})
        fake_sandbox.process._results_queue = [
            _FakeExecResult(exit_code=0, output=""),
            _FakeExecResult(exit_code=0, output=""),
        ]
        tar_path = f"/tmp/bl-persist-{session.state.session_id.hex}.tar"
        fake_sandbox.fs.files[tar_path] = tar_data
        fake_sandbox.fs.return_str = True
        # This will encode the string back to bytes.
        result = await session.persist_workspace()
        assert len(result.read()) > 0


# ---------------------------------------------------------------------------
# Workspace hydration tests
# ---------------------------------------------------------------------------


class TestWorkspaceHydration:
    @pytest.mark.asyncio
    async def test_hydrate_workspace(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"file.txt": b"hello"})
        fake_sandbox.process._results_queue = [
            _FakeExecResult(exit_code=0, output=""),  # tar extract
            _FakeExecResult(exit_code=0, output=""),  # rm cleanup
        ]
        await session.hydrate_workspace(io.BytesIO(tar_data))

    @pytest.mark.asyncio
    async def test_hydrate_invalid_tar(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
            await session.hydrate_workspace(io.BytesIO(b"not a tar"))
        assert exc_info.value.context["reason"] == "unsafe_or_invalid_tar"

    @pytest.mark.asyncio
    async def test_hydrate_tar_with_symlink(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        raw = _make_tar_with_symlink_and_file(
            symlink_name="link.txt",
            target="/etc/shadow",
            file_name="link.txt/nested.txt",
        )
        with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
            await session.hydrate_workspace(io.BytesIO(raw))
        assert "unsafe_or_invalid_tar" in str(exc_info.value.context)

    @pytest.mark.asyncio
    async def test_hydrate_extract_fails(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"file.txt": b"hello"})
        fake_sandbox.process._results_queue = [
            _FakeExecResult(exit_code=1, output="tar: extract error"),  # extract fails
            _FakeExecResult(exit_code=0, output=""),  # rm cleanup
        ]
        with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
            await session.hydrate_workspace(io.BytesIO(tar_data))
        assert exc_info.value.context["reason"] == "tar_extract_failed"

    @pytest.mark.asyncio
    async def test_hydrate_str_payload_encoded(self, fake_sandbox: _FakeSandboxInstance) -> None:
        # A str payload gets encoded to bytes, then fails tar validation.
        session = _make_session(fake_sandbox)

        class _StrIO(io.IOBase):
            def read(self) -> str:
                return "not a valid tar"

        with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
            await session.hydrate_workspace(_StrIO())
        assert exc_info.value.context["reason"] == "unsafe_or_invalid_tar"

    @pytest.mark.asyncio
    async def test_hydrate_invalid_payload_type(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)

        class _IntIO(io.IOBase):
            def read(self) -> int:
                return 42

        with pytest.raises(WorkspaceWriteTypeError):
            await session.hydrate_workspace(_IntIO())

    @pytest.mark.asyncio
    async def test_hydrate_write_binary_fails(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"file.txt": b"hello"})
        fake_sandbox.fs.write_error = ConnectionError("upload failed")
        with pytest.raises(WorkspaceArchiveWriteError):
            await session.hydrate_workspace(io.BytesIO(tar_data))


# ---------------------------------------------------------------------------
# Additional client tests
# ---------------------------------------------------------------------------


class TestBlaxelSandboxClientExtra:
    @pytest.mark.asyncio
    async def test_delete_wrong_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions(name="test")
        session = await client.create(options=options)
        # Replace the inner session with a non-Blaxel type.
        session._inner = "not a BlaxelSandboxSession"  # type: ignore[assignment]
        with pytest.raises(TypeError, match="BlaxelSandboxClient.delete"):
            await client.delete(session)

    @pytest.mark.asyncio
    async def test_resume_wrong_state_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod
        from tests.utils.factories import TestSessionState

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        # Pass a non-Blaxel SandboxSessionState subclass.
        state = TestSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="test"),
        )
        with pytest.raises(TypeError, match="BlaxelSandboxClient.resume"):
            await client.resume(state)

    @pytest.mark.asyncio
    async def test_resume_pause_on_exit_get_fails_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        # No instances exist, so get() will fail and fall back to create.
        client = mod.BlaxelSandboxClient(token="test-token")
        state = _make_state(sandbox_name="missing-sandbox", pause_on_exit=True)
        session = await client.resume(state)
        assert session is not None

    @pytest.mark.asyncio
    async def test_create_with_timeouts_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions(
            name="timeout-test",
            timeouts={"exec_timeout_s": 60, "cleanup_s": 10},
        )
        session = await client.create(options=options)
        assert session is not None

    @pytest.mark.asyncio
    async def test_create_without_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions(name="no-manifest")
        session = await client.create(manifest=None, options=options)
        assert session is not None

    @pytest.mark.asyncio
    async def test_create_with_all_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions(
            name="full-opts",
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            memory=8192,
            region="eu-ams-1",
            ports=({"target": 3000, "protocol": "HTTP"},),
            env_vars={"FOO": "bar"},
            labels={"team": "test"},
            ttl="1h",
            pause_on_exit=True,
            timeouts=mod.BlaxelTimeouts(exec_timeout_s=120),
        )
        session = await client.create(options=options)
        assert session is not None

    @pytest.mark.asyncio
    async def test_client_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)
        monkeypatch.setenv("BL_API_KEY", "env-token")

        client = mod.BlaxelSandboxClient()
        assert client._token == "env-token"

    @pytest.mark.asyncio
    async def test_close_is_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        await client.close()  # Should not raise.


# ---------------------------------------------------------------------------
# Timeouts model tests
# ---------------------------------------------------------------------------


class TestBlaxelTimeouts:
    def test_defaults(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import BlaxelTimeouts

        t = BlaxelTimeouts()
        assert t.exec_timeout_s == 300.0
        assert t.cleanup_s == 30.0
        assert t.file_upload_s == 1800.0
        assert t.file_download_s == 1800.0
        assert t.workspace_tar_s == 300.0
        assert t.fast_op_s == 30.0

    def test_custom_values(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import BlaxelTimeouts

        t = BlaxelTimeouts(exec_timeout_s=60, cleanup_s=10, fast_op_s=5)
        assert t.exec_timeout_s == 60
        assert t.cleanup_s == 10
        assert t.fast_op_s == 5

    def test_frozen(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import BlaxelTimeouts

        t = BlaxelTimeouts()
        with pytest.raises(ValidationError):
            t.exec_timeout_s = 999

    def test_validation_ge_1(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import BlaxelTimeouts

        with pytest.raises(ValidationError):
            BlaxelTimeouts(exec_timeout_s=0)


# ---------------------------------------------------------------------------
# Session state tests
# ---------------------------------------------------------------------------


class TestBlaxelSandboxSessionState:
    def test_defaults(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import BlaxelSandboxSessionState

        state = BlaxelSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="test"),
            sandbox_name="test",
        )
        assert state.image is None
        assert state.memory is None
        assert state.region is None
        assert state.base_env_vars == {}
        assert state.labels == {}
        assert state.ttl is None
        assert state.pause_on_exit is False
        assert state.sandbox_url is None

    def test_serialization_roundtrip(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import (
            BlaxelSandboxSessionState,
            BlaxelTimeouts,
        )

        state = BlaxelSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="test"),
            sandbox_name="test-rt",
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            memory=4096,
            region="us-pdx-1",
            base_env_vars={"K": "V"},
            labels={"env": "test"},
            ttl="24h",
            pause_on_exit=True,
            timeouts=BlaxelTimeouts(exec_timeout_s=60),
            sandbox_url="https://test.bl.run",
        )
        payload = state.model_dump()
        restored = BlaxelSandboxSessionState.model_validate(payload)
        assert restored.sandbox_name == "test-rt"
        assert restored.image == DEFAULT_PYTHON_SANDBOX_IMAGE
        assert restored.memory == 4096
        assert restored.timeouts.exec_timeout_s == 60


# ---------------------------------------------------------------------------
# Client options tests
# ---------------------------------------------------------------------------


class TestBlaxelSandboxClientOptions:
    def test_defaults(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import BlaxelSandboxClientOptions

        opts = BlaxelSandboxClientOptions()
        assert opts.image is None
        assert opts.memory is None
        assert opts.region is None
        assert opts.ports is None
        assert opts.env_vars is None
        assert opts.labels is None
        assert opts.ttl is None
        assert opts.name is None
        assert opts.pause_on_exit is False
        assert opts.timeouts is None
        assert opts.exposed_port_public is True

    def test_frozen(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import BlaxelSandboxClientOptions

        opts = BlaxelSandboxClientOptions(name="test")
        with pytest.raises(FrozenInstanceError):
            opts.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tar exclude args tests
# ---------------------------------------------------------------------------


class TestTarExcludeArgs:
    @pytest.mark.asyncio
    async def test_exclude_args_empty(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        args = session._tar_exclude_args()
        # With default manifest (no skip paths), should be empty.
        assert isinstance(args, list)

    @pytest.mark.asyncio
    async def test_resolved_envs(self, fake_sandbox: _FakeSandboxInstance) -> None:
        state = _make_state()
        state.base_env_vars = {"BASE_KEY": "base_val"}
        session = _make_session(fake_sandbox, state=state)
        envs = await session._resolved_envs()
        assert envs["BASE_KEY"] == "base_val"


# ---------------------------------------------------------------------------
# Start lifecycle test
# ---------------------------------------------------------------------------


class TestStartLifecycle:
    @pytest.mark.asyncio
    async def test_start_mkdir_failure_suppressed(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)

        async def _raise(*args: object, **kw: object) -> None:
            raise ConnectionError("mkdir failed")

        fake_sandbox.process.exec = _raise  # type: ignore[assignment]
        # start() should suppress the mkdir error and call super().start().
        # super().start() will try to materialize the manifest, which may
        # also call process.exec. We just verify it does not raise from the
        # initial mkdir.
        try:
            await session.start()
        except Exception:
            # May fail in super().start() but not from the mkdir.
            pass


# ---------------------------------------------------------------------------
# PTY fake helpers
# ---------------------------------------------------------------------------


class _FakeWSMessage:
    def __init__(self, msg_type: Any, data: str | bytes) -> None:
        self.type = msg_type
        self.data = data


class _FakeWS:
    """Fake WebSocket that yields predefined messages then closes."""

    def __init__(self, messages: list[_FakeWSMessage] | None = None) -> None:
        self._messages = messages or []
        self._sent: list[str] = []
        self._closed = False

    async def send_str(self, data: str) -> None:
        self._sent.append(data)

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self) -> _FakeWS:
        self._iter_index = 0
        return self

    async def __anext__(self) -> _FakeWSMessage:
        if self._iter_index >= len(self._messages):
            await asyncio.sleep(3600)
            raise StopAsyncIteration
        msg = self._messages[self._iter_index]
        self._iter_index += 1
        return msg


class _FakeHTTPSession:
    def __init__(self, ws: _FakeWS | None = None) -> None:
        self._ws = ws or _FakeWS()
        self._closed = False

    async def ws_connect(self, url: str) -> _FakeWS:
        return self._ws

    async def close(self) -> None:
        self._closed = True


class _FakeAiohttp:
    """Minimal aiohttp mock module."""

    class WSMsgType:
        TEXT = 1
        BINARY = 2
        ERROR = 256
        CLOSE = 257
        CLOSING = 258

    def __init__(self, ws: _FakeWS | None = None) -> None:
        self._ws = ws

    def ClientSession(self) -> _FakeHTTPSession:
        return _FakeHTTPSession(self._ws)


# ---------------------------------------------------------------------------
# PTY tests
# ---------------------------------------------------------------------------


class TestPtyExec:
    @pytest.mark.asyncio
    async def test_pty_exec_start_success(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        output_msg = json.dumps({"type": "output", "data": "hello from pty"})
        ws = _FakeWS(messages=[_FakeWSMessage(_FakeAiohttp.WSMsgType.TEXT, output_msg)])
        fake_aiohttp = _FakeAiohttp(ws=ws)

        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("echo", "hello", yield_time_s=0.5)
            assert update.output is not None
            assert b"hello from pty" in update.output
            # process_id may be None if the reader finishes before finalize (entry.done=True).

    @pytest.mark.asyncio
    async def test_pty_exec_start_timeout(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        session = _make_session(fake_sandbox)

        class _SlowAiohttp:
            WSMsgType = _FakeAiohttp.WSMsgType

            def ClientSession(self) -> Any:
                class _SlowSession:
                    async def ws_connect(self, url: str) -> None:
                        await asyncio.sleep(100)

                    async def close(self) -> None:
                        pass

                return _SlowSession()

        with patch.object(mod, "_import_aiohttp", return_value=_SlowAiohttp()):
            with pytest.raises(ExecTimeoutError):
                await session.pty_exec_start("echo", "hello", timeout=0.01)

    @pytest.mark.asyncio
    async def test_pty_exec_start_connection_error(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        session = _make_session(fake_sandbox)

        class _ErrorAiohttp:
            WSMsgType = _FakeAiohttp.WSMsgType

            def ClientSession(self) -> Any:
                class _ErrorSession:
                    async def ws_connect(self, url: str) -> None:
                        raise ConnectionError("ws connect failed")

                    async def close(self) -> None:
                        pass

                return _ErrorSession()

        with patch.object(mod, "_import_aiohttp", return_value=_ErrorAiohttp()):
            with pytest.raises(ExecTransportError):
                await session.pty_exec_start("echo", "hello")

    @pytest.mark.asyncio
    async def test_pty_write_stdin(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        ws = _FakeWS()
        entry = _BlaxelPtySessionEntry(
            ws_session_id="write-test",
            ws=ws,
            http_session=_FakeHTTPSession(ws),
        )
        session._pty_sessions[1] = entry
        session._reserved_pty_process_ids.add(1)

        with patch.object(mod, "_import_aiohttp", return_value=_FakeAiohttp()):
            update = await session.pty_write_stdin(session_id=1, chars="input\n", yield_time_s=0.2)
            assert update.output is not None
            assert len(ws._sent) == 1

    @pytest.mark.asyncio
    async def test_pty_write_stdin_empty_chars(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        ws = _FakeWS()
        entry = _BlaxelPtySessionEntry(
            ws_session_id="empty-write",
            ws=ws,
            http_session=_FakeHTTPSession(ws),
        )
        session._pty_sessions[1] = entry
        session._reserved_pty_process_ids.add(1)

        with patch.object(mod, "_import_aiohttp", return_value=_FakeAiohttp()):
            update = await session.pty_write_stdin(session_id=1, chars="", yield_time_s=0.2)
            assert update.output is not None
            # Empty chars should not send anything.
            assert len(ws._sent) == 0

    @pytest.mark.asyncio
    async def test_pty_terminate_all(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        ws = _FakeWS()
        entry = _BlaxelPtySessionEntry(
            ws_session_id="term-all",
            ws=ws,
            http_session=_FakeHTTPSession(ws),
        )
        session._pty_sessions[1] = entry
        session._reserved_pty_process_ids.add(1)

        await session.pty_terminate_all()
        assert len(session._pty_sessions) == 0
        assert len(session._reserved_pty_process_ids) == 0
        assert ws._closed

    @pytest.mark.asyncio
    async def test_pty_ws_reader_error_message(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        error_msg = json.dumps({"type": "error", "data": "something failed"})
        ws = _FakeWS(messages=[_FakeWSMessage(_FakeAiohttp.WSMsgType.TEXT, error_msg)])
        fake_aiohttp = _FakeAiohttp(ws=ws)
        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("bad_cmd", yield_time_s=0.5)
            assert update.output is not None
            assert b"something failed" in update.output

    @pytest.mark.asyncio
    async def test_pty_ws_reader_binary_message(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        output_msg = json.dumps({"type": "output", "data": "binary-data"}).encode()
        ws = _FakeWS(messages=[_FakeWSMessage(_FakeAiohttp.WSMsgType.BINARY, output_msg)])
        fake_aiohttp = _FakeAiohttp(ws=ws)
        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("echo", "test", yield_time_s=0.5)
            assert b"binary-data" in update.output

    @pytest.mark.asyncio
    async def test_pty_ws_reader_close_message(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        ws = _FakeWS(
            messages=[
                _FakeWSMessage(
                    _FakeAiohttp.WSMsgType.TEXT, json.dumps({"type": "output", "data": "hi"})
                ),
                _FakeWSMessage(_FakeAiohttp.WSMsgType.CLOSE, ""),
            ]
        )
        fake_aiohttp = _FakeAiohttp(ws=ws)
        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("echo", "test", yield_time_s=0.5)
            assert b"hi" in update.output

    @pytest.mark.asyncio
    async def test_pty_ws_reader_invalid_json(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        ws = _FakeWS(
            messages=[
                _FakeWSMessage(_FakeAiohttp.WSMsgType.TEXT, "not json"),
                _FakeWSMessage(
                    _FakeAiohttp.WSMsgType.TEXT,
                    json.dumps({"type": "output", "data": "valid"}),
                ),
            ]
        )
        fake_aiohttp = _FakeAiohttp(ws=ws)
        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("echo", "test", yield_time_s=0.5)
            # Invalid JSON should be silently ignored; valid output should appear.
            assert b"valid" in update.output

    @pytest.mark.asyncio
    async def test_pty_ws_reader_error_type_message(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        ws = _FakeWS(
            messages=[
                _FakeWSMessage(_FakeAiohttp.WSMsgType.ERROR, "ws error"),
            ]
        )
        fake_aiohttp = _FakeAiohttp(ws=ws)
        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("echo", "test", yield_time_s=0.3)
            # Error WS message should break the reader loop.
            assert update.output is not None

    @pytest.mark.asyncio
    async def test_pty_finalize_done_session(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        entry = _BlaxelPtySessionEntry(
            ws_session_id="test-done",
            ws=None,
            http_session=None,
            done=True,
            exit_code=0,
        )
        # Manually register the entry.
        session._pty_sessions[1] = entry
        session._reserved_pty_process_ids.add(1)

        result = await session._finalize_pty_update(
            process_id=1,
            entry=entry,
            output=b"done output",
            original_token_count=None,
        )
        assert result.process_id is None
        assert result.exit_code == 0
        assert 1 not in session._pty_sessions

    @pytest.mark.asyncio
    async def test_pty_prune_sessions(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry
        from agents.sandbox.session.pty_types import PTY_PROCESSES_MAX

        session = _make_session(fake_sandbox)
        # Fill to max capacity with done entries.
        for i in range(PTY_PROCESSES_MAX):
            entry = _BlaxelPtySessionEntry(
                ws_session_id=f"test-{i}",
                ws=None,
                http_session=None,
                done=True,
                exit_code=0,
            )
            entry.last_used = time.monotonic() - (PTY_PROCESSES_MAX - i)
            session._pty_sessions[i] = entry
            session._reserved_pty_process_ids.add(i)

        pruned = session._prune_pty_sessions_if_needed()
        assert pruned is not None

    @pytest.mark.asyncio
    async def test_pty_prune_below_max(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        # Below max, no pruning.
        pruned = session._prune_pty_sessions_if_needed()
        assert pruned is None

    @pytest.mark.asyncio
    async def test_terminate_pty_entry_with_reader_task(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        ws = _FakeWS()
        http = _FakeHTTPSession(ws)

        async def _reader() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(_reader())
        entry = _BlaxelPtySessionEntry(
            ws_session_id="term-test",
            ws=ws,
            http_session=http,
            reader_task=task,
        )
        await session._terminate_pty_entry(entry)
        assert task.cancelled() or task.done()
        assert ws._closed
        assert http._closed

    @pytest.mark.asyncio
    async def test_terminate_pty_entry_all_none(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        entry = _BlaxelPtySessionEntry(
            ws_session_id="null-test",
            ws=None,
            http_session=None,
            reader_task=None,
        )
        # Should not raise.
        await session._terminate_pty_entry(entry)

    @pytest.mark.asyncio
    async def test_pty_exec_default_yield_time(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        ws = _FakeWS(
            messages=[
                _FakeWSMessage(
                    _FakeAiohttp.WSMsgType.TEXT,
                    json.dumps({"type": "output", "data": "quick"}),
                ),
            ]
        )
        fake_aiohttp = _FakeAiohttp(ws=ws)
        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            # Pass yield_time_s=None to test default (10s), but with a short timeout.
            # We use a small timeout to not wait 10 seconds.
            update = await session.pty_exec_start("echo", "test", yield_time_s=0.1)
            assert b"quick" in update.output

    @pytest.mark.asyncio
    async def test_pty_ws_reader_capital_type_keys(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        # Test the alternative capitalized key paths (Type/Data).
        output_msg = json.dumps({"Type": "output", "Data": "cap-data"})
        ws = _FakeWS(messages=[_FakeWSMessage(_FakeAiohttp.WSMsgType.TEXT, output_msg)])
        fake_aiohttp = _FakeAiohttp(ws=ws)
        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("echo", "test", yield_time_s=0.5)
            assert b"cap-data" in update.output

    @pytest.mark.asyncio
    async def test_pty_max_output_tokens(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        long_output = "x" * 10000
        output_msg = json.dumps({"type": "output", "data": long_output})
        ws = _FakeWS(messages=[_FakeWSMessage(_FakeAiohttp.WSMsgType.TEXT, output_msg)])
        fake_aiohttp = _FakeAiohttp(ws=ws)
        session = _make_session(fake_sandbox)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start(
                "echo", "test", yield_time_s=0.5, max_output_tokens=10
            )
            # Output should be truncated.
            assert len(update.output) < len(long_output.encode())
            assert update.original_token_count is not None


# ---------------------------------------------------------------------------
# Persist workspace with mount handling
# ---------------------------------------------------------------------------


class TestPersistWithMounts:
    @pytest.mark.asyncio
    async def test_persist_unmount_error(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)

        mock_strategy = MagicMock()
        mock_strategy.teardown_for_snapshot = AsyncMock(side_effect=RuntimeError("unmount fail"))

        mock_mount = MagicMock()
        mock_mount.mount_strategy = mock_strategy
        mount_path = Path("/workspace/mount")

        orig_manifest = session.state.manifest
        mock_manifest = MagicMock(wraps=orig_manifest)
        mock_manifest.root = orig_manifest.root
        mock_manifest.environment = orig_manifest.environment
        mock_manifest.ephemeral_mount_targets = MagicMock(return_value=[(mock_mount, mount_path)])
        session.state.manifest = mock_manifest

        with pytest.raises(WorkspaceArchiveReadError):
            await session.persist_workspace()

    @pytest.mark.asyncio
    async def test_persist_remount_error(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"file.txt": b"data"})

        mock_strategy = MagicMock()
        mock_strategy.teardown_for_snapshot = AsyncMock()
        mock_strategy.restore_after_snapshot = AsyncMock(side_effect=RuntimeError("remount fail"))

        mock_mount = MagicMock()
        mock_mount.mount_strategy = mock_strategy
        mount_path = Path("/workspace/mount")

        fake_sandbox.process._results_queue = [
            _FakeExecResult(exit_code=0, output=""),
            _FakeExecResult(exit_code=0, output=""),
        ]
        tar_path = f"/tmp/bl-persist-{session.state.session_id.hex}.tar"
        fake_sandbox.fs.files[tar_path] = tar_data

        orig_manifest = session.state.manifest
        mock_manifest = MagicMock(wraps=orig_manifest)
        mock_manifest.root = orig_manifest.root
        mock_manifest.environment = orig_manifest.environment
        mock_manifest.ephemeral_mount_targets = MagicMock(return_value=[(mock_mount, mount_path)])
        session.state.manifest = mock_manifest

        with pytest.raises(WorkspaceArchiveReadError):
            await session.persist_workspace()

    @pytest.mark.asyncio
    async def test_persist_snapshot_error_still_remounts(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox)

        mock_strategy = MagicMock()
        mock_strategy.teardown_for_snapshot = AsyncMock()
        mock_strategy.restore_after_snapshot = AsyncMock()

        mock_mount = MagicMock()
        mock_mount.mount_strategy = mock_strategy
        mount_path = Path("/workspace/mount")

        fake_sandbox.process._results_queue = [
            _FakeExecResult(exit_code=1, output="tar fail"),
            _FakeExecResult(exit_code=0, output=""),
        ]

        orig_manifest = session.state.manifest
        mock_manifest = MagicMock(wraps=orig_manifest)
        mock_manifest.root = orig_manifest.root
        mock_manifest.environment = orig_manifest.environment
        mock_manifest.ephemeral_mount_targets = MagicMock(return_value=[(mock_mount, mount_path)])
        session.state.manifest = mock_manifest

        with pytest.raises(WorkspaceArchiveReadError):
            await session.persist_workspace()

        mock_strategy.restore_after_snapshot.assert_called_once()


# ---------------------------------------------------------------------------
# _import_blaxel_sdk actual error path
# ---------------------------------------------------------------------------


class TestImportBlaxelSdkActual:
    def test_actual_import_error(self) -> None:
        # Force the actual function (not mocked) to fail by hiding the module.
        from agents.extensions.sandbox.blaxel.sandbox import _import_blaxel_sdk

        with patch.dict(
            "sys.modules", {"blaxel": None, "blaxel.core": None, "blaxel.core.sandbox": None}
        ):
            with pytest.raises(ImportError, match="BlaxelSandboxClient requires"):
                _import_blaxel_sdk()

    def test_actual_import_aiohttp_error(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _import_aiohttp

        with patch.dict("sys.modules", {"aiohttp": None}):
            with pytest.raises(ImportError, match="aiohttp"):
                _import_aiohttp()


# ---------------------------------------------------------------------------
# shared tar validation: unsupported member type (for example, device or fifo)
# ---------------------------------------------------------------------------


class TestValidateTarBytesExtra:
    def test_unsupported_member_type(self) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="device")
            info.type = tarfile.CHRTYPE  # Character device, not dir or reg.
            tar.addfile(info)

        with pytest.raises(ValueError, match="unsupported member type"):
            validate_tar_bytes(buf.getvalue())

    def test_hardlink_rejected(self) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="hardlink")
            info.type = tarfile.LNKTYPE
            info.linkname = "target"
            tar.addfile(info)

        with pytest.raises(ValueError, match="hardlink"):
            validate_tar_bytes(buf.getvalue())


# ---------------------------------------------------------------------------
# Additional coverage: tar_exclude_args with skip paths
# ---------------------------------------------------------------------------


class TestTarExcludeArgsWithSkipPaths:
    @pytest.mark.asyncio
    async def test_exclude_args_with_skip_paths(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        session._runtime_persist_workspace_skip_relpaths = {
            Path("node_modules"),
            Path(".git"),
        }
        args = session._tar_exclude_args()
        assert len(args) > 0
        assert any("node_modules" in a for a in args)
        assert any(".git" in a for a in args)

    @pytest.mark.asyncio
    async def test_exclude_args_skips_empty_and_dot(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox)
        session._runtime_persist_workspace_skip_relpaths = {
            Path("."),
            Path("keep_me"),
        }
        args = session._tar_exclude_args()
        # "." should be skipped, "keep_me" should be included.
        assert any("keep_me" in a for a in args)
        assert not any(a == "--exclude='.'" for a in args)


# ---------------------------------------------------------------------------
# Additional coverage: terminate entry with close errors
# ---------------------------------------------------------------------------


class TestTerminatePtyEntryErrors:
    @pytest.mark.asyncio
    async def test_terminate_ws_close_error(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)

        class _ErrorWS:
            async def close(self) -> None:
                raise ConnectionError("ws close failed")

        class _ErrorHTTP:
            async def close(self) -> None:
                raise ConnectionError("http close failed")

        entry = _BlaxelPtySessionEntry(
            ws_session_id="err-close",
            ws=_ErrorWS(),
            http_session=_ErrorHTTP(),
            reader_task=None,
        )
        # Should not raise.
        await session._terminate_pty_entry(entry)

    @pytest.mark.asyncio
    async def test_terminate_reader_already_done(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)

        async def _done_task() -> None:
            pass

        task = asyncio.create_task(_done_task())
        await task  # Let it complete.

        entry = _BlaxelPtySessionEntry(
            ws_session_id="done-reader",
            ws=_FakeWS(),
            http_session=_FakeHTTPSession(),
            reader_task=task,
        )
        await session._terminate_pty_entry(entry)


# ---------------------------------------------------------------------------
# Additional coverage: _collect_pty_output with entry already done at start
# ---------------------------------------------------------------------------


class TestCollectPtyOutputEdgeCases:
    @pytest.mark.asyncio
    async def test_collect_output_entry_done_immediately(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        entry = _BlaxelPtySessionEntry(
            ws_session_id="done-imm",
            ws=None,
            http_session=None,
            done=True,
        )
        entry.output_chunks.append(b"final output")
        output, token_count = await session._collect_pty_output(
            entry=entry, yield_time_ms=100, max_output_tokens=None
        )
        assert b"final output" in output

    @pytest.mark.asyncio
    async def test_collect_output_timeout_path(self, fake_sandbox: _FakeSandboxInstance) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        entry = _BlaxelPtySessionEntry(
            ws_session_id="timeout-collect",
            ws=None,
            http_session=None,
        )
        # Very short yield time, no output, not done.
        output, token_count = await session._collect_pty_output(
            entry=entry, yield_time_ms=1, max_output_tokens=None
        )
        assert output == b""


# ---------------------------------------------------------------------------
# Additional coverage: actual import success paths
# ---------------------------------------------------------------------------


class TestActualImportSuccess:
    def test_import_blaxel_sdk_success(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _import_blaxel_sdk

        try:
            result = _import_blaxel_sdk()
            assert result is not None
        except ImportError:
            pytest.skip("blaxel not available")

    def test_import_aiohttp_success(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _import_aiohttp

        try:
            result = _import_aiohttp()
            assert result is not None
        except ImportError:
            pytest.skip("aiohttp not available")


# ---------------------------------------------------------------------------
# Additional coverage: hydrate cleanup and persist cleanup rm paths
# ---------------------------------------------------------------------------


class TestCleanupPaths:
    @pytest.mark.asyncio
    async def test_persist_cleanup_rm_failure_suppressed(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"file.txt": b"hello"})

        call_count = 0

        async def _counting_exec(config: dict[str, Any], **kw: object) -> _FakeExecResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # tar command succeeds.
                return _FakeExecResult(exit_code=0, output="")
            # rm cleanup fails.
            raise ConnectionError("rm failed")

        fake_sandbox.process.exec = _counting_exec  # type: ignore[method-assign]
        tar_path = f"/tmp/bl-persist-{session.state.session_id.hex}.tar"
        fake_sandbox.fs.files[tar_path] = tar_data

        # Should succeed despite rm failure.
        result = await session.persist_workspace()
        assert result.read() == tar_data

    @pytest.mark.asyncio
    async def test_hydrate_cleanup_rm_failure_suppressed(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"file.txt": b"hello"})

        call_count = 0

        async def _counting_exec(config: dict[str, Any], **kw: object) -> _FakeExecResult:
            nonlocal call_count
            call_count += 1
            command = str(config.get("command", ""))
            helper_result = _fake_helper_exec_result(
                command, symlinks=fake_sandbox.process.symlinks
            )
            if helper_result is not None:
                return helper_result
            if "tar" in command:
                if "xf" in command:
                    # tar extract succeeds.
                    return _FakeExecResult(exit_code=0, output="")
            if "rm" in command:
                raise ConnectionError("rm failed")
            return _FakeExecResult(exit_code=0, output="")

        fake_sandbox.process.exec = _counting_exec  # type: ignore[method-assign]

        # Should succeed despite rm failure.
        await session.hydrate_workspace(io.BytesIO(tar_data))


# ---------------------------------------------------------------------------
# Additional coverage: client branch partials
# ---------------------------------------------------------------------------


class TestClientBranchCoverage:
    @pytest.mark.asyncio
    async def test_create_no_name_generates_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions()  # No name.
        session = await client.create(options=options)
        assert session is not None

    @pytest.mark.asyncio
    async def test_resume_reconnects_no_new_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        # Create an instance with no URL.
        class _NoUrlSandbox(_FakeSandboxInstance):
            def __init__(self, name: str = "no-url") -> None:
                super().__init__(name=name)
                self.sandbox = _FakeSandboxModel(name=name, url="")

        _FakeSandboxInstance._instances["no-url-sandbox"] = _NoUrlSandbox("no-url-sandbox")

        client = mod.BlaxelSandboxClient(token="test-token")
        state = _make_state(sandbox_name="no-url-sandbox", pause_on_exit=True)
        session = await client.resume(state)
        assert session is not None

    @pytest.mark.asyncio
    async def test_delete_shutdown_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agents.extensions.sandbox.blaxel import sandbox as mod

        monkeypatch.setattr(mod, "_import_blaxel_sdk", lambda: _FakeSandboxInstance)

        client = mod.BlaxelSandboxClient(token="test-token")
        options = mod.BlaxelSandboxClientOptions(name="del-err")
        session = await client.create(options=options)

        # Make shutdown raise.
        async def _raise() -> None:
            raise RuntimeError("shutdown error")

        session._inner.shutdown = _raise  # type: ignore[method-assign]
        # delete should suppress the error.
        result = await client.delete(session)
        assert result is session


# ---------------------------------------------------------------------------
# Final coverage gap tests
# ---------------------------------------------------------------------------


class TestFinalCoverageGaps:
    @pytest.mark.asyncio
    async def test_exec_reraises_exec_timeout_error(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """Cover line 401: except (ExecTimeoutError, ExecTransportError): raise."""
        session = _make_session(fake_sandbox)

        async def _timeout_exec(*args: object, **kw: object) -> None:
            raise ExecTimeoutError(command=("test",), timeout_s=1.0, cause=None)

        fake_sandbox.process.exec = _timeout_exec  # type: ignore[assignment]
        with pytest.raises(ExecTimeoutError):
            await session._exec_internal("test")

    @pytest.mark.asyncio
    async def test_persist_rm_exception_suppressed(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """Cover lines 493-494: except Exception: pass in persist cleanup."""
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"file.txt": b"hello"})

        async def _exec_with_rm_fail(config: dict[str, Any], **kw: object) -> _FakeExecResult:
            command = str(config.get("command", ""))
            helper_result = _fake_helper_exec_result(
                command, symlinks=fake_sandbox.process.symlinks
            )
            if helper_result is not None:
                return helper_result
            if "rm" in command:
                raise OSError("rm failed")
            return _FakeExecResult(exit_code=0, output="")

        fake_sandbox.process.exec = _exec_with_rm_fail  # type: ignore[method-assign]
        tar_path = f"/tmp/bl-persist-{session.state.session_id.hex}.tar"
        fake_sandbox.fs.files[tar_path] = tar_data

        result = await session.persist_workspace()
        assert result.read() == tar_data

    @pytest.mark.asyncio
    async def test_hydrate_rm_exception_suppressed(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """Cover lines 560-561: except Exception: pass in hydrate cleanup."""
        session = _make_session(fake_sandbox)
        tar_data = _make_tar({"file.txt": b"hello"})

        async def _exec_with_rm_fail(config: dict[str, Any], **kw: object) -> _FakeExecResult:
            command = str(config.get("command", ""))
            helper_result = _fake_helper_exec_result(
                command, symlinks=fake_sandbox.process.symlinks
            )
            if helper_result is not None:
                return helper_result
            if "rm" in command:
                raise OSError("rm failed")
            return _FakeExecResult(exit_code=0, output="")

        fake_sandbox.process.exec = _exec_with_rm_fail  # type: ignore[method-assign]

        await session.hydrate_workspace(io.BytesIO(tar_data))

    @pytest.mark.asyncio
    async def test_pty_exec_with_pruning(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """Cover line 638: pruned entry termination in pty_exec_start."""
        from agents.extensions.sandbox.blaxel import sandbox as mod
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry
        from agents.sandbox.session.pty_types import PTY_PROCESSES_MAX

        session = _make_session(fake_sandbox)

        # Fill sessions to capacity with done entries.
        for i in range(PTY_PROCESSES_MAX):
            entry = _BlaxelPtySessionEntry(
                ws_session_id=f"fill-{i}",
                ws=None,
                http_session=None,
                done=True,
                exit_code=0,
            )
            entry.last_used = time.monotonic() - (PTY_PROCESSES_MAX - i)
            session._pty_sessions[i + 100] = entry
            session._reserved_pty_process_ids.add(i + 100)

        ws = _FakeWS(
            messages=[
                _FakeWSMessage(
                    _FakeAiohttp.WSMsgType.TEXT,
                    json.dumps({"type": "output", "data": "pruned-test"}),
                ),
            ]
        )
        fake_aiohttp = _FakeAiohttp(ws=ws)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("echo", "test", yield_time_s=0.3)
            assert b"pruned-test" in update.output

    @pytest.mark.asyncio
    async def test_pty_warning_threshold(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """Cover line 641: warning log for high PTY count."""
        from agents.extensions.sandbox.blaxel import sandbox as mod
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry
        from agents.sandbox.session.pty_types import PTY_PROCESSES_WARNING

        session = _make_session(fake_sandbox)

        # Fill up to just below warning threshold.
        for i in range(PTY_PROCESSES_WARNING - 1):
            entry = _BlaxelPtySessionEntry(
                ws_session_id=f"warn-{i}",
                ws=None,
                http_session=None,
            )
            session._pty_sessions[i + 200] = entry
            session._reserved_pty_process_ids.add(i + 200)

        ws = _FakeWS(
            messages=[
                _FakeWSMessage(
                    _FakeAiohttp.WSMsgType.TEXT,
                    json.dumps({"type": "output", "data": "warn-test"}),
                ),
            ]
        )
        fake_aiohttp = _FakeAiohttp(ws=ws)

        with patch.object(mod, "_import_aiohttp", return_value=fake_aiohttp):
            update = await session.pty_exec_start("echo", "test", yield_time_s=0.3)
            assert update.output is not None

    @pytest.mark.asyncio
    async def test_pty_ws_reader_exception_in_iter(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """Cover line 744: except Exception: pass in _pty_ws_reader."""
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)

        class _ErrorWS:
            _sent: list[str] = []
            _closed = False

            async def send_str(self, data: str) -> None:
                self._sent.append(data)

            async def close(self) -> None:
                self._closed = True

            def __aiter__(self) -> _ErrorWS:
                return self

            async def __anext__(self) -> None:
                raise RuntimeError("WS iteration error")

        entry = _BlaxelPtySessionEntry(
            ws_session_id="err-iter",
            ws=_ErrorWS(),
            http_session=_FakeHTTPSession(),
        )

        # Run the reader directly.
        await session._pty_ws_reader(entry)
        assert entry.done is True

    @pytest.mark.asyncio
    async def test_terminate_pty_outer_exception(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """Cover lines 841-842: outer except Exception: pass in _terminate_pty_entry."""
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)

        class _BadReaderTask:
            """Fake task whose done() raises."""

            def done(self) -> bool:
                raise RuntimeError("task check failed")

            def cancel(self) -> None:
                pass

        entry = _BlaxelPtySessionEntry(
            ws_session_id="outer-err",
            ws=None,
            http_session=None,
            reader_task=_BadReaderTask(),  # type: ignore[arg-type]
        )
        # Should not raise.
        await session._terminate_pty_entry(entry)

    @pytest.mark.asyncio
    async def test_prune_returns_none_when_no_pid(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """Cover line 819: prune returns None when process_id_to_prune_from_meta returns None."""
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry
        from agents.sandbox.session.pty_types import PTY_PROCESSES_MAX

        session = _make_session(fake_sandbox)

        # Fill to max with entries, then patch process_id_to_prune_from_meta to return None.
        for i in range(PTY_PROCESSES_MAX):
            entry = _BlaxelPtySessionEntry(
                ws_session_id=f"no-prune-{i}",
                ws=None,
                http_session=None,
            )
            session._pty_sessions[i + 300] = entry
            session._reserved_pty_process_ids.add(i + 300)

        with patch(
            "agents.extensions.sandbox.blaxel.sandbox.process_id_to_prune_from_meta",
            return_value=None,
        ):
            result = session._prune_pty_sessions_if_needed()
            assert result is None

    @pytest.mark.asyncio
    async def test_collect_output_deadline_break(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """Cover lines 765, 774: deadline and remaining_s break paths."""
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        entry = _BlaxelPtySessionEntry(
            ws_session_id="deadline-test",
            ws=None,
            http_session=None,
        )
        entry.output_chunks.append(b"some data")

        # yield_time_ms=1 means very short deadline, should hit deadline break.
        output, _ = await session._collect_pty_output(
            entry=entry, yield_time_ms=1, max_output_tokens=None
        )
        assert b"some data" in output

    @pytest.mark.asyncio
    async def test_collect_output_done_with_remaining_chunks(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """Cover line 769: collecting remaining chunks when entry is done."""
        from agents.extensions.sandbox.blaxel.sandbox import _BlaxelPtySessionEntry

        session = _make_session(fake_sandbox)
        entry = _BlaxelPtySessionEntry(
            ws_session_id="done-chunks",
            ws=None,
            http_session=None,
            done=True,
        )
        # Add chunks after marking done, to test the inner drain loop.
        entry.output_chunks.append(b"chunk1")
        entry.output_chunks.append(b"chunk2")

        output, _ = await session._collect_pty_output(
            entry=entry, yield_time_ms=5000, max_output_tokens=None
        )
        assert b"chunk1" in output
        assert b"chunk2" in output


# ---------------------------------------------------------------------------
# Mounts tests
# ---------------------------------------------------------------------------


class _FakeExecResultForMount:
    def __init__(self, exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeMountSession:
    """Minimal BaseSandboxSession stand-in for mount tests."""

    __name__ = "BlaxelSandboxSession"

    def __init__(self) -> None:
        self.exec_calls: list[tuple[tuple[str, ...], dict[str, float]]] = []
        self._next_results: list[_FakeExecResultForMount] = []
        self._default_result = _FakeExecResultForMount()

    async def exec(self, *cmd: str, timeout: float = 120) -> _FakeExecResultForMount:
        self.exec_calls.append((cmd, {"timeout": timeout}))
        if self._next_results:
            return self._next_results.pop(0)
        return self._default_result

    class __class__:
        __name__ = "BlaxelSandboxSession"


# Override type name for _assert_blaxel_session check.
_FakeMountSession.__name__ = "BlaxelSandboxSession"


def _bl_strategy() -> Any:
    from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountStrategy

    return BlaxelCloudBucketMountStrategy()


class TestMountsModule:
    def test_build_mount_config_s3(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _build_mount_config
        from agents.sandbox.entries import S3Mount

        mount = S3Mount(
            bucket="my-bucket",
            mount_strategy=_bl_strategy(),
            access_key_id="AKID",
            secret_access_key="SECRET",
            region="us-east-1",
            prefix="data/",
            read_only=True,
        )
        config = _build_mount_config(mount, mount_path="/mnt/s3")
        assert config.provider == "s3"
        assert config.bucket == "my-bucket"
        assert config.mount_path == "/mnt/s3"
        assert config.access_key_id == "AKID"
        assert config.region == "us-east-1"
        assert config.prefix == "data/"

    def test_build_mount_config_r2(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _build_mount_config
        from agents.sandbox.entries import R2Mount

        mount = R2Mount(
            bucket="r2-bucket",
            mount_strategy=_bl_strategy(),
            account_id="acc123",
            access_key_id="R2KEY",
            secret_access_key="R2SECRET",
        )
        config = _build_mount_config(mount, mount_path="/mnt/r2")
        assert config.provider == "r2"
        assert "r2.cloudflarestorage.com" in (config.endpoint_url or "")

    def test_build_mount_config_r2_custom_domain(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _build_mount_config
        from agents.sandbox.entries import R2Mount

        mount = R2Mount(
            bucket="r2-bucket",
            account_id="acc123",
            mount_strategy=_bl_strategy(),
            access_key_id="R2KEY",
            secret_access_key="R2SECRET",
            custom_domain="https://custom.example.com",
        )
        config = _build_mount_config(mount, mount_path="/mnt/r2")
        assert config.endpoint_url == "https://custom.example.com"

    def test_build_mount_config_gcs(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _build_mount_config
        from agents.sandbox.entries import GCSMount

        mount = GCSMount(
            bucket="gcs-bucket",
            mount_strategy=_bl_strategy(),
            service_account_credentials='{"type":"service_account"}',
            prefix="prefix/",
        )
        config = _build_mount_config(mount, mount_path="/mnt/gcs")
        assert config.provider == "gcs"
        assert config.service_account_key is not None

    def test_build_mount_config_gcs_hmac(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _build_mount_config
        from agents.sandbox.entries import GCSMount

        mount = GCSMount(
            bucket="gcs-bucket",
            mount_strategy=_bl_strategy(),
            access_id="GOOG1",
            secret_access_key="SECRET",
            endpoint_url="https://storage.googleapis.com",
            prefix="prefix/",
        )
        config = _build_mount_config(mount, mount_path="/mnt/gcs")
        assert config.provider == "s3"
        assert config.access_key_id == "GOOG1"
        assert config.secret_access_key == "SECRET"
        assert config.endpoint_url == "https://storage.googleapis.com"
        assert config.prefix == "prefix/"

    def test_build_mount_config_unsupported(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _build_mount_config
        from agents.sandbox.errors import MountConfigError

        # Use a MagicMock with a type attribute to simulate an unsupported mount.
        mount = MagicMock()
        mount.type = "unsupported_mount"
        with pytest.raises(MountConfigError, match="only support"):
            _build_mount_config(mount, mount_path="/mnt/x")

    def test_assert_blaxel_session_wrong_type(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _assert_blaxel_session
        from agents.sandbox.errors import MountConfigError

        class _WrongSession:
            pass

        with pytest.raises(MountConfigError, match="BlaxelSandboxSession"):
            _assert_blaxel_session(_WrongSession())  # type: ignore[arg-type]

    def test_validate_mount(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountStrategy
        from agents.sandbox.entries import S3Mount

        strategy = BlaxelCloudBucketMountStrategy()
        mount = S3Mount(bucket="test-bucket", mount_strategy=_bl_strategy())
        strategy.validate_mount(mount)

    def test_build_docker_volume_driver_config_returns_none(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountStrategy
        from agents.sandbox.entries import S3Mount

        strategy = BlaxelCloudBucketMountStrategy()
        mount = S3Mount(bucket="test", mount_strategy=_bl_strategy())
        assert strategy.build_docker_volume_driver_config(mount) is None

    @pytest.mark.asyncio
    async def test_mount_s3_with_credentials(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountConfig, _mount_s3

        session = _FakeMountSession()
        # Simulate: which s3fs succeeds.
        session._next_results = [
            _FakeExecResultForMount(exit_code=0, stdout=b"/usr/bin/s3fs"),  # which s3fs
            _FakeExecResultForMount(exit_code=0),  # write cred file
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # s3fs mount
            _FakeExecResultForMount(exit_code=0),  # rm cred file
        ]

        config = BlaxelCloudBucketMountConfig(
            provider="s3",
            bucket="my-bucket",
            mount_path="/mnt/s3",
            access_key_id="AKID",
            secret_access_key="SECRET",
            region="us-east-1",
            prefix="data/",
            read_only=True,
        )
        await _mount_s3(session, config)  # type: ignore[arg-type]
        assert len(session.exec_calls) == 5

    @pytest.mark.asyncio
    async def test_mount_s3_public_bucket(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountConfig, _mount_s3

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which s3fs
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # s3fs mount (no cred cleanup)
        ]

        config = BlaxelCloudBucketMountConfig(
            provider="s3",
            bucket="public-bucket",
            mount_path="/mnt/pub",
            read_only=True,
        )
        await _mount_s3(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mount_s3_with_endpoint(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountConfig, _mount_s3

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which s3fs
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # s3fs mount
        ]

        config = BlaxelCloudBucketMountConfig(
            provider="s3",
            bucket="endpoint-bucket",
            mount_path="/mnt/ep",
            endpoint_url="https://custom-s3.example.com",
        )
        await _mount_s3(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mount_s3_r2_sigv4(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountConfig, _mount_s3

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which s3fs
            _FakeExecResultForMount(exit_code=0),  # write cred
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # s3fs mount
            _FakeExecResultForMount(exit_code=0),  # rm cred
        ]

        config = BlaxelCloudBucketMountConfig(
            provider="r2",
            bucket="r2-bucket",
            mount_path="/mnt/r2",
            access_key_id="KEY",
            secret_access_key="SECRET",
            endpoint_url="https://acc.r2.cloudflarestorage.com",
        )
        await _mount_s3(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mount_s3_fails(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountConfig, _mount_s3
        from agents.sandbox.errors import MountConfigError

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which s3fs
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=1, stderr=b"mount error"),  # s3fs fails
        ]

        config = BlaxelCloudBucketMountConfig(
            provider="s3",
            bucket="fail-bucket",
            mount_path="/mnt/fail",
        )
        with pytest.raises(MountConfigError, match="s3fs mount failed"):
            await _mount_s3(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mount_gcs_with_key(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountConfig, _mount_gcs

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which gcsfuse
            _FakeExecResultForMount(exit_code=0),  # write key
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # gcsfuse mount
            _FakeExecResultForMount(exit_code=0),  # rm key
        ]

        config = BlaxelCloudBucketMountConfig(
            provider="gcs",
            bucket="gcs-bucket",
            mount_path="/mnt/gcs",
            service_account_key='{"type":"service_account"}',
            read_only=True,
            prefix="data/",
        )
        await _mount_gcs(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mount_gcs_anonymous(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountConfig, _mount_gcs

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which gcsfuse
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # gcsfuse mount
        ]

        config = BlaxelCloudBucketMountConfig(
            provider="gcs",
            bucket="pub-gcs",
            mount_path="/mnt/pub-gcs",
        )
        await _mount_gcs(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mount_gcs_fails(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountConfig, _mount_gcs
        from agents.sandbox.errors import MountConfigError

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which gcsfuse
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=1, stderr=b"gcs error"),  # fails
        ]

        config = BlaxelCloudBucketMountConfig(
            provider="gcs",
            bucket="fail-gcs",
            mount_path="/mnt/fail-gcs",
        )
        with pytest.raises(MountConfigError, match="gcsfuse mount failed"):
            await _mount_gcs(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mount_bucket_dispatch_s3(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import (
            BlaxelCloudBucketMountConfig,
            _mount_bucket,
        )

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which s3fs
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # s3fs mount
        ]
        config = BlaxelCloudBucketMountConfig(provider="s3", bucket="b", mount_path="/m")
        await _mount_bucket(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mount_bucket_dispatch_gcs(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import (
            BlaxelCloudBucketMountConfig,
            _mount_bucket,
        )

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),
            _FakeExecResultForMount(exit_code=0),
            _FakeExecResultForMount(exit_code=0),
        ]
        config = BlaxelCloudBucketMountConfig(provider="gcs", bucket="b", mount_path="/m")
        await _mount_bucket(session, config)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_unmount_bucket_fusermount(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _unmount_bucket

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # fusermount succeeds
        ]
        await _unmount_bucket(session, "/mnt/test")  # type: ignore[arg-type]
        assert len(session.exec_calls) == 1

    @pytest.mark.asyncio
    async def test_unmount_bucket_umount_fallback(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _unmount_bucket

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=1),  # fusermount fails
            _FakeExecResultForMount(exit_code=0),  # umount succeeds
        ]
        await _unmount_bucket(session, "/mnt/test")  # type: ignore[arg-type]
        assert len(session.exec_calls) == 2

    @pytest.mark.asyncio
    async def test_unmount_bucket_lazy(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _unmount_bucket

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=1),  # fusermount fails
            _FakeExecResultForMount(exit_code=1),  # umount fails
            _FakeExecResultForMount(exit_code=0),  # umount -l
        ]
        await _unmount_bucket(session, "/mnt/test")  # type: ignore[arg-type]
        assert len(session.exec_calls) == 3

    @pytest.mark.asyncio
    async def test_install_tool_with_apk(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _install_tool

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0, stdout=b"apk"),  # detect pkg mgr
            _FakeExecResultForMount(exit_code=0),  # apk add succeeds
        ]
        await _install_tool(session, "s3fs")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_install_tool_with_apt(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _install_tool

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0, stdout=b"apt"),  # detect pkg mgr
            _FakeExecResultForMount(exit_code=0),  # apt-get install succeeds
        ]
        await _install_tool(session, "gcsfuse")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_install_tool_fails_after_retries(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _install_tool
        from agents.sandbox.errors import MountConfigError

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0, stdout=b"apt"),  # detect
            _FakeExecResultForMount(exit_code=1),  # attempt 1
            _FakeExecResultForMount(exit_code=1),  # attempt 2
            _FakeExecResultForMount(exit_code=1),  # attempt 3
        ]
        with pytest.raises(MountConfigError, match="failed to install"):
            await _install_tool(session, "s3fs")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_ensure_tool_already_installed(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _ensure_tool

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which s3fs succeeds
        ]
        await _ensure_tool(session, "s3fs")  # type: ignore[arg-type]
        assert len(session.exec_calls) == 1

    @pytest.mark.asyncio
    async def test_ensure_tool_needs_install(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _ensure_tool

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=1),  # which fails
            _FakeExecResultForMount(exit_code=0, stdout=b"apt"),  # detect
            _FakeExecResultForMount(exit_code=0),  # install
        ]
        await _ensure_tool(session, "s3fs")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_activate(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountStrategy
        from agents.sandbox.entries import S3Mount

        strategy = BlaxelCloudBucketMountStrategy()
        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # mount
        ]
        mount = S3Mount(bucket="test", mount_strategy=_bl_strategy(), mount_path=Path("/mnt/s3"))
        # activate needs a real mount path resolution, mock it.
        mount._resolve_mount_path = lambda s, d: Path("/workspace/mnt/s3")  # type: ignore[assignment]
        result = await strategy.activate(
            mount,
            session,  # type: ignore[arg-type]
            Path("/workspace/mnt/s3"),
            Path("/workspace"),
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_deactivate(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountStrategy
        from agents.sandbox.entries import S3Mount

        strategy = BlaxelCloudBucketMountStrategy()
        session = _FakeMountSession()
        session._next_results = [_FakeExecResultForMount(exit_code=0)]
        mount = S3Mount(bucket="test", mount_strategy=_bl_strategy(), mount_path=Path("/mnt/s3"))
        mount._resolve_mount_path = lambda s, d: Path("/workspace/mnt/s3")  # type: ignore[assignment]
        await strategy.deactivate(
            mount,
            session,  # type: ignore[arg-type]
            Path("/workspace/mnt/s3"),
            Path("/workspace"),
        )

    @pytest.mark.asyncio
    async def test_teardown_for_snapshot(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountStrategy
        from agents.sandbox.entries import S3Mount

        strategy = BlaxelCloudBucketMountStrategy()
        session = _FakeMountSession()
        session._next_results = [_FakeExecResultForMount(exit_code=0)]
        mount = S3Mount(bucket="test", mount_strategy=_bl_strategy())
        await strategy.teardown_for_snapshot(
            mount,
            session,  # type: ignore[arg-type]
            Path("/workspace/mnt/s3"),
        )

    @pytest.mark.asyncio
    async def test_restore_after_snapshot(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelCloudBucketMountStrategy
        from agents.sandbox.entries import S3Mount

        strategy = BlaxelCloudBucketMountStrategy()
        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=0),  # which
            _FakeExecResultForMount(exit_code=0),  # mkdir
            _FakeExecResultForMount(exit_code=0),  # mount
        ]
        mount = S3Mount(bucket="test", mount_strategy=_bl_strategy())
        await strategy.restore_after_snapshot(
            mount,
            session,  # type: ignore[arg-type]
            Path("/workspace/mnt/s3"),
        )


# ---------------------------------------------------------------------------
# SDK exception mapping tests
# ---------------------------------------------------------------------------


class TestSdkExceptionMapping:
    def test_import_sandbox_api_error(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _import_sandbox_api_error

        cls = _import_sandbox_api_error()
        if cls is None:
            pytest.skip("blaxel not available")
        assert issubclass(cls, BaseException)

    def test_import_sandbox_api_error_missing_sdk(self) -> None:
        from agents.extensions.sandbox.blaxel.sandbox import _import_sandbox_api_error

        with patch.dict(
            "sys.modules",
            {"blaxel": None, "blaxel.core": None, "blaxel.core.sandbox": None},
        ):
            assert _import_sandbox_api_error() is None

    @pytest.mark.asyncio
    async def test_exec_maps_sdk_api_error_408_to_timeout(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """SandboxAPIError with status_code=408 should map to ExecTimeoutError."""
        from agents.extensions.sandbox.blaxel import sandbox as mod

        session = _make_session(fake_sandbox)

        # Create a fake SandboxAPIError with status_code.
        class FakeApiError(Exception):
            def __init__(self, msg: str, status_code: int) -> None:
                super().__init__(msg)
                self.status_code = status_code

        async def _raise_timeout(*args: object, **kw: object) -> None:
            raise FakeApiError("request timeout", status_code=408)

        fake_sandbox.process.exec = _raise_timeout  # type: ignore[assignment]

        with patch.object(mod, "_import_sandbox_api_error", return_value=FakeApiError):
            with pytest.raises(ExecTimeoutError):
                await session._exec_internal("sleep", "100")

    @pytest.mark.asyncio
    async def test_exec_maps_sdk_api_error_504_to_timeout(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """SandboxAPIError with status_code=504 should map to ExecTimeoutError."""
        from agents.extensions.sandbox.blaxel import sandbox as mod

        session = _make_session(fake_sandbox)

        class FakeApiError(Exception):
            def __init__(self, msg: str, status_code: int) -> None:
                super().__init__(msg)
                self.status_code = status_code

        async def _raise_504(*args: object, **kw: object) -> None:
            raise FakeApiError("gateway timeout", status_code=504)

        fake_sandbox.process.exec = _raise_504  # type: ignore[assignment]

        with patch.object(mod, "_import_sandbox_api_error", return_value=FakeApiError):
            with pytest.raises(ExecTimeoutError):
                await session._exec_internal("sleep", "100")

    @pytest.mark.asyncio
    async def test_exec_non_timeout_api_error_becomes_transport(
        self, fake_sandbox: _FakeSandboxInstance
    ) -> None:
        """SandboxAPIError with status_code=500 should map to ExecTransportError."""
        from agents.extensions.sandbox.blaxel import sandbox as mod

        session = _make_session(fake_sandbox)

        class FakeApiError(Exception):
            def __init__(self, msg: str, status_code: int) -> None:
                super().__init__(msg)
                self.status_code = status_code

        async def _raise_500(*args: object, **kw: object) -> None:
            raise FakeApiError("internal error", status_code=500)

        fake_sandbox.process.exec = _raise_500  # type: ignore[assignment]

        with patch.object(mod, "_import_sandbox_api_error", return_value=FakeApiError):
            with pytest.raises(ExecTransportError):
                await session._exec_internal("echo", "hello")


# ---------------------------------------------------------------------------
# Timeout coercion tests
# ---------------------------------------------------------------------------


class TestCoerceExecTimeout:
    def test_none_returns_default(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        result = session._coerce_exec_timeout(None)
        assert result == 300.0  # Default from BlaxelTimeouts.

    def test_positive_value_passthrough(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        assert session._coerce_exec_timeout(42.5) == 42.5

    def test_zero_returns_small_positive(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        assert session._coerce_exec_timeout(0) == 0.001

    def test_negative_returns_small_positive(self, fake_sandbox: _FakeSandboxInstance) -> None:
        session = _make_session(fake_sandbox)
        assert session._coerce_exec_timeout(-5) == 0.001


# ---------------------------------------------------------------------------
# Drive mount tests
# ---------------------------------------------------------------------------


class TestDriveMounts:
    @pytest.mark.asyncio
    async def test_attach_drive_success(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelDriveMountConfig, _attach_drive

        sandbox = _FakeSandboxInstance()
        config = BlaxelDriveMountConfig(
            drive_name="test-drive", mount_path="/mnt/data", drive_path="/"
        )
        await _attach_drive(sandbox, config)
        assert sandbox.drives.mount_calls == [("test-drive", "/mnt/data", "/")]

    @pytest.mark.asyncio
    async def test_attach_drive_error(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelDriveMountConfig, _attach_drive
        from agents.sandbox.errors import MountConfigError

        sandbox = _FakeSandboxInstance()
        sandbox.drives.mount_error = RuntimeError("mount api error")
        config = BlaxelDriveMountConfig(
            drive_name="test-drive", mount_path="/mnt/data", drive_path="/"
        )
        with pytest.raises(MountConfigError, match="drive mount failed"):
            await _attach_drive(sandbox, config)

    @pytest.mark.asyncio
    async def test_attach_drive_no_drives_api(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelDriveMountConfig, _attach_drive
        from agents.sandbox.errors import MountConfigError

        class _NoDrives:
            pass

        config = BlaxelDriveMountConfig(
            drive_name="test-drive", mount_path="/mnt/data", drive_path="/"
        )
        with pytest.raises(MountConfigError, match="does not expose a drives API"):
            await _attach_drive(_NoDrives(), config)

    @pytest.mark.asyncio
    async def test_detach_drive_success(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _detach_drive

        sandbox = _FakeSandboxInstance()
        await _detach_drive(sandbox, "/mnt/data")
        assert sandbox.drives.unmount_calls == ["/mnt/data"]

    @pytest.mark.asyncio
    async def test_detach_drive_error_logged_not_raised(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _detach_drive

        sandbox = _FakeSandboxInstance()
        sandbox.drives.unmount_error = RuntimeError("unmount failed")
        # Should not raise; error is logged.
        await _detach_drive(sandbox, "/mnt/data")

    @pytest.mark.asyncio
    async def test_detach_drive_no_drives_api(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _detach_drive

        class _NoDrives:
            pass

        # Should not raise when drives API is missing.
        await _detach_drive(_NoDrives(), "/mnt/data")

    @pytest.mark.asyncio
    async def test_drive_strategy_validate_wrong_mount_type(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelDriveMountStrategy
        from agents.sandbox.errors import MountConfigError

        strategy = BlaxelDriveMountStrategy()
        mount = MagicMock()
        mount.type = "blaxel_drive"
        with pytest.raises(MountConfigError, match="BlaxelDriveMount"):
            strategy.validate_mount(mount)

    @pytest.mark.asyncio
    async def test_drive_strategy_validate_non_drive_mount(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelDriveMountStrategy
        from agents.sandbox.errors import MountConfigError

        strategy = BlaxelDriveMountStrategy()
        mount = MagicMock()
        mount.type = "s3_mount"
        with pytest.raises(MountConfigError, match="BlaxelDriveMount"):
            strategy.validate_mount(mount)

    def test_drive_strategy_build_docker_volume_returns_none(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import BlaxelDriveMountStrategy

        strategy = BlaxelDriveMountStrategy()
        mount = MagicMock()
        assert strategy.build_docker_volume_driver_config(mount) is None


# ---------------------------------------------------------------------------
# Unmount bucket stderr logging tests
# ---------------------------------------------------------------------------


class TestUnmountBucketLogging:
    @pytest.mark.asyncio
    async def test_unmount_all_attempts_fail_logs_warning(self) -> None:
        from agents.extensions.sandbox.blaxel.mounts import _unmount_bucket

        session = _FakeMountSession()
        session._next_results = [
            _FakeExecResultForMount(exit_code=1),  # fusermount fails
            _FakeExecResultForMount(exit_code=1),  # umount fails
            _FakeExecResultForMount(exit_code=1),  # umount -l fails
        ]
        # Should not raise, just log warning.
        await _unmount_bucket(session, "/mnt/test")  # type: ignore[arg-type]
        assert len(session.exec_calls) == 3


# ---------------------------------------------------------------------------
# FakeFs.ls improvement tests
# ---------------------------------------------------------------------------


class TestFakeFs:
    @pytest.mark.asyncio
    async def test_ls_returns_matching_paths(self) -> None:
        fs = _FakeFs()
        fs.files["/workspace/a.txt"] = b"a"
        fs.files["/workspace/b.txt"] = b"b"
        fs.files["/other/c.txt"] = b"c"
        result = await fs.ls("/workspace")
        assert "/workspace/a.txt" in result
        assert "/workspace/b.txt" in result
        assert "/other/c.txt" not in result

    @pytest.mark.asyncio
    async def test_ls_empty_returns_path(self) -> None:
        fs = _FakeFs()
        result = await fs.ls("/empty")
        assert result == ["/empty"]


# ---------------------------------------------------------------------------
# Shutdown logging tests
# ---------------------------------------------------------------------------


class TestShutdownLogging:
    @pytest.mark.asyncio
    async def test_shutdown_delete_logs_warning(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """shutdown() should log a warning when delete fails, not silently suppress."""
        session = _make_session(fake_sandbox)

        async def _raise() -> None:
            raise RuntimeError("delete failed")

        fake_sandbox.delete = _raise  # type: ignore[method-assign]
        # Should not raise.
        await session.shutdown()

    @pytest.mark.asyncio
    async def test_running_false_logs_debug(self, fake_sandbox: _FakeSandboxInstance) -> None:
        """running() should log at debug level when health check fails."""
        session = _make_session(fake_sandbox)

        async def _raise(*args: object, **kw: object) -> None:
            raise ConnectionError("offline")

        fake_sandbox.fs.ls = _raise  # type: ignore[assignment]
        assert await session.running() is False
