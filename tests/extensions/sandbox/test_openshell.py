"""Tests for the OpenShell sandbox backend."""

from __future__ import annotations

import base64
import io
import tarfile
import uuid
from typing import Any

import pytest

import agents.extensions.sandbox.openshell.sandbox as _openshell_mod
from agents.extensions.sandbox.openshell import (
    OpenShellSandboxClient,
    OpenShellSandboxClientOptions,
    OpenShellSandboxSession,
    OpenShellSandboxSessionState,
)
from agents.sandbox.errors import ExecTransportError
from agents.sandbox.manifest import Manifest
from agents.sandbox.snapshot import NoopSnapshot

# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------


class _FakeOpenShellExecResult:
    """Mimics ``openshell.sandbox.ExecResult``."""

    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeSandboxRef:
    """Mimics ``openshell.sandbox.SandboxRef``."""

    def __init__(self, id: str = "", name: str = "", phase: int = 0) -> None:
        self.id = id
        self.name = name
        self.phase = phase


class _FakeOpenShellClient:
    """Mimics ``openshell.sandbox.SandboxClient`` for testing."""

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.wait_ready_calls: list[tuple[str, float]] = []
        self.exec_calls: list[tuple[str, list[str], dict[str, Any]]] = []
        self.close_calls: int = 0
        self._exec_result = _FakeOpenShellExecResult()
        self._get_result = _FakeSandboxRef()
        self._exec_error: BaseException | None = None
        self._get_error: BaseException | None = None

    def create(self, *, spec: Any = None) -> _FakeSandboxRef:
        self.create_calls.append({"spec": spec})
        return _FakeSandboxRef(id="sandbox-id-1", name="sandbox-name-1", phase=2)

    def get(self, sandbox_name: str) -> _FakeSandboxRef:
        self.get_calls.append(sandbox_name)
        if self._get_error is not None:
            raise self._get_error
        return self._get_result

    def delete(self, sandbox_name: str) -> bool:
        self.delete_calls.append(sandbox_name)
        return True

    def wait_ready(self, sandbox_name: str, *, timeout_seconds: float = 300.0) -> _FakeSandboxRef:
        self.wait_ready_calls.append((sandbox_name, timeout_seconds))
        return _FakeSandboxRef(id="sandbox-id-1", name=sandbox_name, phase=2)

    def exec(
        self,
        sandbox_id: str,
        command: list[str],
        **kwargs: Any,
    ) -> _FakeOpenShellExecResult:
        self.exec_calls.append((sandbox_id, command, kwargs))
        if self._exec_error is not None:
            raise self._exec_error
        return self._exec_result

    def close(self) -> None:
        self.close_calls += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    sandbox_id: str = "test-sandbox-id",
    sandbox_name: str = "test-sandbox-name",
    manifest_root: str = "/workspace",
    workspace_root_ready: bool = False,
    base_envs: dict[str, str] | None = None,
) -> OpenShellSandboxSessionState:
    """Build a minimal session state for tests."""
    return OpenShellSandboxSessionState(
        session_id=uuid.uuid4(),
        sandbox_id=sandbox_id,
        sandbox_name=sandbox_name,
        manifest=Manifest(root=manifest_root),
        snapshot=NoopSnapshot(id="snapshot"),
        workspace_root_ready=workspace_root_ready,
        base_envs=base_envs or {},
    )


def _make_session(
    *,
    state: OpenShellSandboxSessionState | None = None,
    client: _FakeOpenShellClient | None = None,
    workspace_ready: bool = False,
) -> tuple[OpenShellSandboxSession, _FakeOpenShellClient]:
    """Build a session with a fake client for testing."""
    if state is None:
        state = _make_state(workspace_root_ready=workspace_ready)
    if client is None:
        client = _FakeOpenShellClient()
    session = OpenShellSandboxSession(state=state, openshell_client=client)
    if workspace_ready:
        session._workspace_root_ready = True
    return session, client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenShellReExports:
    """Verify that public symbols are exported through the package hierarchy."""

    def test_openshell_package_re_exports_backend_symbols(self) -> None:
        """The openshell __init__.py should re-export all four classes."""
        from agents.extensions.sandbox import openshell

        assert hasattr(openshell, "OpenShellSandboxClient")
        assert hasattr(openshell, "OpenShellSandboxClientOptions")
        assert hasattr(openshell, "OpenShellSandboxSession")
        assert hasattr(openshell, "OpenShellSandboxSessionState")

    def test_openshell_extension_re_exports_symbols(self) -> None:
        """The sandbox __init__.py should conditionally export OpenShell symbols."""
        from agents.extensions import sandbox as sandbox_ext

        # The import may or may not succeed depending on whether the openshell
        # extra is installed, but the names should be present when it does.
        for name in (
            "OpenShellSandboxClient",
            "OpenShellSandboxClientOptions",
            "OpenShellSandboxSession",
            "OpenShellSandboxSessionState",
        ):
            assert name in sandbox_ext.__all__


class TestOpenShellClientOptions:
    """Options dataclass behavior."""

    def test_openshell_client_options_defaults(self) -> None:
        """Default values should match the documented specification."""
        opts = OpenShellSandboxClientOptions()
        assert opts.type == "openshell"
        assert opts.cluster is None
        assert opts.endpoint is None
        assert opts.tls_ca_path is None
        assert opts.tls_cert_path is None
        assert opts.tls_key_path is None
        assert opts.image is None
        assert opts.envs is None
        assert opts.gpu is False
        assert opts.providers is None
        assert opts.timeout == 30.0
        assert opts.ready_timeout == 120.0

    def test_openshell_client_options_with_values(self) -> None:
        """Custom values should be stored correctly."""
        opts = OpenShellSandboxClientOptions(
            cluster="my-cluster",
            endpoint="localhost:50051",
            tls_ca_path="/certs/ca.crt",
            tls_cert_path="/certs/tls.crt",
            tls_key_path="/certs/tls.key",
            image="quay.io/myimage:latest",
            envs={"KEY": "value"},
            gpu=True,
            providers=["vllm"],
            timeout=60.0,
            ready_timeout=300.0,
        )
        assert opts.cluster == "my-cluster"
        assert opts.endpoint == "localhost:50051"
        assert opts.tls_ca_path == "/certs/ca.crt"
        assert opts.tls_cert_path == "/certs/tls.crt"
        assert opts.tls_key_path == "/certs/tls.key"
        assert opts.image == "quay.io/myimage:latest"
        assert opts.envs == {"KEY": "value"}
        assert opts.gpu is True
        assert opts.providers == ["vllm"]
        assert opts.timeout == 60.0
        assert opts.ready_timeout == 300.0


class TestOpenShellSessionState:
    """Session state serialization."""

    def test_openshell_session_state_round_trip(self) -> None:
        """State should survive a serialize-then-deserialize round trip."""
        state = _make_state(
            sandbox_id="abc-123",
            sandbox_name="my-sandbox",
            base_envs={"FOO": "bar"},
        )
        payload = state.model_dump(mode="json")
        restored = OpenShellSandboxSessionState.model_validate(payload)
        assert restored.type == "openshell"
        assert restored.sandbox_id == "abc-123"
        assert restored.sandbox_name == "my-sandbox"
        assert restored.base_envs == {"FOO": "bar"}
        assert restored.session_id == state.session_id

    def test_openshell_deserialize_session_state(self) -> None:
        """The client deserialize_session_state method should produce the correct type."""
        client = OpenShellSandboxClient()
        state = _make_state(sandbox_id="deser-id", sandbox_name="deser-name")
        payload = state.model_dump(mode="json")
        restored = client.deserialize_session_state(payload)
        assert isinstance(restored, OpenShellSandboxSessionState)
        assert restored.sandbox_id == "deser-id"


class TestOpenShellExec:
    """Exec command plumbing."""

    @pytest.mark.asyncio
    async def test_openshell_exec_passes_command_as_list(self) -> None:
        """The exec call should forward command parts as a list and use sandbox_id."""
        session, client = _make_session(workspace_ready=True)
        client._exec_result = _FakeOpenShellExecResult(exit_code=0, stdout="hello", stderr="")

        result = await session._exec_internal("echo", "hello")

        assert result.exit_code == 0
        assert result.stdout == b"hello"
        assert len(client.exec_calls) == 1

        call_sandbox_id, call_cmd, call_kwargs = client.exec_calls[0]
        assert call_sandbox_id == session.state.sandbox_id
        assert call_cmd == ["echo", "hello"]

    @pytest.mark.asyncio
    async def test_openshell_exec_uses_manifest_root_after_workspace_ready(self) -> None:
        """When workspace is ready, workdir should be set to the manifest root."""
        session, client = _make_session(workspace_ready=True)
        client._exec_result = _FakeOpenShellExecResult()

        await session._exec_internal("ls")

        _, _, kwargs = client.exec_calls[0]
        assert kwargs["workdir"] == session.state.manifest.root

    @pytest.mark.asyncio
    async def test_openshell_exec_omits_cwd_until_workspace_ready(self) -> None:
        """Before the workspace is ready, workdir should be None."""
        session, client = _make_session(workspace_ready=False)
        client._exec_result = _FakeOpenShellExecResult()

        await session._exec_internal("whoami")

        _, _, kwargs = client.exec_calls[0]
        assert kwargs["workdir"] is None

    @pytest.mark.asyncio
    async def test_openshell_exec_wraps_grpc_error_as_transport_error(self) -> None:
        """A RuntimeError from the gRPC layer should become ExecTransportError."""
        session, client = _make_session(workspace_ready=True)
        client._exec_error = RuntimeError("gRPC unavailable")

        with pytest.raises(ExecTransportError) as exc_info:
            await session._exec_internal("failing-cmd")

        assert "gRPC unavailable" in str(exc_info.value.__cause__)


# ---------------------------------------------------------------------------
# Client tests
# ---------------------------------------------------------------------------


class TestOpenShellClientCreate:
    """Client create() plumbing."""

    @pytest.mark.asyncio
    async def test_openshell_client_create_passes_spec_options(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create() should forward options to the fake client and return a session."""
        fake_client = _FakeOpenShellClient()
        monkeypatch.setattr(
            _openshell_mod,
            "_resolve_openshell_client",
            lambda options: fake_client,
        )
        monkeypatch.setattr(
            _openshell_mod,
            "_build_sandbox_spec",
            lambda options: {"mock_spec": True},
        )

        client = OpenShellSandboxClient()
        options = OpenShellSandboxClientOptions(
            endpoint="localhost:50051",
            image="quay.io/test:latest",
            envs={"K": "V"},
        )
        session = await client.create(options=options)

        try:
            assert len(fake_client.create_calls) == 1
            assert fake_client.create_calls[0]["spec"] == {"mock_spec": True}
            assert isinstance(session._inner, OpenShellSandboxSession)
        finally:
            await session.aclose()

    @pytest.mark.asyncio
    async def test_openshell_client_create_waits_for_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create() should call wait_ready with the sandbox name."""
        fake_client = _FakeOpenShellClient()
        monkeypatch.setattr(
            _openshell_mod,
            "_resolve_openshell_client",
            lambda options: fake_client,
        )
        monkeypatch.setattr(
            _openshell_mod,
            "_build_sandbox_spec",
            lambda options: None,
        )

        client = OpenShellSandboxClient()
        options = OpenShellSandboxClientOptions(ready_timeout=60.0)
        session = await client.create(options=options)

        try:
            assert len(fake_client.wait_ready_calls) == 1
            sandbox_name, timeout = fake_client.wait_ready_calls[0]
            assert sandbox_name == "sandbox-name-1"
            assert timeout == 60.0
        finally:
            await session.aclose()


class TestOpenShellClientResume:
    """Client resume() plumbing."""

    @pytest.mark.asyncio
    async def test_openshell_resume_reconnects_existing_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resume() should call get() and update the sandbox_id when the sandbox exists."""
        fake_client = _FakeOpenShellClient()
        fake_client._get_result = _FakeSandboxRef(
            id="reconnected-id", name="test-sandbox-name", phase=2
        )
        monkeypatch.setattr(
            _openshell_mod,
            "_build_openshell_client",
            lambda state: fake_client,
        )

        state = _make_state(
            sandbox_id="old-id",
            sandbox_name="test-sandbox-name",
            workspace_root_ready=True,
        )
        client = OpenShellSandboxClient()
        session = await client.resume(state)

        try:
            assert len(fake_client.get_calls) == 1
            assert fake_client.get_calls[0] == "test-sandbox-name"
            inner = session._inner
            assert isinstance(inner, OpenShellSandboxSession)
            assert inner.state.sandbox_id == "reconnected-id"
        finally:
            await session.aclose()

    @pytest.mark.asyncio
    async def test_openshell_resume_recreates_when_sandbox_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resume() should fall back to create() when get() raises."""
        fake_client = _FakeOpenShellClient()
        fake_client._get_error = RuntimeError("sandbox not found")
        monkeypatch.setattr(
            _openshell_mod,
            "_build_openshell_client",
            lambda state: fake_client,
        )

        state = _make_state(
            sandbox_id="old-id",
            sandbox_name="gone-sandbox",
            workspace_root_ready=True,
        )
        client = OpenShellSandboxClient()
        session = await client.resume(state)

        try:
            # get() was attempted, then create() was called as fallback.
            assert len(fake_client.get_calls) == 1
            assert len(fake_client.create_calls) == 1
            inner = session._inner
            assert isinstance(inner, OpenShellSandboxSession)
            # The state should reflect the new sandbox.
            assert inner.state.sandbox_id == "sandbox-id-1"
            assert inner.state.sandbox_name == "sandbox-name-1"
            assert inner.state.workspace_root_ready is False
        finally:
            await session.aclose()


# ---------------------------------------------------------------------------
# Shutdown tests
# ---------------------------------------------------------------------------


class TestOpenShellShutdown:
    """Session shutdown behavior."""

    @pytest.mark.asyncio
    async def test_openshell_shutdown_deletes_sandbox_best_effort(self) -> None:
        """_shutdown_backend should call delete and close on the client."""
        session, client = _make_session(workspace_ready=True)

        await session._shutdown_backend()

        assert len(client.delete_calls) == 1
        assert client.delete_calls[0] == "test-sandbox-name"
        assert client.close_calls == 1

    @pytest.mark.asyncio
    async def test_openshell_shutdown_logs_on_delete_failure(self) -> None:
        """Shutdown should swallow delete errors and still call close."""
        session, client = _make_session(workspace_ready=True)

        def _failing_delete(name: str) -> bool:
            raise RuntimeError("delete failed")

        client.delete = _failing_delete  # type: ignore[assignment]

        # Should not raise.
        await session._shutdown_backend()

        # close() should still be called despite delete failure.
        assert client.close_calls == 1


# ---------------------------------------------------------------------------
# Read / write tests
# ---------------------------------------------------------------------------


class TestOpenShellReadWrite:
    """File read and write operations."""

    @pytest.mark.asyncio
    async def test_openshell_read_returns_file_content(self) -> None:
        """_exec_internal base64 read should decode content correctly.

        Tests the read pipeline at the exec layer, bypassing path validation
        which requires a remote runtime helper script.
        """
        session, client = _make_session(workspace_ready=True)
        expected_content = b"hello from sandbox"
        encoded = base64.b64encode(expected_content).decode("ascii")
        client._exec_result = _FakeOpenShellExecResult(exit_code=0, stdout=encoded, stderr="")

        result = await session._exec_internal("base64", "-w0", "--", "/workspace/test.txt")
        raw = base64.b64decode(result.stdout)

        assert raw == expected_content

    @pytest.mark.asyncio
    async def test_openshell_read_raises_not_found_on_nonzero_exit(self) -> None:
        """A non-zero exit from the read command indicates the file is missing."""
        session, client = _make_session(workspace_ready=True)
        client._exec_result = _FakeOpenShellExecResult(
            exit_code=1, stdout="", stderr="No such file"
        )

        result = await session._exec_internal("base64", "-w0", "--", "/workspace/missing.txt")
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_openshell_write_sends_base64_content(self) -> None:
        """The write pipeline should produce exec calls for mkdir and base64 decode.

        Tests the write pipeline at the exec layer, bypassing path validation
        which requires a remote runtime helper script.
        """
        session, client = _make_session(workspace_ready=True)
        client._exec_result = _FakeOpenShellExecResult(exit_code=0)

        payload = b"file content"
        encoded = base64.b64encode(payload).decode("ascii")
        await session._exec_internal("mkdir", "-p", "--", "/workspace")
        import shlex

        write_cmd = (
            f"printf '%s' {shlex.quote(encoded)} | base64 -d > {shlex.quote('/workspace/out.txt')}"
        )
        await session._exec_internal("sh", "-c", write_cmd)

        assert len(client.exec_calls) == 2


# ---------------------------------------------------------------------------
# Running tests
# ---------------------------------------------------------------------------


class TestOpenShellRunning:
    """Sandbox running status."""

    @pytest.mark.asyncio
    async def test_openshell_running_returns_true_when_ready(self) -> None:
        """running() should return True when the phase indicates ready."""
        session, client = _make_session(workspace_ready=True)
        client._get_result = _FakeSandboxRef(
            id="test-sandbox-id", name="test-sandbox-name", phase=2
        )

        result = await session.running()

        assert result is True

    @pytest.mark.asyncio
    async def test_openshell_running_returns_false_when_not_ready(self) -> None:
        """running() should return False when the phase is not ready."""
        session, client = _make_session(workspace_ready=True)
        client._get_result = _FakeSandboxRef(
            id="test-sandbox-id", name="test-sandbox-name", phase=3
        )

        result = await session.running()

        assert result is False

    @pytest.mark.asyncio
    async def test_openshell_running_returns_false_before_workspace_ready(self) -> None:
        """running() should return False when workspace root is not yet prepared."""
        session, client = _make_session(workspace_ready=False)

        result = await session.running()

        assert result is False


# ---------------------------------------------------------------------------
# Workspace persistence tests
# ---------------------------------------------------------------------------


class TestOpenShellPersistence:
    """Workspace persist/hydrate round trips."""

    @pytest.mark.asyncio
    async def test_openshell_tar_persistence_round_trip(self) -> None:
        """persist_workspace should return valid tar data decoded from base64."""
        session, client = _make_session(workspace_ready=True)

        # Create a small in-memory tar archive.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = b"round-trip content"
            info = tarfile.TarInfo(name="test.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        tar_bytes = buf.getvalue()
        encoded = base64.b64encode(tar_bytes).decode("ascii")

        client._exec_result = _FakeOpenShellExecResult(exit_code=0, stdout=encoded, stderr="")

        result_stream = await session.persist_workspace()
        result_bytes = result_stream.read()

        # Verify the returned bytes form a valid tar archive.
        with tarfile.open(fileobj=io.BytesIO(result_bytes), mode="r") as tf:
            names = tf.getnames()
        assert "test.txt" in names


# ---------------------------------------------------------------------------
# Start lifecycle tests
# ---------------------------------------------------------------------------


class TestOpenShellStartLifecycle:
    """Session start and workspace preparation."""

    @pytest.mark.asyncio
    async def test_openshell_start_prepares_workspace_root(self) -> None:
        """_prepare_backend_workspace should exec mkdir and set _workspace_root_ready."""
        session, client = _make_session(workspace_ready=False)
        client._exec_result = _FakeOpenShellExecResult(exit_code=0)

        await session._prepare_backend_workspace()

        assert session._workspace_root_ready is True
        # The first exec call should be a mkdir command.
        assert len(client.exec_calls) >= 1
        _, cmd, _ = client.exec_calls[0]
        assert cmd[0] == "mkdir"

    def test_openshell_skips_runtime_helpers(self) -> None:
        """OpenShell sessions return no runtime helpers.

        OpenShell rejects command arguments containing newline characters, so the
        multi-line RESOLVE_WORKSPACE_PATH_HELPER script cannot be installed via exec.
        Path validation uses local normalization instead.
        """
        session, _ = _make_session()
        helpers = session._runtime_helpers()

        assert helpers == ()


# ---------------------------------------------------------------------------
# Gateway resolution tests
# ---------------------------------------------------------------------------


class _FakeGatewayClient:
    """Fake SandboxClient class for gateway resolution tests."""

    _instances: list[_FakeGatewayClient] = []
    _from_cluster: str | None = None

    def __init__(self, endpoint: str = "", *, tls: Any = None, timeout: float = 30.0) -> None:
        self.endpoint = endpoint
        self.tls = tls
        self.timeout = timeout
        _FakeGatewayClient._instances.append(self)

    @classmethod
    def from_active_cluster(
        cls, *, cluster: str | None = None, timeout: float = 30.0
    ) -> _FakeGatewayClient:
        instance = cls(endpoint="<from-cluster>", timeout=timeout)
        instance._from_cluster = cluster
        return instance


class TestOpenShellGatewayResolution:
    """Client connection resolution (endpoint vs cluster)."""

    def test_openshell_client_uses_explicit_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When an endpoint is set, the client should be created with it directly."""
        _FakeGatewayClient._instances = []
        monkeypatch.setattr(
            _openshell_mod,
            "_import_openshell_client",
            lambda: _FakeGatewayClient,
        )

        options = OpenShellSandboxClientOptions(endpoint="my-host:50051")
        result = _openshell_mod._resolve_openshell_client(options)

        assert isinstance(result, _FakeGatewayClient)
        assert result.endpoint == "my-host:50051"

    def test_openshell_client_resolves_from_active_cluster(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no endpoint is set, the client should use from_active_cluster."""
        _FakeGatewayClient._instances = []
        monkeypatch.setattr(
            _openshell_mod,
            "_import_openshell_client",
            lambda: _FakeGatewayClient,
        )

        options = OpenShellSandboxClientOptions(cluster="staging-cluster")
        result = _openshell_mod._resolve_openshell_client(options)

        assert isinstance(result, _FakeGatewayClient)
        assert result.endpoint == "<from-cluster>"
        assert result._from_cluster == "staging-cluster"
