from __future__ import annotations

import asyncio
import builtins
import errno
import io
import queue
import shutil
import socket
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import docker.errors  # type: ignore[import-untyped]
import pytest
from pydantic import Field, PrivateAttr

import agents.sandbox.sandboxes.docker as docker_sandbox
from agents.sandbox import SandboxPathGrant
from agents.sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from agents.sandbox.entries import (
    AzureBlobMount,
    BoxMount,
    Dir,
    DockerVolumeMountStrategy,
    File,
    FuseMountPattern,
    GCSMount,
    InContainerMountStrategy,
    Mount,
    MountpointMountPattern,
    MountStrategy,
    RcloneMountPattern,
    S3FilesMount,
    S3FilesMountPattern,
    S3Mount,
)
from agents.sandbox.entries.mounts.base import InContainerMountAdapter
from agents.sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    InvalidManifestPathError,
    MountConfigError,
    PtySessionNotFoundError,
    WorkspaceArchiveWriteError,
)
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.manifest import Manifest
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxSession,
    DockerSandboxSessionState,
)
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, Permissions


class _FakeDockerContainer:
    def __init__(self, host_root: Path, *, archive_error: Exception | None = None) -> None:
        self._host_root = host_root
        self.client: object | None = None
        self.id = "container"
        self.status = "running"
        self.archive_calls: list[str] = []
        self.archive_error = archive_error

    def reload(self) -> None:
        return

    def get_archive(self, path: str) -> tuple[object, dict[str, object]]:
        self.archive_calls.append(path)
        if self.archive_error is not None:
            raise self.archive_error
        if path == "/workspace":
            raise docker.errors.APIError("root archive unsupported")

        host_path = self._host_path(path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(host_path, arcname=Path(path).name)
        buf.seek(0)
        return iter([buf.getvalue()]), {}

    def _host_path(self, path: str | Path) -> Path:
        container_path = Path(path)
        return self._host_root / container_path.relative_to("/")


class _PullRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, bool]] = []

    def pull(self, repo: str, *, tag: str | None = None, all_tags: bool = False) -> None:
        self.calls.append((repo, tag, all_tags))


class _FakeDockerClient:
    def __init__(self) -> None:
        self.images = _PullRecorder()


class _StreamingArchiveResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.headers: dict[str, str] = {}
        self.close_calls = 0

    def iter_content(self, chunk_size: int, decode: bool) -> Iterator[bytes]:
        del chunk_size, decode
        return iter(self._chunks)

    def close(self) -> None:
        self.close_calls += 1


class _StreamingArchiveAPI:
    def __init__(self, response: _StreamingArchiveResponse) -> None:
        self._response = response
        self.get_calls: list[dict[str, object]] = []
        self.stream_calls: list[tuple[int, bool]] = []

    def _url(self, template: str, container_id: str) -> str:
        return template.format(container_id)

    def _get(
        self,
        url: str,
        *,
        params: dict[str, str],
        stream: bool,
        headers: dict[str, str],
    ) -> _StreamingArchiveResponse:
        self.get_calls.append(
            {
                "url": url,
                "params": dict(params),
                "stream": stream,
                "headers": dict(headers),
            }
        )
        return self._response

    def _raise_for_status(self, response: _StreamingArchiveResponse) -> None:
        assert response is self._response

    def _stream_raw_result(
        self,
        response: _StreamingArchiveResponse,
        *,
        chunk_size: int,
        decode: bool,
    ) -> Iterator[bytes]:
        assert response is self._response
        self.stream_calls.append((chunk_size, decode))
        yield from response.iter_content(chunk_size, decode)


class _StreamingArchiveContainerClient:
    def __init__(self, api: _StreamingArchiveAPI) -> None:
        self.api = api


class _SocketStartResponse:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _SocketStartSocket:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _SocketStartAPI:
    def __init__(self) -> None:
        self.response = _SocketStartResponse()
        self.sock = _SocketStartSocket()
        self.post_calls: list[dict[str, object]] = []

    def _url(self, template: str, exec_id: str) -> str:
        return template.format(exec_id)

    def _post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        data: dict[str, object],
        stream: bool,
    ) -> _SocketStartResponse:
        self.post_calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "data": dict(data),
                "stream": stream,
            }
        )
        return self.response

    def _get_raw_response_socket(self, response: _SocketStartResponse) -> _SocketStartSocket:
        assert response is self.response
        return self.sock


class _CreateRecorder:
    def __init__(self, container: object) -> None:
        self._container = container
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(dict(kwargs))
        return self._container


class _FakeCreateDockerClient(_FakeDockerClient):
    def __init__(self, container: object) -> None:
        super().__init__()
        self.containers = _CreateRecorder(container)


class _DeleteVolume:
    def __init__(self) -> None:
        self.remove_calls = 0

    def remove(self) -> None:
        self.remove_calls += 1


class _DeleteVolumeCollection:
    def __init__(self, volumes: dict[str, _DeleteVolume]) -> None:
        self._volumes = volumes
        self.get_calls: list[str] = []

    def get(self, name: str) -> _DeleteVolume:
        self.get_calls.append(name)
        try:
            return self._volumes[name]
        except KeyError as exc:
            raise docker.errors.NotFound("volume not found") from exc


class _DeleteContainer:
    def __init__(self) -> None:
        self.status = "exited"
        self.remove_calls: list[dict[str, object]] = []
        self.stop_calls = 0

    def reload(self) -> None:
        return None

    def stop(self) -> None:
        self.stop_calls += 1

    def remove(self, **kwargs: object) -> None:
        self.remove_calls.append(kwargs)


class _DeleteContainerCollection:
    def __init__(self, container: _DeleteContainer) -> None:
        self._container = container
        self.get_calls: list[str] = []

    def get(self, container_id: str) -> _DeleteContainer:
        self.get_calls.append(container_id)
        return self._container


class _DeleteDockerClient(_FakeDockerClient):
    def __init__(
        self,
        *,
        container: _DeleteContainer,
        volumes: dict[str, _DeleteVolume],
    ) -> None:
        super().__init__()
        self.containers = _DeleteContainerCollection(container)
        self.volumes = _DeleteVolumeCollection(volumes)


class _HostBackedDockerSession(DockerSandboxSession):
    def __init__(
        self,
        *,
        host_root: Path,
        manifest: Manifest,
        event_log: list[tuple[str, str]] | None = None,
        archive_error: Exception | None = None,
    ) -> None:
        container = _FakeDockerContainer(host_root, archive_error=archive_error)
        state = DockerSandboxSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
        )
        super().__init__(
            docker_client=object(),
            container=container,
            state=state,
        )
        self._host_root = host_root
        self._fake_container = container
        self._event_log = event_log if event_log is not None else []

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd = [str(part) for part in command]
        helper_path = str(RESOLVE_WORKSPACE_PATH_HELPER.install_path)
        if cmd[:2] == ["sh", "-c"] and RESOLVE_WORKSPACE_PATH_HELPER.install_marker in cmd[2]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if cmd == ["test", "-x", helper_path]:
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if cmd and cmd[0] == helper_path:
            for_write = cmd[3]
            candidate = self._host_path(cmd[2]).resolve(strict=False)
            workspace_root = self._host_path(cmd[1]).resolve(strict=False)
            try:
                candidate.relative_to(workspace_root)
            except ValueError:
                pass
            else:
                return ExecResult(
                    stdout=self._container_path(candidate).as_posix().encode("utf-8"),
                    stderr=b"",
                    exit_code=0,
                )

            best_root: Path | None = None
            best_original = ""
            best_read_only = False
            grant_args = cmd[4:]
            assert len(grant_args) % 2 == 0
            for original_root, read_only_text in zip(
                grant_args[::2],
                grant_args[1::2],
                strict=False,
            ):
                root = self._host_path(original_root).resolve(strict=False)
                if root == root.parent:
                    return ExecResult(
                        stdout=b"",
                        stderr=(
                            f"extra path grant must not resolve to filesystem root: {original_root}"
                        ).encode(),
                        exit_code=113,
                    )
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                if best_root is None or len(root.parts) > len(best_root.parts):
                    best_root = root
                    best_original = original_root
                    best_read_only = read_only_text == "1"
            if best_root is not None:
                if for_write == "1" and best_read_only:
                    return ExecResult(
                        stdout=b"",
                        stderr=(
                            f"read-only extra path grant: {best_original}\n"
                            f"resolved path: {self._container_path(candidate).as_posix()}\n"
                        ).encode(),
                        exit_code=114,
                    )
                return ExecResult(
                    stdout=self._container_path(candidate).as_posix().encode("utf-8"),
                    stderr=b"",
                    exit_code=0,
                )
            return ExecResult(stdout=b"", stderr=b"workspace escape", exit_code=111)
        if cmd[:2] == ["mkdir", "-p"]:
            self._host_path(cmd[2]).mkdir(parents=True, exist_ok=True)
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if cmd[:3] == ["cp", "-R", "--"]:
            self._event_log.append(("cp", cmd[3]))
            src = self._host_path(cmd[3])
            dst = self._host_path(cmd[4])
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if cmd[:2] == ["cat", "--"]:
            src = self._host_path(cmd[2])
            try:
                return ExecResult(stdout=src.read_bytes(), stderr=b"", exit_code=0)
            except OSError as exc:
                return ExecResult(stdout=b"", stderr=str(exc).encode(), exit_code=1)
        if cmd[:2] == ["rm", "--"] or cmd[:3] == ["rm", "-rf", "--"]:
            recursive = cmd[1] == "-rf"
            target = self._host_path(cmd[3] if recursive else cmd[2])
            if target.is_symlink() or target.is_file():
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                return ExecResult(stdout=b"", stderr=b"", exit_code=0)
            if target.is_dir() and recursive:
                shutil.rmtree(target, ignore_errors=True)
                return ExecResult(stdout=b"", stderr=b"", exit_code=0)
            return ExecResult(stdout=b"", stderr=b"is a directory", exit_code=1)
        raise AssertionError(f"Unexpected command: {cmd!r}")

    async def ls(
        self,
        path: Path | str,
        *,
        user: object = None,
    ) -> list[FileEntry]:
        _ = user
        container_path = await self._validate_path_access(path)
        host_path = self._host_path(container_path)
        entries: list[FileEntry] = []
        for child in sorted(host_path.iterdir()):
            if child.is_dir():
                kind = EntryKind.DIRECTORY
            elif child.is_symlink():
                kind = EntryKind.SYMLINK
            else:
                kind = EntryKind.FILE
            entries.append(
                FileEntry(
                    path=(container_path / child.name).as_posix(),
                    permissions=Permissions.from_mode(child.stat().st_mode),
                    owner="root",
                    group="root",
                    size=child.stat().st_size,
                    kind=kind,
                )
            )
        return entries

    def _host_path(self, path: str | Path) -> Path:
        container_path = Path(path)
        return self._host_root / container_path.relative_to("/")

    def _container_path(self, path: Path) -> Path:
        return Path("/") / path.relative_to(self._host_root)


class _CleanupTrackingDockerSession(_HostBackedDockerSession):
    def __init__(self, *, host_root: Path, manifest: Manifest) -> None:
        super().__init__(host_root=host_root, manifest=manifest)
        self.stage_cleanup_calls: list[Path] = []
        self.last_staging_parent: Path | None = None

    async def _stage_workspace_copy(
        self,
        *,
        skip_rel_paths: set[Path],
    ) -> tuple[Path, Path]:
        staging_parent, staging_workspace = await super()._stage_workspace_copy(
            skip_rel_paths=skip_rel_paths
        )
        self.last_staging_parent = staging_parent
        return staging_parent, staging_workspace

    async def _rm_best_effort(self, path: Path) -> None:
        self.stage_cleanup_calls.append(path)
        await super()._rm_best_effort(path)


class _RecordingMount(Mount):
    type: str = f"recording_mount_{uuid.uuid4().hex}"
    mount_strategy: MountStrategy = Field(
        default_factory=lambda: InContainerMountStrategy(pattern=MountpointMountPattern())
    )
    remove_on_unmount: bool = True
    remount_marker: str | None = None
    _events: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    def bind_events(self, events: list[tuple[str, str]]) -> _RecordingMount:
        self._events = events
        return self

    def supported_in_container_patterns(
        self,
    ) -> tuple[builtins.type[MountpointMountPattern], ...]:
        return (MountpointMountPattern,)

    def supported_docker_volume_drivers(self) -> frozenset[str]:
        return frozenset({"rclone"})

    def build_docker_volume_driver_config(
        self,
        strategy: DockerVolumeMountStrategy,
    ) -> tuple[str, dict[str, str], bool]:
        _ = strategy
        raise MountConfigError(
            message="docker-volume mounts are not supported for this mount type",
            context={"mount_type": self.type},
        )

    def in_container_adapter(self) -> InContainerMountAdapter:
        mount = self

        class _Adapter(InContainerMountAdapter):
            def validate(self, strategy: InContainerMountStrategy) -> None:
                _ = strategy

            async def activate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> list[MaterializedFile]:
                _ = (strategy, base_dir)
                mount_path = mount._resolve_mount_path(session, dest)
                host_path = cast(_HostBackedDockerSession, session)._host_path(mount_path)
                host_path.mkdir(parents=True, exist_ok=True)
                mount._events.append(("mount", mount_path.as_posix()))
                if mount.remount_marker is not None:
                    (host_path / mount.remount_marker).write_text("remounted", encoding="utf-8")
                return []

            async def deactivate(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                dest: Path,
                base_dir: Path,
            ) -> None:
                _ = (strategy, base_dir)
                mount_path = mount._resolve_mount_path(session, dest)
                await self.teardown_for_snapshot(strategy, session, mount_path)

            async def teardown_for_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = strategy
                host_path = cast(_HostBackedDockerSession, session)._host_path(path)
                mount._events.append(("unmount", path.as_posix()))
                if not mount.remove_on_unmount:
                    return
                shutil.rmtree(host_path, ignore_errors=True)

            async def restore_after_snapshot(
                self,
                strategy: InContainerMountStrategy,
                session: BaseSandboxSession,
                path: Path,
            ) -> None:
                _ = strategy
                host_path = cast(_HostBackedDockerSession, session)._host_path(path)
                host_path.mkdir(parents=True, exist_ok=True)
                mount._events.append(("mount", path.as_posix()))
                if mount.remount_marker is not None:
                    (host_path / mount.remount_marker).write_text("remounted", encoding="utf-8")

        return _Adapter(self)


def _archive_member_names(archive: io.IOBase) -> list[str]:
    payload = archive.read()
    if not isinstance(payload, bytes):
        raise AssertionError(f"Expected bytes archive payload, got {type(payload)!r}")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
        return tar.getnames()


def _tar_bytes(*members: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in members:
            payload = b"pwned"
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _tar_symlink_bytes(*, name: str, target: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tar.addfile(info)
    return buf.getvalue()


class _RejectUnboundedRead(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            raise AssertionError("hydrate_workspace() must read archive streams in bounded chunks")
        return super().read(size)


@pytest.mark.asyncio
async def test_docker_persist_workspace_stages_copy_before_get_archive(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("hello from workspace", encoding="utf-8")

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )

    archive = await session.persist_workspace()

    names = _archive_member_names(archive)

    assert "/workspace" not in session._fake_container.archive_calls
    assert "." in names
    assert "README.md" in names
    assert not any(name == "workspace" or name.startswith("workspace/") for name in names)


@pytest.mark.asyncio
async def test_docker_persist_workspace_closes_archive_http_response_after_normalization(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("hello from workspace", encoding="utf-8")

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )
    payload = _tar_bytes("workspace/README.md")
    response = _StreamingArchiveResponse([payload])
    api = _StreamingArchiveAPI(response)
    session._fake_container.client = _StreamingArchiveContainerClient(api)
    session._fake_container.id = "container"

    archive = await session.persist_workspace()

    assert response.close_calls == 1
    assert _archive_member_names(archive) == ["README.md"]
    assert response.close_calls == 1


@pytest.mark.asyncio
async def test_docker_persist_workspace_defers_stage_cleanup_until_archive_close(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("hello from workspace", encoding="utf-8")

    session = _CleanupTrackingDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )

    archive = await session.persist_workspace()

    assert session.last_staging_parent is not None
    assert session.stage_cleanup_calls == []

    _ = archive.read()
    await asyncio.sleep(0)

    assert session.stage_cleanup_calls == [session.last_staging_parent]


def test_docker_start_exec_socket_closes_underlying_http_response() -> None:
    api = _SocketStartAPI()

    exec_socket = DockerSandboxSession._start_exec_socket(api=api, exec_id="exec-123", tty=True)

    assert api.post_calls == [
        {
            "url": "/exec/exec-123/start",
            "headers": {"Connection": "Upgrade", "Upgrade": "tcp"},
            "data": {"Tty": True, "Detach": False},
            "stream": True,
        }
    ]
    assert exec_socket.sock is api.sock
    assert exec_socket.raw_sock is api.sock

    exec_socket.close()

    assert api.sock.close_calls == 1
    assert api.response.close_calls == 1


@pytest.mark.asyncio
async def test_docker_persist_workspace_prunes_ephemeral_entries_from_staged_copy(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "keep.txt").write_text("keep", encoding="utf-8")
    (workspace / "skip.txt").write_text("skip", encoding="utf-8")

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            entries={
                "skip.txt": File(content=b"skip", ephemeral=True),
            },
        ),
    )

    archive = await session.persist_workspace()

    names = _archive_member_names(archive)

    assert "keep.txt" in names
    assert "skip.txt" not in names


@pytest.mark.asyncio
async def test_docker_persist_workspace_prunes_mount_paths_without_mount_lifecycle(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    mount_dir = workspace / "repo" / "mount"
    mount_dir.mkdir(parents=True)
    (mount_dir / "remote.txt").write_text("remote", encoding="utf-8")

    events: list[tuple[str, str]] = []
    mount = _RecordingMount(remount_marker="remounted.txt").bind_events(events)
    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            entries={
                "repo": Dir(
                    children={
                        "mount": mount,
                    }
                )
            },
        ),
        event_log=events,
    )

    archive = await session.persist_workspace()

    names = _archive_member_names(archive)

    assert events == []
    assert not any(name.endswith("repo/mount/remote.txt") for name in names)
    assert not (mount_dir / "remounted.txt").exists()


@pytest.mark.asyncio
async def test_docker_persist_workspace_skips_workspace_root_mount_without_traversing_remote_data(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "remote.txt").write_text("remote", encoding="utf-8")

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            entries={
                "root-mount": _RecordingMount(mount_path=Path("/workspace")),
            },
        ),
    )

    archive = await session.persist_workspace()

    names = _archive_member_names(archive)

    assert "." in names
    assert "remote.txt" not in names


@pytest.mark.asyncio
async def test_docker_persist_workspace_pruned_copy_skips_mount_subtree_but_copies_siblings(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    repo_dir = workspace / "repo"
    mount_dir = repo_dir / "mount"
    mount_dir.mkdir(parents=True)
    (repo_dir / "keep.txt").write_text("keep", encoding="utf-8")
    (mount_dir / "remote.txt").write_text("remote", encoding="utf-8")

    events: list[tuple[str, str]] = []
    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            entries={
                "repo": Dir(
                    children={
                        "mount": _RecordingMount().bind_events(events),
                    }
                )
            },
        ),
        event_log=events,
    )

    archive = await session.persist_workspace()

    names = _archive_member_names(archive)

    assert ("cp", "/workspace/repo/keep.txt") in events
    assert not any(
        path.startswith("/workspace/repo/mount") for kind, path in events if kind == "cp"
    )
    assert "repo/keep.txt" in names
    assert "repo/mount/remote.txt" not in names


@pytest.mark.asyncio
async def test_docker_persist_workspace_prunes_runtime_only_skip_paths_from_staged_copy(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    logs = workspace / "logs"
    logs.mkdir(parents=True)
    (logs / "keep.txt").write_text("keep", encoding="utf-8")
    (logs / "events.jsonl").write_text("skip", encoding="utf-8")

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )
    session.register_persist_workspace_skip_path(Path("logs/events.jsonl"))

    archive = await session.persist_workspace()

    names = _archive_member_names(archive)

    assert "logs/keep.txt" in names
    assert "logs/events.jsonl" not in names


@pytest.mark.asyncio
async def test_docker_persist_workspace_prunes_explicit_mount_path_from_staged_copy(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    actual_mount_path = workspace / "actual"
    actual_mount_path.mkdir(parents=True)
    (actual_mount_path / "remote.txt").write_text("remote", encoding="utf-8")

    mount = _RecordingMount(mount_path=Path("actual"), remove_on_unmount=False)
    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            entries={
                "logical": mount,
            },
        ),
    )

    archive = await session.persist_workspace()

    names = _archive_member_names(archive)

    assert "actual/remote.txt" not in names
    assert (actual_mount_path / "remote.txt").read_text(encoding="utf-8") == "remote"


@pytest.mark.asyncio
async def test_docker_persist_workspace_prunes_nested_mount_paths_without_mount_lifecycle(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    parent_mount_dir = workspace / "repo"
    child_mount_dir = parent_mount_dir / "sub"
    child_mount_dir.mkdir(parents=True)
    (child_mount_dir / "remote.txt").write_text("remote", encoding="utf-8")

    events: list[tuple[str, str]] = []
    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            entries={
                "repo": _RecordingMount(
                    remount_marker="parent-remounted.txt",
                ).bind_events(events),
                "child": _RecordingMount(
                    mount_path=Path("repo/sub"),
                    remount_marker="child-remounted.txt",
                ).bind_events(events),
            },
        ),
        event_log=events,
    )

    archive = await session.persist_workspace()

    names = _archive_member_names(archive)

    assert events == []
    assert "repo/remote.txt" not in names
    assert "repo/sub/remote.txt" not in names
    assert not (parent_mount_dir / "parent-remounted.txt").exists()
    assert not (child_mount_dir / "child-remounted.txt").exists()


@pytest.mark.asyncio
async def test_docker_read_and_write_reject_paths_outside_workspace_root(tmp_path: Path) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.read(Path("../secret.txt"))
    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.write(Path("../secret.txt"), io.BytesIO(b"nope"))


@pytest.mark.asyncio
async def test_docker_read_returns_file_bytes_without_archive_api(tmp_path: Path) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "hello.bin").write_bytes(b"hello\x00world")

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )

    data = await session.read(Path("hello.bin"))

    assert data.read() == b"hello\x00world"
    assert session._fake_container.archive_calls == []


@pytest.mark.asyncio
async def test_docker_normalize_path_preserves_safe_leaf_symlink_path(tmp_path: Path) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "target.txt"
    target.write_text("hello", encoding="utf-8")
    (workspace / "link.txt").symlink_to(target)

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )

    normalized = await session._validate_path_access(Path("link.txt"))  # noqa: SLF001

    assert normalized == Path("/workspace/link.txt")


@pytest.mark.asyncio
async def test_docker_read_allows_extra_path_grant(tmp_path: Path) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    extra_root = host_root / "tmp"
    workspace.mkdir(parents=True)
    extra_root.mkdir(parents=True)
    (extra_root / "result.txt").write_text("scratch output", encoding="utf-8")

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            extra_path_grants=(SandboxPathGrant(path="/tmp"),),
        ),
    )

    data = await session.read(Path("/tmp/result.txt"))

    assert data.read() == b"scratch output"


@pytest.mark.asyncio
async def test_docker_write_rejects_read_only_extra_path_grant(tmp_path: Path) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    extra_root = host_root / "tmp"
    workspace.mkdir(parents=True)
    extra_root.mkdir(parents=True)

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            extra_path_grants=(SandboxPathGrant(path="/tmp", read_only=True),),
        ),
    )

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await session.write(Path("/tmp/result.txt"), io.BytesIO(b"scratch output"))

    assert str(exc_info.value) == "failed to write archive for path: /tmp/result.txt"
    assert exc_info.value.context == {
        "path": "/tmp/result.txt",
        "reason": "read_only_extra_path_grant",
        "grant_path": "/tmp",
    }


@pytest.mark.asyncio
async def test_docker_write_rejects_workspace_symlink_to_read_only_extra_path_grant(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    extra_root = host_root / "tmp"
    workspace.mkdir(parents=True)
    extra_root.mkdir(parents=True)
    (workspace / "tmp-link").symlink_to(extra_root, target_is_directory=True)

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            extra_path_grants=(SandboxPathGrant(path="/tmp", read_only=True),),
        ),
    )

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await session.write(Path("tmp-link/result.txt"), io.BytesIO(b"scratch output"))

    assert str(exc_info.value) == "failed to write archive for path: /workspace/tmp-link/result.txt"
    assert exc_info.value.context == {
        "path": "/workspace/tmp-link/result.txt",
        "reason": "read_only_extra_path_grant",
        "grant_path": "/tmp",
        "resolved_path": "/tmp/result.txt",
    }


@pytest.mark.asyncio
async def test_docker_write_rejects_workspace_symlink_to_nested_read_only_extra_path_grant(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    extra_root = host_root / "tmp"
    protected_root = extra_root / "protected"
    workspace.mkdir(parents=True)
    protected_root.mkdir(parents=True)
    (workspace / "tmp-link").symlink_to(extra_root, target_is_directory=True)

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(
            root="/workspace",
            extra_path_grants=(
                SandboxPathGrant(path="/tmp"),
                SandboxPathGrant(path="/tmp/protected", read_only=True),
            ),
        ),
    )

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await session.write(
            Path("tmp-link/protected/result.txt"),
            io.BytesIO(b"scratch output"),
        )

    assert (
        str(exc_info.value)
        == "failed to write archive for path: /workspace/tmp-link/protected/result.txt"
    )
    assert exc_info.value.context == {
        "path": "/workspace/tmp-link/protected/result.txt",
        "reason": "read_only_extra_path_grant",
        "grant_path": "/tmp/protected",
        "resolved_path": "/tmp/protected/result.txt",
    }


@pytest.mark.asyncio
async def test_docker_rm_unlinks_safe_internal_leaf_symlink(tmp_path: Path) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    target = workspace / "target.txt"
    target.write_text("hello", encoding="utf-8")
    link = workspace / "link.txt"
    link.symlink_to(target)

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )

    await session.rm(Path("link.txt"))

    assert target.read_text(encoding="utf-8") == "hello"
    assert not link.exists()


@pytest.mark.asyncio
async def test_docker_workspace_file_ops_reject_symlink_escape(tmp_path: Path) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    outside = host_root / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside, target_is_directory=True)

    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.read(Path("link/secret.txt"))
    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.write(Path("link/secret.txt"), io.BytesIO(b"overwrite"))
    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.ls(Path("link"))
    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.mkdir(Path("link/newdir"), parents=True)
    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        await session.rm(Path("link/secret.txt"))


def test_manifest_requires_fuse_detects_nested_mounts() -> None:
    manifest = Manifest(
        entries={
            "workspace": Dir(
                children={
                    "mount": AzureBlobMount(
                        account="account",
                        container="container",
                        mount_strategy=InContainerMountStrategy(pattern=FuseMountPattern()),
                    )
                }
            )
        }
    )
    assert docker_sandbox._manifest_requires_fuse(manifest) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member_name", "reason"),
    [
        ("/etc/passwd", "absolute path"),
        ("../escape.txt", "parent traversal"),
    ],
)
async def test_docker_hydrate_workspace_rejects_unsafe_tar_members(
    tmp_path: Path,
    member_name: str,
    reason: str,
) -> None:
    session = _HostBackedDockerSession(
        host_root=tmp_path / "container",
        manifest=Manifest(root="/workspace"),
    )

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await session.hydrate_workspace(io.BytesIO(_tar_bytes(member_name)))

    assert str(exc_info.value) == "failed to write archive for path: /workspace"
    assert exc_info.value.context == {
        "path": "/workspace",
        "reason": reason,
        "member": member_name,
    }


@pytest.mark.asyncio
async def test_docker_hydrate_workspace_rejects_workspace_root_symlink(
    tmp_path: Path,
) -> None:
    session = _HostBackedDockerSession(
        host_root=tmp_path / "container",
        manifest=Manifest(root="/workspace"),
    )

    async def _unexpected_stream_into_exec(
        *,
        cmd: list[str],
        stream: io.IOBase,
        error_path: Path,
        user: object = None,
    ) -> None:
        _ = (cmd, stream, error_path, user)
        raise AssertionError("unsafe archive must be rejected before raw tar extraction")

    session._stream_into_exec = _unexpected_stream_into_exec  # type: ignore[method-assign]

    with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
        await session.hydrate_workspace(
            io.BytesIO(_tar_symlink_bytes(name=".", target="/tmp/outside"))
        )

    assert exc_info.value.context == {
        "path": "/workspace",
        "reason": "archive root symlink",
        "member": ".",
    }


@pytest.mark.asyncio
async def test_docker_hydrate_workspace_reads_archive_in_bounded_chunks(tmp_path: Path) -> None:
    host_root = tmp_path / "container"
    workspace = host_root / "workspace"
    workspace.mkdir(parents=True)
    session = _HostBackedDockerSession(
        host_root=host_root,
        manifest=Manifest(root="/workspace"),
    )

    streamed = bytearray()
    stream_cmd: list[str] | None = None

    async def _fake_stream_into_exec(
        *,
        cmd: list[str],
        stream: io.IOBase,
        error_path: Path,
        user: object = None,
    ) -> None:
        nonlocal stream_cmd
        _ = (error_path, user)
        stream_cmd = cmd
        while True:
            chunk = stream.read(7)
            if not chunk:
                break
            assert isinstance(chunk, bytes)
            streamed.extend(chunk)

    session._stream_into_exec = _fake_stream_into_exec  # type: ignore[method-assign]

    await session.hydrate_workspace(_RejectUnboundedRead(_tar_bytes("hello.txt")))

    assert bytes(streamed) == _tar_bytes("hello.txt")
    assert stream_cmd == ["tar", "-x", "-C", "/workspace"]


@pytest.mark.asyncio
async def test_docker_create_container_parses_registry_port_image_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_client = _FakeDockerClient()
    client = DockerSandboxClient(docker_client=cast(object, docker_client))

    def _missing_image(_image: str) -> bool:
        return False

    monkeypatch.setattr(client, "image_exists", _missing_image)
    with pytest.raises(AssertionError):
        await client._create_container("localhost:5000/myimg:latest")

    assert docker_client.images.calls == [("localhost:5000/myimg", "latest", False)]


@pytest.mark.asyncio
async def test_docker_create_container_publishes_exposed_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(
        DEFAULT_PYTHON_SANDBOX_IMAGE, exposed_ports=(8765, 9000)
    )

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": None,
            "ports": {
                "8765/tcp": ("127.0.0.1", None),
                "9000/tcp": ("127.0.0.1", None),
            },
        }
    ]


@pytest.mark.asyncio
async def test_docker_create_container_mounts_s3_with_volume_driver_ignoring_mount_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    session_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="key-id",
                secret_access_key="secret",
                read_only=False,
                prefix="logs/",
                region="us-west-2",
                endpoint_url="https://s3.example.test",
                mount_strategy=DockerVolumeMountStrategy(
                    driver="mountpoint",
                    driver_options={"allow_other": "true"},
                ),
            )
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(
        DEFAULT_PYTHON_SANDBOX_IMAGE,
        manifest=manifest,
        session_id=session_id,
    )

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": {},
            "mounts": [
                {
                    "Target": "/workspace/data",
                    "Source": (
                        "sandbox_12345678123456781234567812345678_ac6cdb3eb035_workspace_data"
                    ),
                    "Type": "volume",
                    "ReadOnly": False,
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "mountpoint",
                            "Options": {
                                "bucket": "bucket",
                                "access_key_id": "key-id",
                                "secret_access_key": "secret",
                                "endpoint_url": "https://s3.example.test",
                                "region": "us-west-2",
                                "prefix": "logs/",
                                "allow_other": "true",
                            },
                        }
                    },
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_docker_create_container_mounts_s3_with_rclone_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    session_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                access_key_id="key-id",
                secret_access_key="secret",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(
        DEFAULT_PYTHON_SANDBOX_IMAGE,
        manifest=manifest,
        session_id=session_id,
    )

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": {},
            "mounts": [
                {
                    "Target": "/workspace/data",
                    "Source": (
                        "sandbox_12345678123456781234567812345678_ac6cdb3eb035_workspace_data"
                    ),
                    "Type": "volume",
                    "ReadOnly": True,
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "rclone",
                            "Options": {
                                "type": "s3",
                                "s3-provider": "AWS",
                                "path": "bucket",
                                "s3-access-key-id": "key-id",
                                "s3-secret-access-key": "secret",
                            },
                        }
                    },
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_docker_create_container_mounts_gcs_with_rclone_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    manifest = Manifest(
        entries={
            "data": GCSMount(
                bucket="bucket",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                service_account_file="/data/config/gcs.json",
            )
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(DEFAULT_PYTHON_SANDBOX_IMAGE, manifest=manifest)

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": {},
            "mounts": [
                {
                    "Target": "/workspace/data",
                    "Source": "sandbox_ac6cdb3eb035_workspace_data",
                    "Type": "volume",
                    "ReadOnly": True,
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "rclone",
                            "Options": {
                                "type": "google cloud storage",
                                "path": "bucket",
                                "gcs-service-account-file": "/data/config/gcs.json",
                            },
                        }
                    },
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_docker_create_container_mounts_gcs_hmac_with_rclone_s3_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    manifest = Manifest(
        entries={
            "data": GCSMount(
                bucket="bucket",
                access_id="access-id",
                secret_access_key="secret-key",
                prefix="prefix/",
                region="auto",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                read_only=False,
            )
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(DEFAULT_PYTHON_SANDBOX_IMAGE, manifest=manifest)

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": {},
            "mounts": [
                {
                    "Target": "/workspace/data",
                    "Source": "sandbox_ac6cdb3eb035_workspace_data",
                    "Type": "volume",
                    "ReadOnly": False,
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "rclone",
                            "Options": {
                                "type": "s3",
                                "path": "bucket/prefix/",
                                "s3-provider": "GCS",
                                "s3-access-key-id": "access-id",
                                "s3-secret-access-key": "secret-key",
                                "s3-endpoint": "https://storage.googleapis.com",
                                "s3-region": "auto",
                            },
                        }
                    },
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_docker_create_container_mounts_azure_with_rclone_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    manifest = Manifest(
        entries={
            "data": AzureBlobMount(
                account="acct",
                container="container",
                endpoint="https://blob.example.test",
                identity_client_id="client-id",
                account_key="account-key",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            )
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(DEFAULT_PYTHON_SANDBOX_IMAGE, manifest=manifest)

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": {},
            "mounts": [
                {
                    "Target": "/workspace/data",
                    "Source": "sandbox_ac6cdb3eb035_workspace_data",
                    "Type": "volume",
                    "ReadOnly": True,
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "rclone",
                            "Options": {
                                "type": "azureblob",
                                "path": "container",
                                "azureblob-account": "acct",
                                "azureblob-endpoint": "https://blob.example.test",
                                "azureblob-msi-client-id": "client-id",
                                "azureblob-key": "account-key",
                            },
                        }
                    },
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_docker_create_container_mounts_box_with_rclone_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    manifest = Manifest(
        entries={
            "data": BoxMount(
                path="/Shared/Finance",
                client_id="client-id",
                client_secret="client-secret",
                access_token="access-token",
                root_folder_id="12345",
                impersonate="user-42",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
                read_only=False,
            )
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(DEFAULT_PYTHON_SANDBOX_IMAGE, manifest=manifest)

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": {},
            "mounts": [
                {
                    "Target": "/workspace/data",
                    "Source": "sandbox_ac6cdb3eb035_workspace_data",
                    "Type": "volume",
                    "ReadOnly": False,
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "rclone",
                            "Options": {
                                "type": "box",
                                "path": "Shared/Finance",
                                "box-client-id": "client-id",
                                "box-client-secret": "client-secret",
                                "box-access-token": "access-token",
                                "box-root-folder-id": "12345",
                                "box-impersonate": "user-42",
                            },
                        }
                    },
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_docker_delete_removes_generated_docker_volumes() -> None:
    session_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
            "in-container": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            ),
        }
    )
    expected_volume_name = "sandbox_12345678123456781234567812345678_ac6cdb3eb035_workspace_data"
    container = _DeleteContainer()
    volume = _DeleteVolume()
    docker_client = _DeleteDockerClient(
        container=container,
        volumes={expected_volume_name: volume},
    )
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    inner = DockerSandboxSession(
        docker_client=cast(object, docker_client),
        container=container,
        state=DockerSandboxSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            session_id=session_id,
        ),
    )
    session = client._wrap_session(inner, instrumentation=client._instrumentation)

    deleted = await client.delete(session)

    assert deleted is session
    assert docker_client.containers.get_calls == ["container"]
    assert container.remove_calls == [{}]
    assert docker_client.volumes.get_calls == [expected_volume_name]
    assert volume.remove_calls == 1


@pytest.mark.asyncio
async def test_docker_clear_workspace_root_on_resume_preserves_nested_docker_volume_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LsEntry:
        def __init__(self, path: str, kind: EntryKind) -> None:
            self.path = path
            self.kind = kind

    manifest = Manifest(
        entries={
            "a/b": S3Mount(
                bucket="bucket",
                mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
            ),
        }
    )
    session = DockerSandboxSession(
        docker_client=object(),
        container=_ResumeContainer(status="running", workspace_exists=True),
        state=DockerSandboxSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
        ),
    )
    ls_calls: list[Path] = []
    rm_calls: list[tuple[Path, bool]] = []

    async def _fake_ls(path: Path | str) -> list[_LsEntry]:
        rendered = Path(path)
        ls_calls.append(rendered)
        if rendered == Path("/workspace"):
            return [
                _LsEntry("/workspace/a", EntryKind.DIRECTORY),
                _LsEntry("/workspace/root.txt", EntryKind.FILE),
            ]
        if rendered == Path("/workspace/a"):
            return [
                _LsEntry("/workspace/a/b", EntryKind.DIRECTORY),
                _LsEntry("/workspace/a/local.txt", EntryKind.FILE),
            ]
        raise AssertionError(f"unexpected ls path: {rendered}")

    async def _fake_rm(path: Path | str, *, recursive: bool = False) -> None:
        rm_calls.append((Path(path), recursive))

    monkeypatch.setattr(session, "ls", _fake_ls)
    monkeypatch.setattr(session, "rm", _fake_rm)

    await session._clear_workspace_root_on_resume()

    assert ls_calls == [Path("/workspace"), Path("/workspace/a")]
    assert rm_calls == [
        (Path("/workspace/a/local.txt"), True),
        (Path("/workspace/root.txt"), True),
    ]


def test_docker_volume_name_is_collision_safe_for_separator_aliases() -> None:
    session_id = uuid.UUID("12345678-1234-5678-1234-567812345678")

    assert (
        docker_sandbox._docker_volume_name(
            session_id=session_id,
            mount_path=Path("/workspace/a_b"),
        )
        == "sandbox_12345678123456781234567812345678_e00b2d707edb_workspace_a_b"
    )
    assert (
        docker_sandbox._docker_volume_name(
            session_id=session_id,
            mount_path=Path("/workspace/a/b"),
        )
        == "sandbox_12345678123456781234567812345678_212366248685_workspace_a_b"
    )


def test_docker_volume_name_uses_strictly_safe_suffix_characters() -> None:
    assert (
        docker_sandbox._docker_volume_name(
            session_id=None,
            mount_path=Path("/workspace/data set/@prod"),
        )
        == "sandbox_fe44fda0e4f6_workspace_data_set__prod"
    )


@pytest.mark.asyncio
async def test_docker_create_container_rejects_unknown_mount_subclasses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    manifest = Manifest(
        entries={
            "custom": _RecordingMount(mount_strategy=DockerVolumeMountStrategy(driver="rclone"))
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    with pytest.raises(
        MountConfigError,
        match="docker-volume mounts are not supported for this mount type",
    ):
        await client._create_container(DEFAULT_PYTHON_SANDBOX_IMAGE, manifest=manifest)

    assert docker_client.containers.calls == []


def test_s3_files_mount_rejects_docker_volume_mount() -> None:
    with pytest.raises(
        MountConfigError,
        match="invalid Docker volume driver",
    ):
        S3FilesMount(
            file_system_id="fs-1234567890abcdef0",
            mount_strategy=DockerVolumeMountStrategy(driver="rclone"),
        )


@pytest.mark.asyncio
async def test_docker_create_container_grants_fuse_for_in_container_rclone_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    manifest = Manifest(
        entries={
            "data": S3Mount(
                bucket="bucket",
                mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
            )
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(DEFAULT_PYTHON_SANDBOX_IMAGE, manifest=manifest)

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": {},
            "devices": ["/dev/fuse"],
            "cap_add": ["SYS_ADMIN"],
            "security_opt": ["apparmor:unconfined"],
        }
    ]


@pytest.mark.asyncio
async def test_docker_create_container_grants_sys_admin_for_s3_files_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="created")
    docker_client = _FakeCreateDockerClient(container)
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    manifest = Manifest(
        entries={
            "data": S3FilesMount(
                file_system_id="fs-1234567890abcdef0",
                mount_strategy=InContainerMountStrategy(pattern=S3FilesMountPattern()),
            )
        }
    )

    monkeypatch.setattr(client, "image_exists", lambda _image: True)

    created = await client._create_container(DEFAULT_PYTHON_SANDBOX_IMAGE, manifest=manifest)

    assert created is container
    assert docker_client.containers.calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": {},
            "cap_add": ["SYS_ADMIN"],
            "security_opt": ["apparmor:unconfined"],
        }
    ]


class _ExecRunContainer:
    def __init__(
        self,
        *,
        workspace_exists: bool = False,
        exec_exit_code: int | None = 0,
        exec_output: tuple[bytes | None, bytes | None] = (b"", b""),
    ) -> None:
        self.exec_calls: list[dict[str, object]] = []
        self._workspace_exists = workspace_exists
        self._exec_exit_code = exec_exit_code
        self._exec_output = exec_output

    def exec_run(
        self,
        cmd: list[str],
        demux: bool = True,
        workdir: str | None = None,
        user: str = "",
    ) -> object:
        call: dict[str, object] = {"cmd": cmd, "demux": demux, "workdir": workdir}
        if user:
            call["user"] = user
        self.exec_calls.append(call)
        exit_code = self._exec_exit_code
        if cmd == ["test", "-d", "/workspace"]:
            exit_code = 0 if self._workspace_exists else 1
        return type(
            "_ExecResult",
            (),
            {"output": self._exec_output, "exit_code": exit_code},
        )()


class _ResumeDockerClient:
    def __init__(self, container: object) -> None:
        self._container = container
        self.containers = self

    def get(self, container_id: str) -> object:
        _ = container_id
        if isinstance(self._container, BaseException):
            raise self._container
        return self._container


class _PositionalOnlyMissingDockerClient:
    def __init__(self) -> None:
        self.containers = self

    def get(self, container_id: str, /) -> object:
        _ = container_id
        raise docker.errors.NotFound("missing")


class _ResumeContainer:
    def __init__(
        self,
        *,
        status: str,
        container_id: str = "container",
        workspace_exists: bool = False,
        published_ports: dict[str, list[dict[str, str]] | None] | None = None,
    ) -> None:
        self.status = status
        self.id = container_id
        self.exec_calls: list[dict[str, object]] = []
        self._workspace_exists = workspace_exists
        self.attrs = {"NetworkSettings": {"Ports": published_ports or {}}}

    def reload(self) -> None:
        return

    def exec_run(
        self,
        cmd: list[str],
        demux: bool = True,
        workdir: str | None = None,
        user: str = "",
    ) -> object:
        call: dict[str, object] = {"cmd": cmd, "demux": demux, "workdir": workdir}
        if user:
            call["user"] = user
        self.exec_calls.append(call)
        exit_code = 0
        if cmd == ["test", "-d", "/workspace"]:
            exit_code = 0 if self._workspace_exists else 1
        return type(
            "_ExecResult",
            (),
            {"output": (b"", b""), "exit_code": exit_code},
        )()


class _FakePtySocket:
    def __init__(self, api: _FakePtyApi, *, initial_chunks: list[bytes] | None = None) -> None:
        self._api = api
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self.sent: list[bytes] = []
        self.shutdown_calls: list[int] = []
        self.closed = False
        for chunk in initial_chunks or []:
            self._chunks.put(chunk)

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)
        self._api.running = False
        self._api.exit_code = 0
        self._chunks.put(payload)
        self._chunks.put(None)

    def close(self) -> None:
        self.closed = True
        self._chunks.put(None)

    def shutdown(self, how: int) -> None:
        self.shutdown_calls.append(how)


class _FakePtyApi:
    def __init__(self, *, socket: _FakePtySocket | None = None) -> None:
        self.socket = socket or _FakePtySocket(self)
        self.running = True
        self.exit_code: int | None = None
        self.exec_create_calls: list[dict[str, object]] = []
        self.exec_start_calls: list[dict[str, object]] = []
        self.exec_inspect_calls: list[str] = []

    def exec_create(self, container_id: str, cmd: list[str], **kwargs: object) -> dict[str, str]:
        self.exec_create_calls.append({"container_id": container_id, "cmd": cmd, **kwargs})
        return {"Id": "exec-123"}

    def exec_start(self, exec_id: str, **kwargs: object) -> _FakePtySocket:
        self.exec_start_calls.append({"exec_id": exec_id, **kwargs})
        return self.socket

    def exec_inspect(self, exec_id: str) -> dict[str, object]:
        self.exec_inspect_calls.append(exec_id)
        return {
            "Running": self.running,
            "ExitCode": self.exit_code,
        }


class _FakePtyDockerClient:
    def __init__(self, api: _FakePtyApi) -> None:
        self.api = api


class _FakePtyContainer:
    def __init__(self, api: _FakePtyApi) -> None:
        self.id = "container"
        self.client = _FakePtyDockerClient(api)
        self.status = "running"
        self.exec_calls: list[dict[str, object]] = []

    def reload(self) -> None:
        return

    def exec_run(
        self,
        cmd: list[str],
        demux: bool = True,
        workdir: str | None = None,
        user: str = "",
    ) -> object:
        call: dict[str, object] = {"cmd": cmd, "demux": demux, "workdir": workdir}
        if user:
            call["user"] = user
        self.exec_calls.append(call)
        return type(
            "_ExecResult",
            (),
            {"output": (b"", b""), "exit_code": 0},
        )()


def _fake_frames_iter(socket: _FakePtySocket, *, tty: bool) -> object:
    _ = tty
    while True:
        chunk = socket._chunks.get(timeout=1)
        if chunk is None:
            return
        yield 1, chunk


def _assert_pty_exec_create_call(
    call: dict[str, object],
    *,
    command_suffix: list[str],
    tty: bool,
) -> None:
    assert call["container_id"] == "container"
    assert call["stdin"] is True
    assert call["stdout"] is True
    assert call["stderr"] is True
    assert call["tty"] is tty
    assert call["workdir"] == "/workspace"
    cmd = cast(list[str], call["cmd"])
    assert cmd[:3] == [
        "sh",
        "-lc",
        'mkdir -p "$1" && printf "%s" "$$" > "$2" && shift 2 && exec "$@"',
    ]
    assert cmd[3] == "sh"
    assert cmd[-len(command_suffix) :] == command_suffix


def _assert_pty_kill_call(call: dict[str, object]) -> None:
    assert call["demux"] is True
    assert call["workdir"] is None
    cmd = cast(list[str], call["cmd"])
    assert cmd[:3] == [
        "sh",
        "-lc",
        (
            'if [ -f "$1" ]; then '
            'pid="$(cat "$1" 2>/dev/null || true)"; '
            'if [ -n "$pid" ]; then kill -KILL "$pid" >/dev/null 2>&1 || true; fi; '
            "fi"
        ),
    ]
    assert cmd[3] == "sh"


@pytest.mark.asyncio
async def test_docker_exec_timeout_uses_shared_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _ExecRunContainer()
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
        ),
    )

    submitted_executors: list[object] = []
    loop = asyncio.get_running_loop()

    def fake_run_in_executor(executor: object, func: object) -> asyncio.Future[object]:
        _ = func
        submitted_executors.append(executor)
        return asyncio.Future()

    monkeypatch.setattr(loop, "run_in_executor", fake_run_in_executor)

    with pytest.raises(ExecTimeoutError):
        await session._exec_internal("sleep", "10", timeout=0.01)
    with pytest.raises(ExecTimeoutError):
        await session._exec_internal("sleep", "20", timeout=0.01)

    assert submitted_executors == [
        docker_sandbox._DOCKER_EXECUTOR,
        docker_sandbox._DOCKER_EXECUTOR,
    ]
    assert container.exec_calls == [
        {
            "cmd": ["sh", "-lc", "pkill -f -- 'sleep 10' >/dev/null 2>&1 || true"],
            "demux": True,
            "workdir": None,
        },
        {
            "cmd": ["sh", "-lc", "pkill -f -- 'sleep 20' >/dev/null 2>&1 || true"],
            "demux": True,
            "workdir": None,
        },
    ]


@pytest.mark.asyncio
async def test_docker_exec_omits_workdir_until_workspace_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ExecRunContainer()
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
        ),
    )

    loop = asyncio.get_running_loop()

    def fake_run_in_executor(
        executor: object, func: Callable[[], object]
    ) -> asyncio.Future[object]:
        _ = executor
        future: asyncio.Future[object] = asyncio.Future()
        future.set_result(func())
        return future

    monkeypatch.setattr(loop, "run_in_executor", fake_run_in_executor)

    result = await session._exec_internal("find", ".", timeout=0.01)

    assert result.ok()
    assert container.exec_calls == [
        {
            "cmd": ["find", "."],
            "demux": True,
            "workdir": None,
        }
    ]


@pytest.mark.asyncio
async def test_docker_exec_unknown_exit_code_is_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ExecRunContainer(
        exec_exit_code=None,
        exec_output=(b"partial stdout", b"partial stderr"),
    )
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
        ),
    )

    loop = asyncio.get_running_loop()

    def fake_run_in_executor(
        executor: object, func: Callable[[], object]
    ) -> asyncio.Future[object]:
        _ = executor
        future: asyncio.Future[object] = asyncio.Future()
        future.set_result(func())
        return future

    monkeypatch.setattr(loop, "run_in_executor", fake_run_in_executor)

    with pytest.raises(ExecTransportError) as exc_info:
        await session._exec_internal("find", ".", timeout=0.01)

    assert exc_info.value.context == {
        "command": ("find", "."),
        "command_str": "find .",
        "reason": "missing_exit_code",
        "stdout": "partial stdout",
        "stderr": "partial stderr",
        "workdir": None,
        "retry_safe": True,
    }
    assert container.exec_calls == [
        {
            "cmd": ["find", "."],
            "demux": True,
            "workdir": None,
        }
    ]


@pytest.mark.asyncio
async def test_docker_exec_uses_manifest_root_as_workdir_after_workspace_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ExecRunContainer()
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
        ),
    )
    session._workspace_root_ready = True

    loop = asyncio.get_running_loop()

    def fake_run_in_executor(
        executor: object, func: Callable[[], object]
    ) -> asyncio.Future[object]:
        _ = executor
        future: asyncio.Future[object] = asyncio.Future()
        future.set_result(func())
        return future

    monkeypatch.setattr(loop, "run_in_executor", fake_run_in_executor)

    result = await session._exec_internal("find", ".", timeout=0.01)

    assert result.ok()
    assert container.exec_calls == [
        {
            "cmd": ["find", "."],
            "demux": True,
            "workdir": "/workspace",
        }
    ]


@pytest.mark.asyncio
async def test_docker_exec_uses_native_docker_user_without_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ExecRunContainer()
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
        ),
    )

    loop = asyncio.get_running_loop()

    def fake_run_in_executor(
        executor: object, func: Callable[[], object]
    ) -> asyncio.Future[object]:
        _ = executor
        future: asyncio.Future[object] = asyncio.Future()
        future.set_result(func())
        return future

    monkeypatch.setattr(loop, "run_in_executor", fake_run_in_executor)

    result = await session.exec("whoami", timeout=0.01, user="sandbox-user")

    assert result.ok()
    assert container.exec_calls == [
        {
            "cmd": ["sh", "-lc", "whoami"],
            "demux": True,
            "workdir": None,
            "user": "sandbox-user",
        }
    ]


@pytest.mark.asyncio
async def test_docker_resolve_exposed_port_reads_published_port_mapping() -> None:
    session = DockerSandboxSession(
        docker_client=object(),
        container=_ResumeContainer(
            status="running",
            published_ports={
                "8765/tcp": [
                    {
                        "HostIp": "127.0.0.1",
                        "HostPort": "45123",
                    }
                ]
            },
        ),
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            exposed_ports=(8765,),
        ),
    )

    endpoint = await session.resolve_exposed_port(8765)

    assert endpoint.host == "127.0.0.1"
    assert endpoint.port == 45123
    assert endpoint.tls is False


@pytest.mark.asyncio
async def test_docker_resume_preserves_workspace_readiness_from_state() -> None:
    client = DockerSandboxClient(
        docker_client=_ResumeDockerClient(_ResumeContainer(status="running"))
    )

    ready_session = await client.resume(
        DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=True,
        )
    )
    not_ready_session = await client.resume(
        DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=False,
        )
    )

    assert isinstance(ready_session._inner, DockerSandboxSession)
    assert ready_session._inner._workspace_root_ready is True
    assert ready_session._inner.should_provision_manifest_accounts_on_resume() is False
    assert isinstance(not_ready_session._inner, DockerSandboxSession)
    assert not_ready_session._inner._workspace_root_ready is False
    assert not_ready_session._inner.should_provision_manifest_accounts_on_resume() is False


@pytest.mark.asyncio
async def test_docker_resume_resets_workspace_readiness_when_container_is_recreated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DockerSandboxClient(
        docker_client=cast(object, _ResumeDockerClient(docker.errors.NotFound("missing")))
    )
    replacement = _ResumeContainer(status="created", container_id="replacement")
    create_calls: list[tuple[str, Manifest | None, tuple[int, ...]]] = []

    async def _fake_create_container(
        image: str,
        *,
        manifest: Manifest | None = None,
        exposed_ports: tuple[int, ...] = (),
        session_id: uuid.UUID | None = None,
    ) -> object:
        _ = session_id
        create_calls.append((image, manifest, exposed_ports))
        return replacement

    monkeypatch.setattr(client, "_create_container", _fake_create_container)

    resumed = await client.resume(
        DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="missing",
            workspace_root_ready=True,
            exposed_ports=(8765,),
        )
    )

    assert isinstance(resumed._inner, DockerSandboxSession)
    inner = resumed._inner
    assert inner.state.container_id == "replacement"
    assert inner.state.workspace_root_ready is False
    assert inner._workspace_root_ready is False
    assert inner.should_provision_manifest_accounts_on_resume() is True
    assert create_calls == [(DEFAULT_PYTHON_SANDBOX_IMAGE, inner.state.manifest, (8765,))]


@pytest.mark.asyncio
async def test_docker_resume_recovers_workspace_workdir_when_root_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="running", workspace_exists=True)
    client = DockerSandboxClient(docker_client=_ResumeDockerClient(container))

    payload = DockerSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        image=DEFAULT_PYTHON_SANDBOX_IMAGE,
        container_id="container",
        workspace_root_ready=True,
    ).model_dump(mode="json")
    payload.pop("workspace_root_ready")

    resumed = await client.resume(client.deserialize_session_state(payload))
    assert isinstance(resumed._inner, DockerSandboxSession)

    loop = asyncio.get_running_loop()

    def fake_run_in_executor(
        executor: object, func: Callable[[], object]
    ) -> asyncio.Future[object]:
        _ = executor
        future: asyncio.Future[object] = asyncio.Future()
        future.set_result(func())
        return future

    monkeypatch.setattr(loop, "run_in_executor", fake_run_in_executor)

    result = await resumed._inner._exec_internal("find", ".", timeout=0.01)

    assert result.ok()
    assert resumed._inner.state.workspace_root_ready is True
    assert resumed._inner._workspace_root_ready is True
    assert container.exec_calls == [
        {
            "cmd": ["test", "-d", "/workspace"],
            "demux": True,
            "workdir": None,
        },
        {
            "cmd": ["find", "."],
            "demux": True,
            "workdir": "/workspace",
        },
    ]


@pytest.mark.asyncio
async def test_docker_exists_returns_false_for_missing_container() -> None:
    session = DockerSandboxSession(
        docker_client=cast(object, _PositionalOnlyMissingDockerClient()),
        container=_ResumeContainer(status="running"),
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="missing",
        ),
    )

    assert await session.exists() is False


@pytest.mark.asyncio
async def test_docker_pty_exec_write_and_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _FakePtyApi()
    api.socket = _FakePtySocket(api, initial_chunks=[b"ready\n"])
    container = _FakePtyContainer(api)
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=True,
        ),
    )
    monkeypatch.setattr(
        "agents.sandbox.sandboxes.docker.docker_socket.frames_iter",
        _fake_frames_iter,
    )

    started = await session.pty_exec_start(
        "python3",
        shell=False,
        tty=True,
        yield_time_s=0.25,
    )

    assert started.process_id is not None
    assert started.exit_code is None
    assert started.output == b"ready\n"
    assert len(api.exec_create_calls) == 1
    _assert_pty_exec_create_call(
        api.exec_create_calls[0],
        command_suffix=["python3"],
        tty=True,
    )
    assert api.exec_start_calls == [
        {
            "exec_id": "exec-123",
            "socket": True,
            "tty": True,
        }
    ]

    updated = await session.pty_write_stdin(
        session_id=started.process_id,
        chars="hello\n",
        yield_time_s=0.25,
    )

    assert updated.process_id is None
    assert updated.exit_code == 0
    assert updated.output == b"hello\n"
    assert api.socket.sent == [b"hello\n"]

    with pytest.raises(PtySessionNotFoundError):
        await session.pty_write_stdin(session_id=started.process_id, chars="")


@pytest.mark.asyncio
async def test_docker_pty_exec_uses_native_docker_user_without_sudo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakePtyApi()
    container = _FakePtyContainer(api)
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=True,
        ),
    )
    monkeypatch.setattr(
        "agents.sandbox.sandboxes.docker.docker_socket.frames_iter",
        _fake_frames_iter,
    )

    started = await session.pty_exec_start(
        "whoami",
        shell=False,
        user="sandbox-user",
        yield_time_s=0,
    )

    assert started.process_id is not None
    assert len(api.exec_create_calls) == 1
    _assert_pty_exec_create_call(
        api.exec_create_calls[0],
        command_suffix=["whoami"],
        tty=False,
    )
    assert api.exec_create_calls[0]["user"] == "sandbox-user"
    pty_pid_path = cast(list[str], api.exec_create_calls[0]["cmd"])[5]
    assert container.exec_calls == [
        {
            "cmd": [
                "sh",
                "-lc",
                docker_sandbox._PREPARE_USER_PTY_PID_SCRIPT,
                "sh",
                pty_pid_path,
                "sandbox-user",
            ],
            "demux": True,
            "workdir": "/workspace",
        }
    ]
    await session.pty_terminate_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sendall_error",
    [
        BrokenPipeError(),
        OSError(errno.EPIPE, "broken pipe"),
    ],
)
async def test_docker_pty_write_stdin_ignores_closed_socket_errors_and_returns_exit(
    monkeypatch: pytest.MonkeyPatch,
    sendall_error: OSError,
) -> None:
    api = _FakePtyApi()
    api.socket = _FakePtySocket(api, initial_chunks=[b"ready\n"])
    container = _FakePtyContainer(api)
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=True,
        ),
    )
    monkeypatch.setattr(
        "agents.sandbox.sandboxes.docker.docker_socket.frames_iter",
        _fake_frames_iter,
    )

    started = await session.pty_exec_start(
        "python3",
        shell=False,
        tty=True,
        yield_time_s=0.25,
    )

    assert started.process_id is not None

    def _sendall(_payload: bytes) -> None:
        raise sendall_error

    api.running = False
    api.exit_code = 0
    api.socket._chunks.put(b"tail\n")
    api.socket._chunks.put(None)
    monkeypatch.setattr(api.socket, "sendall", _sendall)

    updated = await session.pty_write_stdin(
        session_id=started.process_id,
        chars="hello\n",
        yield_time_s=0.25,
    )

    assert updated.process_id is None
    assert updated.exit_code == 0
    assert updated.output == b"tail\n"


@pytest.mark.asyncio
async def test_docker_pty_non_tty_rejects_stdin_and_stop_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakePtyApi()
    api.socket = _FakePtySocket(api, initial_chunks=[b"stdout\n", b"stderr\n"])
    container = _FakePtyContainer(api)
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=True,
        ),
    )
    monkeypatch.setattr(
        "agents.sandbox.sandboxes.docker.docker_socket.frames_iter",
        _fake_frames_iter,
    )

    started = await session.pty_exec_start(
        "sh",
        "-c",
        "sleep 30",
        shell=False,
        tty=False,
        yield_time_s=0.25,
    )

    assert started.process_id is not None
    assert started.exit_code is None
    assert started.output == b"stdout\nstderr\n"
    assert api.socket.shutdown_calls == [socket.SHUT_WR]

    with pytest.raises(RuntimeError, match="stdin is not available for this process"):
        await session.pty_write_stdin(session_id=started.process_id, chars="hello")

    await session.stop()

    assert api.socket.closed is True
    assert len(container.exec_calls) == 2
    _assert_pty_kill_call(container.exec_calls[0])
    assert container.exec_calls[1]["cmd"] == [
        "rm",
        "-rf",
        "--",
        cast(list[str], api.exec_create_calls[0]["cmd"])[5],
    ]

    with pytest.raises(PtySessionNotFoundError):
        await session.pty_write_stdin(session_id=started.process_id, chars="")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["exec_create", "exec_start"])
async def test_docker_pty_exec_start_times_out_blocking_docker_startup(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    api = _FakePtyApi()
    container = _FakePtyContainer(api)
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=True,
        ),
    )

    original = getattr(api, operation)

    def _delayed_operation(*args: object, **kwargs: object) -> object:
        time.sleep(0.2)
        return original(*args, **kwargs)

    monkeypatch.setattr(api, operation, _delayed_operation)

    with pytest.raises(ExecTimeoutError):
        await session.pty_exec_start(
            "python3",
            shell=False,
            tty=True,
            timeout=0.01,
            yield_time_s=0.01,
        )

    assert len(container.exec_calls) == 2
    _assert_pty_kill_call(container.exec_calls[0])
    assert container.exec_calls[1]["cmd"] == [
        "rm",
        "-rf",
        "--",
        cast(list[str], container.exec_calls[0]["cmd"])[4],
    ]


@pytest.mark.asyncio
async def test_docker_pty_exec_returns_exit_code_for_fast_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakePtyApi()
    api.running = False
    api.exit_code = 0
    api.socket = _FakePtySocket(api, initial_chunks=[b"done\n"])
    api.socket._chunks.put(None)
    container = _FakePtyContainer(api)
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=True,
        ),
    )
    monkeypatch.setattr(
        "agents.sandbox.sandboxes.docker.docker_socket.frames_iter",
        _fake_frames_iter,
    )

    started = await session.pty_exec_start(
        "sh",
        "-c",
        "printf done",
        shell=False,
        tty=False,
        yield_time_s=0.25,
    )

    assert started.process_id is None
    assert started.exit_code == 0
    assert started.output == b"done\n"
    assert container.exec_calls == [
        {
            "cmd": [
                "rm",
                "-rf",
                "--",
                cast(list[str], api.exec_create_calls[0]["cmd"])[5],
            ],
            "demux": True,
            "workdir": "/workspace",
        }
    ]


@pytest.mark.asyncio
async def test_docker_pty_exec_waits_for_socket_drain_after_process_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _FakePtyApi()
    api.running = False
    api.exit_code = 0
    api.socket = _FakePtySocket(api)
    container = _FakePtyContainer(api)
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            container_id="container",
            workspace_root_ready=True,
        ),
    )
    release_output = threading.Event()
    original_exec_inspect = api.exec_inspect

    def _exec_inspect(exec_id: str) -> dict[str, object]:
        release_output.set()
        return original_exec_inspect(exec_id)

    def _delayed_frames_iter(socket: _FakePtySocket, *, tty: bool) -> object:
        _ = tty
        assert release_output.wait(timeout=1)
        yield 1, b"done\n"

    monkeypatch.setattr(api, "exec_inspect", _exec_inspect)
    monkeypatch.setattr(
        "agents.sandbox.sandboxes.docker.docker_socket.frames_iter",
        _delayed_frames_iter,
    )

    started = await session.pty_exec_start(
        "sh",
        "-c",
        "printf done",
        shell=False,
        tty=False,
        yield_time_s=0.25,
    )

    assert started.process_id is None
    assert started.exit_code == 0
    assert started.output == b"done\n"
    assert container.exec_calls == [
        {
            "cmd": [
                "rm",
                "-rf",
                "--",
                cast(list[str], api.exec_create_calls[0]["cmd"])[5],
            ],
            "demux": True,
            "workdir": "/workspace",
        }
    ]
