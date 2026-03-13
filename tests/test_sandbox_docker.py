from __future__ import annotations

import asyncio
import io
import shutil
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import docker.errors  # type: ignore[import-untyped]
import pytest

import agents.sandbox.sandboxes.docker as docker_sandbox
from agents.sandbox.entries import (
    AzureBlobMount,
    Dir,
    File,
    FuseMountPattern,
    RcloneMountPattern,
)
from agents.sandbox.errors import ExecTimeoutError, InvalidManifestPathError
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxSession,
    DockerSandboxSessionState,
    _manifest_requires_fuse,
    _manifest_requires_sys_admin,
)
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult


class _FakeDockerContainer:
    def __init__(self, host_root: Path) -> None:
        self._host_root = host_root
        self.status = "running"
        self.archive_calls: list[str] = []

    def reload(self) -> None:
        return

    def get_archive(self, path: str) -> tuple[object, dict[str, object]]:
        self.archive_calls.append(path)
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


class _HostBackedDockerSession(DockerSandboxSession):
    def __init__(self, *, host_root: Path, manifest: Manifest) -> None:
        container = _FakeDockerContainer(host_root)
        state = DockerSandboxSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id="snapshot"),
            image="python:3.11-slim",
            container_id="container",
        )
        super().__init__(
            docker_client=object(),
            container=container,
            state=state,
        )
        self._host_root = host_root
        self._fake_container = container

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        cmd = [str(part) for part in command]
        if cmd[:2] == ["mkdir", "-p"]:
            self._host_path(cmd[2]).mkdir(parents=True, exist_ok=True)
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if cmd[:3] == ["cp", "-R", "--"]:
            src = self._host_path(cmd[3])
            dst = self._host_path(cmd[4])
            shutil.copytree(src, dst)
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        if cmd[:2] == ["rm", "-rf"]:
            target = self._host_path(cmd[3])
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            return ExecResult(stdout=b"", stderr=b"", exit_code=0)
        raise AssertionError(f"Unexpected command: {cmd!r}")

    def _host_path(self, path: str | Path) -> Path:
        container_path = Path(path)
        return self._host_root / container_path.relative_to("/")


def _archive_member_names(archive: io.IOBase) -> list[str]:
    payload = archive.read()
    if not isinstance(payload, bytes):
        raise AssertionError(f"Expected bytes archive payload, got {type(payload)!r}")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as tar:
        return tar.getnames()


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
    assert any(name.endswith("workspace") for name in names)
    assert any(name.endswith("workspace/README.md") for name in names)


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

    assert any(name.endswith("workspace/keep.txt") for name in names)
    assert not any(name.endswith("workspace/skip.txt") for name in names)


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


def test_manifest_requires_fuse_detects_nested_mounts() -> None:
    manifest = Manifest(
        entries={
            "workspace": Dir(
                children={
                    "mount": AzureBlobMount(
                        account="account",
                        container="container",
                        mount_pattern=FuseMountPattern(),
                    )
                }
            )
        }
    )

    assert _manifest_requires_fuse(manifest) is True


def test_manifest_requires_sys_admin_detects_nested_mounts() -> None:
    manifest = Manifest(
        entries={
            "workspace": Dir(
                children={
                    "mount": AzureBlobMount(
                        account="account",
                        container="container",
                        mount_pattern=RcloneMountPattern(mode="nfs"),
                    )
                }
            )
        }
    )

    assert _manifest_requires_sys_admin(manifest) is True


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


class _ExecRunContainer:
    def __init__(self, *, workspace_exists: bool = False) -> None:
        self.exec_calls: list[dict[str, object]] = []
        self._workspace_exists = workspace_exists

    def exec_run(
        self,
        cmd: list[str],
        demux: bool = True,
        workdir: str | None = None,
    ) -> object:
        self.exec_calls.append({"cmd": cmd, "demux": demux, "workdir": workdir})
        exit_code = 0
        if cmd == ["test", "-d", "--", "/workspace"]:
            exit_code = 0 if self._workspace_exists else 1
        return type(
            "_ExecResult",
            (),
            {"output": (b"", b""), "exit_code": exit_code},
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
    ) -> None:
        self.status = status
        self.id = container_id
        self.exec_calls: list[dict[str, object]] = []
        self._workspace_exists = workspace_exists

    def reload(self) -> None:
        return

    def exec_run(
        self,
        cmd: list[str],
        demux: bool = True,
        workdir: str | None = None,
    ) -> object:
        self.exec_calls.append({"cmd": cmd, "demux": demux, "workdir": workdir})
        exit_code = 0
        if cmd == ["test", "-d", "--", "/workspace"]:
            exit_code = 0 if self._workspace_exists else 1
        return type(
            "_ExecResult",
            (),
            {"output": (b"", b""), "exit_code": exit_code},
        )()


@pytest.mark.asyncio
async def test_docker_exec_timeout_uses_shared_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _ExecRunContainer()
    session = DockerSandboxSession(
        docker_client=object(),
        container=container,
        state=DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image="python:3.11-slim",
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
            image="python:3.11-slim",
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
            image="python:3.11-slim",
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
async def test_docker_resume_preserves_workspace_readiness_from_state() -> None:
    client = DockerSandboxClient(
        docker_client=_ResumeDockerClient(_ResumeContainer(status="running"))
    )

    ready_session = await client.resume(
        DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image="python:3.11-slim",
            container_id="container",
            workspace_root_ready=True,
        )
    )
    not_ready_session = await client.resume(
        DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image="python:3.11-slim",
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

    async def _fake_create_container(image: str, *, manifest: Manifest | None = None) -> object:
        _ = (image, manifest)
        return replacement

    monkeypatch.setattr(client, "_create_container", _fake_create_container)

    resumed = await client.resume(
        DockerSandboxSessionState(
            manifest=Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id="snapshot"),
            image="python:3.11-slim",
            container_id="missing",
            workspace_root_ready=True,
        )
    )

    assert isinstance(resumed._inner, DockerSandboxSession)
    inner = resumed._inner
    assert inner.state.container_id == "replacement"
    assert inner.state.workspace_root_ready is False
    assert inner._workspace_root_ready is False
    assert inner.should_provision_manifest_accounts_on_resume() is True


@pytest.mark.asyncio
async def test_docker_resume_recovers_workspace_workdir_when_root_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _ResumeContainer(status="running", workspace_exists=True)
    client = DockerSandboxClient(docker_client=_ResumeDockerClient(container))

    payload = DockerSandboxSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=NoopSnapshot(id="snapshot"),
        image="python:3.11-slim",
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
            "cmd": ["test", "-d", "--", "/workspace"],
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
            image="python:3.11-slim",
            container_id="missing",
        ),
    )

    assert await session.exists() is False
