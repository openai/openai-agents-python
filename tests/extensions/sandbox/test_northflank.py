"""NorthflankSandboxClient + NorthflankSandboxSession tests.

The Northflank SDK is faked so tests are fully offline. We assert on the
recorded SDK calls — that's what guarantees we match the wire contract.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

# The ``northflank`` SDK pins ``python_version < '3.14'`` (see pyproject's
# optional-dependencies block), so the package isn't installed on 3.14 CI
# legs. Skip this whole module there instead of failing at import time.
pytest.importorskip("northflank")

from northflank import ApiCallError  # noqa: E402

from agents.extensions.sandbox.northflank import (  # noqa: E402
    NorthflankSandboxClient,
    NorthflankSandboxClientOptions,
    NorthflankSandboxSession,
    NorthflankSandboxSessionState,
)
from agents.sandbox.errors import (
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
)
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import SandboxSession, SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshotSpec

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeExecResult:
    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        status: str = "completed",
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.status = status
        self.message = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


_HELPER_PATH_PREFIX = "/tmp/openai-agents/bin/resolve-workspace-path"


def _looks_like_resolve_helper_invocation(cmd: tuple[Any, ...]) -> bool:
    return bool(cmd) and isinstance(cmd[0], str) and cmd[0].startswith(_HELPER_PATH_PREFIX)


def _looks_like_helper_install(cmd: tuple[Any, ...]) -> bool:
    return (
        len(cmd) >= 5
        and cmd[0] == "sh"
        and cmd[1] == "-c"
        and isinstance(cmd[2], str)
        and "INSTALL_RUNTIME_HELPER_V1" in cmd[2]
        and cmd[3] == "sh"
        and isinstance(cmd[4], str)
        and cmd[4].startswith(_HELPER_PATH_PREFIX)
    )


def _looks_like_helper_present_test(cmd: tuple[Any, ...]) -> bool:
    return (
        len(cmd) >= 3
        and cmd[0] == "test"
        and cmd[1] == "-x"
        and isinstance(cmd[2], str)
        and cmd[2].startswith(_HELPER_PATH_PREFIX)
    )


class _FakeExec:
    """Fake Northflank exec channel.

    By default it auto-handles the openai-agents resolve-workspace-path
    runtime helper: install commands return ok, presence checks return ok,
    and helper invocations echo the requested workspace path back as stdout
    so ``_validate_remote_path_access`` accepts the path. ``user_calls``
    (re-)exposes only the non-helper invocations for assertion purposes.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_result: _FakeExecResult = _FakeExecResult()
        self.results_by_prefix: list[tuple[tuple[str, ...], _FakeExecResult]] = []

    def queue(self, command_prefix: tuple[str, ...], result: _FakeExecResult) -> None:
        self.results_by_prefix.append((command_prefix, result))

    @property
    def user_calls(self) -> list[dict[str, Any]]:
        """Calls that are not part of the runtime-helper install/probe round trip."""
        return [
            c
            for c in self.calls
            if not (
                _looks_like_helper_install(tuple(c.get("command") or ()))
                or _looks_like_helper_present_test(tuple(c.get("command") or ()))
                or _looks_like_resolve_helper_invocation(tuple(c.get("command") or ()))
            )
        ]

    async def arun_service_command(self, **kwargs: Any) -> _FakeExecResult:
        self.calls.append(kwargs)
        cmd = tuple(kwargs.get("command") or ())
        if _looks_like_resolve_helper_invocation(cmd):
            # Echo back the workspace path so _validate_remote_path_access
            # succeeds. The helper signature is:
            #   <helper_path> <root> <workspace_path> <for_write> [grants...]
            workspace = str(cmd[2]) if len(cmd) >= 3 else "/workspace"
            return _FakeExecResult(stdout=workspace)
        if _looks_like_helper_install(cmd) or _looks_like_helper_present_test(cmd):
            return _FakeExecResult()
        for prefix, result in self.results_by_prefix:
            if cmd[: len(prefix)] == prefix:
                return result
        return self.next_result


class _FakeFiles:
    """Fake Northflank files endpoint.

    ``download_payload`` is written to ``<local_path>/<remote_basename>``
    so the session's read/persist paths — which pass a temp *directory*
    as ``local_path`` — can look up the file by its remote basename,
    matching the real SDK's directory-target semantics for
    ``_extract_download_tar``.
    """

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.downloads: list[dict[str, Any]] = []
        self.download_payload: bytes = b""
        self.raise_on_download: Exception | None = None

    async def aupload(self, **kwargs: Any) -> None:
        self.uploads.append(kwargs)

    async def adownload(self, **kwargs: Any) -> None:
        self.downloads.append(kwargs)
        if self.raise_on_download is not None:
            raise self.raise_on_download
        from pathlib import Path as _P, PurePosixPath as _PP

        local = _P(kwargs["local_path"])
        local.mkdir(parents=True, exist_ok=True)
        target = local / _PP(kwargs["remote_path"]).name
        target.write_bytes(self.download_payload)


class _FakeServiceEndpoint:
    """Mirrors the SDK's CallableNamespace pattern: the endpoint is the
    namespace itself, invoked directly via __call__."""

    def __init__(self) -> None:
        self.response_data: dict[str, Any] = {
            "id": "svc-new",
            "name": "svc-new",
        }
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call: Exception | None = None

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raise_on_call is not None:
            raise self.raise_on_call

        class _R:
            data = self.response_data

        return _R()


class _FakeGetService(_FakeServiceEndpoint):
    def __init__(self) -> None:
        super().__init__()
        self.response_data = {
            "servicePaused": False,
            "status": {"deployment": {"status": "COMPLETED"}},
        }


class _FakeNamespace:
    pass


class _FakeHelpers:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call: Exception | None = None

    async def wait_for_service_ready(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return {"status": {"deployment": {"status": "COMPLETED"}}}


class _FakeClient:
    def __init__(self) -> None:
        self.exec = _FakeExec()
        self.files = _FakeFiles()
        self.helpers = _FakeHelpers()

        self._get_service = _FakeGetService()
        self.get = _FakeNamespace()
        self.get.service = self._get_service  # type: ignore[attr-defined]

        self._create_deployment = _FakeServiceEndpoint()
        self.create = _FakeNamespace()
        self.create.service = _FakeNamespace()  # type: ignore[attr-defined]
        self.create.service.deployment = self._create_deployment  # type: ignore[attr-defined]

        self._delete_service = _FakeServiceEndpoint()
        self._delete_volume = _FakeServiceEndpoint()
        self.delete = _FakeNamespace()
        self.delete.service = self._delete_service  # type: ignore[attr-defined]
        self.delete.volume = self._delete_volume  # type: ignore[attr-defined]

        self._create_volume = _FakeServiceEndpoint()
        self._create_volume.response_data = {"id": "vol-new", "name": "vol-new"}
        self.create.volume = self._create_volume  # type: ignore[attr-defined]

        self._detach_volume = _FakeServiceEndpoint()
        self.detach = _FakeNamespace()
        self.detach.volume = self._detach_volume  # type: ignore[attr-defined]

        self._attach_volume = _FakeServiceEndpoint()
        self.attach = _FakeNamespace()
        self.attach.volume = self._attach_volume  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    service_id: str = "svc-1",
    project_id: str = "proj-1",
    owned: bool = False,
    root: str = "/workspace",
) -> NorthflankSandboxSessionState:
    return NorthflankSandboxSessionState(
        manifest=Manifest(root=root),
        snapshot=NoopSnapshotSpec().build("snap-1"),
        project_id=project_id,
        service_id=service_id,
        owned_by_client=owned,
    )


def _make_session(client: _FakeClient, **state_kwargs: Any) -> NorthflankSandboxSession:
    state = _make_state(**state_kwargs)
    return NorthflankSandboxSession(state=state, client=client)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_options_round_trip() -> None:
    opts = NorthflankSandboxClientOptions(project_id="proj", service_id="svc", team_id="team-x")
    dumped = opts.model_dump()
    assert dumped["type"] == "northflank"
    assert dumped["project_id"] == "proj"
    assert dumped["service_id"] == "svc"


def test_deserialize_session_state_registry() -> None:
    client = NorthflankSandboxClient(client=_FakeClient())
    state = _make_state()
    raw = state.model_dump()
    revived = client.deserialize_session_state(raw)
    assert isinstance(revived, NorthflankSandboxSessionState)
    assert revived.service_id == state.service_id

    # The polymorphic registry should pick up our subclass too.
    again = SandboxSessionState.parse(raw)
    assert isinstance(again, NorthflankSandboxSessionState)


def test_backend_id_constants() -> None:
    assert NorthflankSandboxClient.backend_id == "northflank"
    assert NorthflankSandboxClient.supports_default_options is False


@pytest.mark.asyncio
async def test_exec_internal_passes_argv_to_sdk_with_shell_none() -> None:
    client = _FakeClient()
    session = _make_session(client)
    result = await session._exec_internal("ls", "-la", "/workspace", timeout=5.0)
    assert result.exit_code == 0
    assert isinstance(result.stdout, bytes)
    assert isinstance(result.stderr, bytes)
    assert len(client.exec.user_calls) == 1
    call = client.exec.user_calls[0]
    assert call["command"] == ["ls", "-la", "/workspace"]
    assert call["shell"] == "none"
    assert call["service_id"] == "svc-1"
    assert call["project_id"] == "proj-1"
    assert call["timeout"] == 5.0


@pytest.mark.asyncio
async def test_exec_internal_falls_back_to_default_timeout() -> None:
    client = _FakeClient()
    session = _make_session(client)
    await session._exec_internal("echo", "hi")
    assert client.exec.user_calls[0]["timeout"] == session.state.exec_timeout_s


@pytest.mark.asyncio
async def test_running_true_when_deployment_completed() -> None:
    client = _FakeClient()
    session = _make_session(client)
    assert await session.running() is True


@pytest.mark.asyncio
async def test_running_false_when_paused() -> None:
    client = _FakeClient()
    client._get_service.response_data = {
        "servicePaused": True,
        "status": {"deployment": {"status": "COMPLETED"}},
    }
    session = _make_session(client)
    assert await session.running() is False


@pytest.mark.asyncio
async def test_running_false_after_shutdown() -> None:
    client = _FakeClient()
    session = _make_session(client)
    await session._shutdown_backend()
    assert await session.running() is False


@pytest.mark.asyncio
async def test_read_downloads_via_files_api() -> None:
    client = _FakeClient()
    client.files.download_payload = b"hello world"
    session = _make_session(client)
    stream = await session.read(Path("/workspace/hello.txt"))
    assert stream.read() == b"hello world"
    assert len(client.files.downloads) == 1
    assert client.files.downloads[0]["remote_path"] == "/workspace/hello.txt"
    assert client.files.downloads[0]["service_id"] == "svc-1"


@pytest.mark.asyncio
async def test_read_with_user_raises_not_implemented() -> None:
    client = _FakeClient()
    session = _make_session(client)
    with pytest.raises(NotImplementedError, match="per-user"):
        await session.read(Path("/workspace/x"), user="root")


@pytest.mark.asyncio
async def test_write_with_user_raises_not_implemented() -> None:
    client = _FakeClient()
    session = _make_session(client)
    with pytest.raises(NotImplementedError, match="per-user"):
        await session.write(Path("/workspace/x"), io.BytesIO(b"hi"), user="root")


@pytest.mark.asyncio
async def test_read_missing_path_raises_workspace_read_not_found() -> None:
    client = _FakeClient()
    client.files.raise_on_download = RuntimeError("404 not found")
    session = _make_session(client)
    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read(Path("/workspace/missing.txt"))


@pytest.mark.asyncio
async def test_write_mkdirs_then_uploads_directory_with_correct_basename() -> None:
    client = _FakeClient()
    captured: dict[str, Any] = {}
    original_upload = client.files.aupload

    async def capturing_upload(**kwargs: Any) -> None:
        local_dir = Path(kwargs["local_path"])
        captured["local_dir"] = str(local_dir)
        captured["entries"] = sorted(p.name for p in local_dir.iterdir())
        target = local_dir / captured["entries"][0]
        captured["bytes"] = target.read_bytes()
        await original_upload(**kwargs)

    client.files.aupload = capturing_upload  # type: ignore[method-assign]
    session = _make_session(client)
    await session.write(Path("/workspace/sub/dir/file.txt"), io.BytesIO(b"payload"))

    # mkdir -p of the parent fires first.
    mkdir_call = client.exec.user_calls[0]
    assert mkdir_call["command"] == ["mkdir", "-p", "/workspace/sub/dir"]
    # Upload is a *directory* targeted at the parent so the SDK's directory
    # extraction places the file at <parent>/<basename> regardless of suffix.
    assert len(client.files.uploads) == 1
    upload = client.files.uploads[0]
    assert upload["remote_path"] == "/workspace/sub/dir"
    assert captured["entries"] == ["file.txt"]
    assert captured["bytes"] == b"payload"


@pytest.mark.asyncio
async def test_write_handles_extensionless_paths() -> None:
    """An extensionless remote like /workspace/Makefile must not be treated
    as a directory by the SDK's upload heuristic."""
    client = _FakeClient()
    captured: dict[str, Any] = {}
    original_upload = client.files.aupload

    async def capturing_upload(**kwargs: Any) -> None:
        local_dir = Path(kwargs["local_path"])
        captured["entries"] = sorted(p.name for p in local_dir.iterdir())
        captured["remote_path"] = kwargs["remote_path"]
        await original_upload(**kwargs)

    client.files.aupload = capturing_upload  # type: ignore[method-assign]
    session = _make_session(client)
    await session.write(Path("/workspace/Makefile"), io.BytesIO(b"all: build\n"))

    # The staging directory has exactly one entry — the basename Makefile —
    # and remote_path is the parent directory.
    assert captured["entries"] == ["Makefile"]
    assert captured["remote_path"] == "/workspace"


@pytest.mark.asyncio
async def test_write_propagates_upload_failure_as_archive_write_error() -> None:
    client = _FakeClient()

    async def boom(**kwargs: Any) -> None:
        raise RuntimeError("permission denied")

    client.files.aupload = boom  # type: ignore[method-assign]
    session = _make_session(client)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.write(Path("/workspace/x"), io.BytesIO(b"hi"))


def _make_valid_tar_bytes(entries: dict[str, bytes]) -> bytes:
    """Build a tar archive in memory with safe member names."""
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_hydrate_then_persist_workspace_round_trip() -> None:
    client = _FakeClient()
    session = _make_session(client)
    tar_bytes = _make_valid_tar_bytes({"./hello.txt": b"hi\n"})

    # Hydrate uploads (staging dir) + extracts on remote.
    upload_snapshot: dict[str, Any] = {}
    original_upload = client.files.aupload

    async def snapshotting_upload(**kwargs: Any) -> None:
        local_dir = Path(kwargs["local_path"])
        upload_snapshot["is_dir"] = local_dir.is_dir()
        upload_snapshot["entries"] = sorted(p.name for p in local_dir.iterdir())
        await original_upload(**kwargs)

    client.files.aupload = snapshotting_upload  # type: ignore[method-assign]

    await session.hydrate_workspace(io.BytesIO(tar_bytes))
    user_cmds = [tuple(c["command"]) for c in client.exec.user_calls]
    assert any("tar" in " ".join(cmd) for cmd in user_cmds)
    assert any(cmd and cmd[0] == "rm" for cmd in user_cmds)
    assert len(client.files.uploads) == 1
    assert upload_snapshot["is_dir"] is True
    # The staging dir held exactly one entry — the per-call archive basename.
    assert len(upload_snapshot["entries"]) == 1
    assert upload_snapshot["entries"][0].endswith(".tar")
    assert client.files.uploads[0]["remote_path"] == "/tmp"

    # Persist: tar in container, download to a directory, rm.
    client.exec.calls.clear()
    client.files.download_payload = tar_bytes
    stream = await session.persist_workspace()
    assert stream.read() == tar_bytes
    first = client.exec.user_calls[0]
    assert "tar" in first["command"][2]
    assert len(client.files.downloads) >= 1


@pytest.mark.asyncio
async def test_hydrate_rejects_absolute_member() -> None:
    client = _FakeClient()
    session = _make_session(client)
    bad = _make_valid_tar_bytes({"/etc/passwd": b"x"})
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(bad))


@pytest.mark.asyncio
async def test_hydrate_rejects_traversal_member() -> None:
    client = _FakeClient()
    session = _make_session(client)
    bad = _make_valid_tar_bytes({"../escape": b"x"})
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(bad))


def _make_symlink_tar(name: str, linkname: str) -> bytes:
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.SYMTYPE
        info.linkname = linkname
        tf.addfile(info)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_hydrate_rejects_absolute_symlink_target() -> None:
    client = _FakeClient()
    session = _make_session(client)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(_make_symlink_tar("link", "/etc/passwd")))


@pytest.mark.asyncio
async def test_hydrate_rejects_symlink_escaping_archive() -> None:
    client = _FakeClient()
    session = _make_session(client)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(_make_symlink_tar("sub/link", "../../outside")))


@pytest.mark.asyncio
async def test_hydrate_allows_relative_symlink_within_archive() -> None:
    client = _FakeClient()
    session = _make_session(client)
    # ``sub/link`` -> ``../other.txt`` resolves to the archive's ``other.txt``,
    # which is inside the root — allowed.
    await session.hydrate_workspace(io.BytesIO(_make_symlink_tar("sub/link", "../other.txt")))
    # Upload + extract + cleanup ran without raising.
    assert len(client.files.uploads) == 1
    assert any(
        tuple(c["command"])[:1] == ("sh",) and "tar" in c["command"][2]
        for c in client.exec.user_calls
    )


@pytest.mark.asyncio
async def test_hydrate_rejects_root_level_dotdot_symlink() -> None:
    """A symlink at the archive root pointing to ``..`` escapes — there
    is no parent inside the archive to pop into."""
    client = _FakeClient()
    session = _make_session(client)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(_make_symlink_tar("link", "..")))


@pytest.mark.asyncio
async def test_hydrate_allows_symlink_pointing_to_sibling() -> None:
    """``a/link`` -> ``b.txt`` resolves to ``a/b.txt`` (same directory)."""
    client = _FakeClient()
    session = _make_session(client)
    await session.hydrate_workspace(io.BytesIO(_make_symlink_tar("a/link", "b.txt")))
    assert len(client.files.uploads) == 1


@pytest.mark.asyncio
async def test_hydrate_allows_symlink_resolving_to_archive_root() -> None:
    """``a/link`` -> ``..`` resolves to the archive root itself — inside,
    so allowed (a symlink to the workspace root is a weird but legal
    archive entry, not an escape)."""
    client = _FakeClient()
    session = _make_session(client)
    await session.hydrate_workspace(io.BytesIO(_make_symlink_tar("a/link", "..")))
    assert len(client.files.uploads) == 1


@pytest.mark.asyncio
async def test_hydrate_handles_collapsed_double_slashes_in_link_target() -> None:
    """``a//b`` is just ``a/b`` after PurePosixPath normalisation; the
    helper must not treat the empty component as a traversal escape."""
    client = _FakeClient()
    session = _make_session(client)
    await session.hydrate_workspace(io.BytesIO(_make_symlink_tar("a/link", "..//b.txt")))
    assert len(client.files.uploads) == 1


@pytest.mark.asyncio
async def test_hydrate_rejects_link_target_with_just_enough_dotdots_to_escape() -> None:
    """``a/b/link`` -> ``../../..`` walks one segment past root — reject."""
    client = _FakeClient()
    session = _make_session(client)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(_make_symlink_tar("a/b/link", "../../..")))


@pytest.mark.asyncio
async def test_hydrate_wraps_non_tar_bytes_as_archive_write_error() -> None:
    """Bytes that aren't a tar at all should surface as a
    WorkspaceArchiveWriteError (the ``invalid_tar`` path), not the raw
    tarfile exception."""
    client = _FakeClient()
    session = _make_session(client)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(b"not a tar at all"))


@pytest.mark.asyncio
async def test_hydrate_rejects_device_member() -> None:
    import tarfile

    client = _FakeClient()
    session = _make_session(client)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name="dev/null")
        info.type = tarfile.CHRTYPE
        info.devmajor = 1
        info.devminor = 3
        tf.addfile(info)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(buf.getvalue()))


@pytest.mark.asyncio
async def test_persist_workspace_emits_exclude_args_from_skip_paths() -> None:
    client = _FakeClient()
    session = _make_session(client)
    rel = session.register_persist_workspace_skip_path(".cache")

    client.files.download_payload = _make_valid_tar_bytes({"./x": b"y"})
    await session.persist_workspace()
    first = client.exec.user_calls[0]
    tar_script = first["command"][2]
    # shell_tar_exclude_args emits both bare and ``./``-prefixed forms.
    assert "--exclude=.cache" in tar_script or "--exclude='.cache'" in tar_script
    assert "--exclude=./.cache" in tar_script or "--exclude='./.cache'" in tar_script
    # The registered path was the one we requested.
    assert str(rel) == ".cache"


# -- client lifecycle -------------------------------------------------------


@pytest.mark.asyncio
async def test_create_attach_mode_does_not_create_service() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(project_id="proj", service_id="svc-existing")
    session = await sandbox_client.create(options=options)
    assert isinstance(session, SandboxSession)
    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    assert inner.state.service_id == "svc-existing"
    assert inner.state.owned_by_client is False
    # No deployment creation, no helpers waiting.
    assert client._create_deployment.calls == []
    assert client.helpers.calls == []


@pytest.mark.asyncio
async def test_create_ephemeral_mode_creates_and_waits() -> None:
    client = _FakeClient()
    client._create_deployment.response_data = {"id": "svc-new", "name": "svc-new"}
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="nginx:1.27",
    )
    session = await sandbox_client.create(options=options)
    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    assert inner.state.service_id == "svc-new"
    assert inner.state.owned_by_client is True
    assert len(client._create_deployment.calls) == 1
    create_call = client._create_deployment.calls[0]
    assert create_call["data"]["deployment"]["external"]["imagePath"] == "nginx:1.27"
    # wait_for_ready=True by default
    assert len(client.helpers.calls) == 1


@pytest.mark.asyncio
async def test_create_ephemeral_sets_docker_command() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="ubuntu:24.04",
        docker_command="sleep infinity",
    )
    await sandbox_client.create(options=options)
    payload = client._create_deployment.calls[0]["data"]
    assert payload["deployment"]["docker"] == {
        "configType": "customCommand",
        "customCommand": "sleep infinity",
    }


@pytest.mark.asyncio
async def test_create_ephemeral_sets_entrypoint_and_command() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        docker_entrypoint="/bin/sh",
        docker_command="-c 'tail -f /dev/null'",
    )
    await sandbox_client.create(options=options)
    payload = client._create_deployment.calls[0]["data"]
    assert payload["deployment"]["docker"] == {
        "configType": "customEntrypointCustomCommand",
        "customEntrypoint": "/bin/sh",
        "customCommand": "-c 'tail -f /dev/null'",
    }


@pytest.mark.asyncio
async def test_create_ephemeral_omits_docker_block_by_default() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(project_id="proj", image_path="alpine")
    await sandbox_client.create(options=options)
    payload = client._create_deployment.calls[0]["data"]
    assert "docker" not in payload["deployment"]


@pytest.mark.asyncio
async def test_create_ephemeral_deletes_service_when_wait_fails() -> None:
    """If wait_for_service_ready raises, the service is already on
    Northflank but no SandboxSession ever materialises — the client must
    delete it best-effort so it doesn't leak."""
    client = _FakeClient()
    client.helpers.raise_on_call = TimeoutError("deployment did not become ready")
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(project_id="proj", image_path="alpine")
    with pytest.raises(TimeoutError):
        await sandbox_client.create(options=options)
    assert len(client._delete_service.calls) == 1
    assert client._delete_service.calls[0]["service_id"] == "svc-new"
    assert client._delete_service.calls[0]["delete_child_objects"] is True


@pytest.mark.asyncio
async def test_create_ephemeral_wait_cleanup_swallows_delete_failure() -> None:
    """If both wait and the cleanup delete fail, the original wait error
    must still propagate — the delete is best-effort and must not mask it."""
    client = _FakeClient()
    client.helpers.raise_on_call = TimeoutError("deployment did not become ready")
    client._delete_service.raise_on_call = RuntimeError("network blip")
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(project_id="proj", image_path="alpine")
    with pytest.raises(TimeoutError):
        await sandbox_client.create(options=options)


@pytest.mark.asyncio
async def test_create_rejects_both_service_id_and_image_path() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj", service_id="svc", image_path="nginx:1.27"
    )
    with pytest.raises(ValueError):
        await sandbox_client.create(options=options)


@pytest.mark.asyncio
async def test_create_requires_service_id_or_image() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(project_id="proj")
    with pytest.raises(ValueError):
        await sandbox_client.create(options=options)


@pytest.mark.asyncio
async def test_resume_attaches_to_existing_state() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    state = _make_state(service_id="svc-resumed", owned=True)
    session = await sandbox_client.resume(state)
    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    assert inner.state.service_id == "svc-resumed"
    # Resume must not create a new service or wait.
    assert client._create_deployment.calls == []
    assert client.helpers.calls == []


@pytest.mark.asyncio
async def test_delete_swallows_only_404() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    client._create_deployment.response_data = {"id": "svc-new"}
    session = await sandbox_client.create(
        options=NorthflankSandboxClientOptions(project_id="proj", image_path="alpine")
    )

    # 404 → swallowed (service already gone is not a cleanup failure)
    client._delete_service.raise_on_call = ApiCallError(status=404, message="not found")
    await sandbox_client.delete(session)

    # 401 → surfaced (cleanup masked an auth issue we want to see)
    session = await sandbox_client.create(
        options=NorthflankSandboxClientOptions(project_id="proj", image_path="alpine")
    )
    client._delete_service.raise_on_call = ApiCallError(status=401, message="unauthorized")
    with pytest.raises(ApiCallError) as excinfo:
        await sandbox_client.delete(session)
    assert excinfo.value.status == 401


@pytest.mark.asyncio
async def test_delete_only_removes_client_owned_services() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    attached = await sandbox_client.create(
        options=NorthflankSandboxClientOptions(project_id="proj", service_id="svc-existing")
    )
    await sandbox_client.delete(attached)
    assert client._delete_service.calls == [], "must not delete attached service"

    client._create_deployment.response_data = {"id": "svc-new"}
    ephemeral = await sandbox_client.create(
        options=NorthflankSandboxClientOptions(project_id="proj", image_path="alpine")
    )
    await sandbox_client.delete(ephemeral)
    assert len(client._delete_service.calls) == 1
    assert client._delete_service.calls[0]["service_id"] == "svc-new"


# -- workspace_persistence: volume mode ------------------------------------


@pytest.mark.asyncio
async def test_volume_mode_create_provisions_volume_and_attaches_to_service() -> None:
    client = _FakeClient()
    client._create_deployment.response_data = {"id": "svc-new", "name": "svc-new"}
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="ubuntu:24.04",
        docker_command="sleep infinity",
        workspace_persistence="volume",
        volume_spec={"storageSize": 20480, "accessMode": "ReadWriteOnce"},
    )

    session = await sandbox_client.create(options=options)

    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    assert inner.state.workspace_persistence == "volume"
    assert inner.state.volume_id == "vol-new"
    assert inner.state.owned_volume is True
    # The service was deployed and the volume was created attached to it.
    assert len(client._create_deployment.calls) == 1
    assert len(client._create_volume.calls) == 1
    volume_call = client._create_volume.calls[0]
    payload = volume_call["data"]
    assert payload["spec"] == {"storageSize": 20480, "accessMode": "ReadWriteOnce"}
    assert payload["mounts"] == [{"containerMountPath": "/workspace"}]
    assert payload["attachedObjects"] == [{"id": "svc-new", "type": "service"}]
    # Readiness wait still ran — once, AFTER the volume was attached.
    assert len(client.helpers.calls) == 1


@pytest.mark.asyncio
async def test_volume_mode_uses_default_spec_when_unspecified() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
    )
    await sandbox_client.create(options=options)
    payload = client._create_volume.calls[0]["data"]
    assert payload["spec"] == {
        "storageSize": 5120,
        "accessMode": "ReadWriteMany",
        "storageClassName": "nf-multi-rw",
    }


@pytest.mark.asyncio
async def test_volume_mode_requires_image_path() -> None:
    """volume mode only makes sense for client-created services."""
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        service_id="svc-existing",
        workspace_persistence="volume",
    )
    with pytest.raises(ValueError, match="image_path"):
        await sandbox_client.create(options=options)
    # No mutation made.
    assert client._create_volume.calls == []
    assert client._create_deployment.calls == []


@pytest.mark.asyncio
async def test_volume_mode_cleans_up_service_when_volume_creation_fails() -> None:
    client = _FakeClient()
    client._create_volume.raise_on_call = RuntimeError("volume quota exceeded")
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
    )
    with pytest.raises(RuntimeError, match="quota"):
        await sandbox_client.create(options=options)
    # Service was created but volume failed — delete the stranded service.
    assert len(client._delete_service.calls) == 1
    assert client._delete_service.calls[0]["service_id"] == "svc-new"
    # No volume was created, so no volume delete either.
    assert client._delete_volume.calls == []


@pytest.mark.asyncio
async def test_volume_mode_cleans_up_volume_and_service_when_wait_fails() -> None:
    """If the post-volume-attach readiness wait fails, both the volume and
    service must be best-effort deleted so no stray resource leaks."""
    client = _FakeClient()
    client.helpers.raise_on_call = TimeoutError("readiness deadline")
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
    )
    with pytest.raises(TimeoutError):
        await sandbox_client.create(options=options)
    assert len(client._delete_volume.calls) == 1
    assert client._delete_volume.calls[0]["volume_id"] == "vol-new"
    assert len(client._delete_service.calls) == 1


@pytest.mark.asyncio
async def test_volume_mode_delete_runs_service_then_detach_then_volume() -> None:
    """Northflank refuses to delete a volume while ``attachedObjects``
    still references the service, and deleting the service does not
    auto-detach. delete() must therefore: (1) remove the service,
    (2) explicitly detach the volume from it, (3) delete the volume."""
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
    )
    session = await sandbox_client.create(options=options)

    order: list[str] = []
    # Wrap each namespace entry so we record the actual call order.
    real_delete_service = client._delete_service

    async def trace_delete_service(**kwargs: Any) -> Any:
        order.append("delete.service")
        return await real_delete_service(**kwargs)

    real_detach_volume = client._detach_volume

    async def trace_detach_volume(**kwargs: Any) -> Any:
        order.append("detach.volume")
        return await real_detach_volume(**kwargs)

    real_delete_volume = client._delete_volume

    async def trace_delete_volume(**kwargs: Any) -> Any:
        order.append("delete.volume")
        return await real_delete_volume(**kwargs)

    client.delete.service = trace_delete_service  # type: ignore[assignment]
    client.detach.volume = trace_detach_volume  # type: ignore[assignment]
    client.delete.volume = trace_delete_volume  # type: ignore[assignment]

    await sandbox_client.delete(session)
    assert order == ["delete.service", "detach.volume", "delete.volume"]
    assert client._detach_volume.calls[0]["volume_id"] == "vol-new"
    assert client._detach_volume.calls[0]["data"] == {
        "nfObject": {"id": "svc-new", "type": "service"}
    }


@pytest.mark.asyncio
async def test_volume_mode_delete_swallows_volume_404() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
    )
    session = await sandbox_client.create(options=options)

    client._delete_volume.raise_on_call = ApiCallError(status=404, message="gone")
    await sandbox_client.delete(session)
    # Service still gets removed even though the volume was already gone.
    assert len(client._delete_service.calls) == 1


@pytest.mark.asyncio
async def test_volume_mode_state_round_trips_through_serialize() -> None:
    """volume_id, owned_volume, and workspace_persistence must survive
    serialize/deserialize so resumed sessions can clean up properly."""
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
    )
    session = await sandbox_client.create(options=options)
    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)

    raw = sandbox_client.serialize_session_state(inner.state)
    revived = sandbox_client.deserialize_session_state(raw)
    assert isinstance(revived, NorthflankSandboxSessionState)
    assert revived.workspace_persistence == "volume"
    assert revived.volume_id == "vol-new"
    assert revived.owned_volume is True


# -- workspace_persistence: caller-owned volume ----------------------------


@pytest.mark.asyncio
async def test_volume_mode_attaches_caller_supplied_volume_to_new_service() -> None:
    """Passing volume_id together with image_path attaches the existing
    volume to the freshly-created service. No volume create happens,
    and owned_volume stays False."""
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
        volume_id="vol-existing",
    )

    session = await sandbox_client.create(options=options)

    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    assert inner.state.workspace_persistence == "volume"
    assert inner.state.volume_id == "vol-existing"
    assert inner.state.owned_volume is False
    # No volume creation. One attach call against the new service.
    assert client._create_volume.calls == []
    assert len(client._attach_volume.calls) == 1
    attach = client._attach_volume.calls[0]
    assert attach["volume_id"] == "vol-existing"
    assert attach["data"] == {"nfObject": {"id": "svc-new", "type": "service"}}
    # Readiness wait still ran AFTER the attach.
    assert len(client.helpers.calls) == 1


@pytest.mark.asyncio
async def test_volume_mode_attaches_caller_supplied_volume_to_existing_service() -> None:
    """service_id + volume_id is a valid combination: attach the
    caller's volume to the caller's service. Neither resource is owned
    by the client."""
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        service_id="svc-existing",
        workspace_persistence="volume",
        volume_id="vol-existing",
    )

    session = await sandbox_client.create(options=options)

    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    assert inner.state.owned_by_client is False
    assert inner.state.owned_volume is False
    assert inner.state.volume_id == "vol-existing"
    # No service create, no service wait.
    assert client._create_deployment.calls == []
    assert client.helpers.calls == []
    # Volume was attached to the caller's service.
    assert len(client._attach_volume.calls) == 1
    assert client._attach_volume.calls[0]["data"] == {
        "nfObject": {"id": "svc-existing", "type": "service"}
    }


@pytest.mark.asyncio
async def test_volume_mode_rejects_volume_id_with_volume_spec() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
        volume_id="vol-existing",
        volume_spec={"storageSize": 5120, "accessMode": "ReadWriteMany"},
    )
    with pytest.raises(ValueError, match="volume_spec is ignored"):
        await sandbox_client.create(options=options)
    assert client._create_deployment.calls == []
    assert client._attach_volume.calls == []


@pytest.mark.asyncio
async def test_volume_id_without_workspace_persistence_raises() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        volume_id="vol-existing",
    )
    with pytest.raises(ValueError, match="workspace_persistence='volume'"):
        await sandbox_client.create(options=options)


@pytest.mark.asyncio
async def test_volume_mode_tolerates_already_attached_409() -> None:
    """If the caller's volume is already attached to this service, the
    attach call returns 409; we treat that as success."""
    client = _FakeClient()
    client._attach_volume.raise_on_call = ApiCallError(
        status=409, message="Volume already attached"
    )
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
        volume_id="vol-existing",
    )

    session = await sandbox_client.create(options=options)
    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    assert inner.state.volume_id == "vol-existing"
    assert inner.state.owned_volume is False


@pytest.mark.asyncio
async def test_volume_mode_attach_failure_deletes_owned_service() -> None:
    """If attach.volume raises a real (non-409) error, the client must
    clean up the service it just created — otherwise it leaks."""
    client = _FakeClient()
    client._attach_volume.raise_on_call = ApiCallError(status=404, message="Volume not found")
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
        volume_id="vol-missing",
    )
    with pytest.raises(ApiCallError):
        await sandbox_client.create(options=options)
    assert len(client._delete_service.calls) == 1
    assert client._delete_service.calls[0]["service_id"] == "svc-new"


@pytest.mark.asyncio
async def test_volume_mode_wait_failure_detaches_caller_volume_no_delete() -> None:
    """When readiness wait fails after attaching a caller-owned volume,
    cleanup detaches the volume but never deletes it."""
    client = _FakeClient()
    client.helpers.raise_on_call = TimeoutError("readiness deadline")
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
        volume_id="vol-existing",
    )
    with pytest.raises(TimeoutError):
        await sandbox_client.create(options=options)
    assert len(client._delete_service.calls) == 1
    assert len(client._detach_volume.calls) == 1
    assert client._detach_volume.calls[0]["volume_id"] == "vol-existing"
    # Caller's volume must never be deleted on cleanup.
    assert client._delete_volume.calls == []


@pytest.mark.asyncio
async def test_volume_mode_delete_detaches_caller_volume_without_deleting() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="volume",
        volume_id="vol-existing",
    )
    session = await sandbox_client.create(options=options)

    await sandbox_client.delete(session)
    # Service deleted (client-owned), volume detached, volume NOT deleted.
    assert len(client._delete_service.calls) == 1
    assert len(client._detach_volume.calls) == 1
    assert client._detach_volume.calls[0]["volume_id"] == "vol-existing"
    assert client._delete_volume.calls == []


@pytest.mark.asyncio
async def test_volume_mode_delete_attach_mode_only_detaches() -> None:
    """service_id + volume_id: the caller owns both. delete() must not
    touch the service or delete the volume — only detach the volume so
    we don't leave the caller's resources cross-wired to nothing."""
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        service_id="svc-existing",
        workspace_persistence="volume",
        volume_id="vol-existing",
    )
    session = await sandbox_client.create(options=options)

    await sandbox_client.delete(session)
    assert client._delete_service.calls == []
    assert client._delete_volume.calls == []
    assert len(client._detach_volume.calls) == 1
    assert client._detach_volume.calls[0]["data"] == {
        "nfObject": {"id": "svc-existing", "type": "service"}
    }


# -- workspace_persistence: tar mode ---------------------------------------


@pytest.mark.asyncio
async def test_tar_mode_persist_snapshot_captures_workspace_tar_into_state() -> None:
    client = _FakeClient()
    state = NorthflankSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshotSpec().build("snap-1"),
        project_id="proj",
        service_id="svc-tar",
        workspace_persistence="tar",
    )
    session = NorthflankSandboxSession(state=state, client=client)

    # When _persist_snapshot runs it should tar the workspace and stash
    # the bytes (base64) in state.persisted_workspace_tar_b64.
    payload = _make_valid_tar_bytes({"./hello.txt": b"hi\n"})
    client.files.download_payload = payload

    await session._persist_snapshot()

    assert session.state.persisted_workspace_tar_b64 is not None
    import base64

    decoded = base64.b64decode(session.state.persisted_workspace_tar_b64)
    assert decoded == payload


@pytest.mark.asyncio
async def test_tar_mode_default_persistence_is_noop_for_snapshot() -> None:
    """Without workspace_persistence set, _persist_snapshot must not embed
    a tar in state (default ephemeral behaviour unchanged)."""
    client = _FakeClient()
    state = NorthflankSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshotSpec().build("snap-1"),
        project_id="proj",
        service_id="svc-default",
        # workspace_persistence stays None (default).
    )
    session = NorthflankSandboxSession(state=state, client=client)
    await session._persist_snapshot()
    assert session.state.persisted_workspace_tar_b64 is None


@pytest.mark.asyncio
async def test_tar_mode_prepare_backend_workspace_hydrates_from_state() -> None:
    """On resume, _prepare_backend_workspace must replay the captured tar
    into the container workspace."""
    client = _FakeClient()
    payload = _make_valid_tar_bytes({"./resumed.txt": b"hello\n"})
    import base64

    state = NorthflankSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshotSpec().build("snap-1"),
        project_id="proj",
        service_id="svc-tar",
        workspace_persistence="tar",
        persisted_workspace_tar_b64=base64.b64encode(payload).decode("ascii"),
    )
    session = NorthflankSandboxSession(state=state, client=client)

    await session._prepare_backend_workspace()

    # First call: mkdir -p /workspace. Then a tar upload + extract round trip.
    assert client.exec.user_calls[0]["command"] == ["mkdir", "-p", "/workspace"]
    assert len(client.files.uploads) == 1
    extract_cmds = [
        c["command"]
        for c in client.exec.user_calls
        if len(c["command"]) > 2 and "tar" in c["command"][2]
    ]
    assert extract_cmds, "expected a tar extract command to run"


@pytest.mark.asyncio
async def test_tar_mode_state_round_trips_through_serialize() -> None:
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
        workspace_persistence="tar",
    )
    session = await sandbox_client.create(options=options)
    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    # Volume should NOT have been provisioned in tar mode.
    assert client._create_volume.calls == []
    assert inner.state.volume_id is None
    assert inner.state.owned_volume is False
    assert inner.state.workspace_persistence == "tar"

    raw = sandbox_client.serialize_session_state(inner.state)
    revived = sandbox_client.deserialize_session_state(raw)
    assert isinstance(revived, NorthflankSandboxSessionState)
    assert revived.workspace_persistence == "tar"


@pytest.mark.asyncio
async def test_default_persistence_does_not_provision_volume() -> None:
    """Default (workspace_persistence=None) must keep current behaviour:
    no volume create, no state mutation, ephemeral lifecycle only."""
    client = _FakeClient()
    sandbox_client = NorthflankSandboxClient(client=client)
    options = NorthflankSandboxClientOptions(
        project_id="proj",
        image_path="alpine",
    )
    session = await sandbox_client.create(options=options)
    inner = session._inner
    assert isinstance(inner, NorthflankSandboxSession)
    assert client._create_volume.calls == []
    assert inner.state.workspace_persistence is None
    assert inner.state.volume_id is None
    assert inner.state.owned_volume is False
    assert inner.state.persisted_workspace_tar_b64 is None

    await sandbox_client.delete(session)
    assert client._delete_volume.calls == []
    assert len(client._delete_service.calls) == 1
