import asyncio
import io
import tarfile
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

import docker.errors  # type: ignore[import-untyped]
from docker import DockerClient as DockerSDKClient
from docker.models.containers import Container  # type: ignore[import-untyped]
from docker.utils import parse_repository_tag  # type: ignore[import-untyped]

from ..entries import (
    FuseMountPattern,
    Mount,
    MountpointMountPattern,
    RcloneMountPattern,
    resolve_workspace_path,
)
from ..errors import (
    ExecTimeoutError,
    ExecTransportError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
)
from ..manifest import Manifest
from ..session import SandboxSession, SandboxSessionState
from ..session.base_sandbox_session import BaseSandboxSession
from ..session.dependencies import Dependencies
from ..session.manager import Instrumentation
from ..session.sandbox_client import BaseSandboxClient
from ..session.workspace_payloads import coerce_write_payload
from ..snapshot import SnapshotSpec, resolve_snapshot
from ..types import ExecResult
from ..util.iterator_io import IteratorIO
from ..util.retry import (
    TRANSIENT_HTTP_STATUS_CODES,
    exception_chain_has_status_code,
    retry_async,
)
from ..util.tar_utils import should_skip_tar_member

_DOCKER_EXECUTOR: Final = ThreadPoolExecutor(
    max_workers=8,
    thread_name_prefix="agents-docker-sandbox",
)


class DockerSandboxSessionState(SandboxSessionState):
    image: str
    container_id: str
    workspace_root_ready: bool = False


@dataclass(frozen=True)
class DockerSandboxClientOptions:
    image: str


class DockerSandboxSession(BaseSandboxSession):
    _docker_client: DockerSDKClient
    _container: Container
    _workspace_root_ready: bool
    _resume_workspace_probe_pending: bool
    _resume_preserves_system_state: bool

    state: DockerSandboxSessionState
    _ARCHIVE_STAGING_DIR: Path = Path("/tmp/uc-docker-archive")

    def __init__(
        self,
        *,
        docker_client: DockerSDKClient,
        container: Container,
        state: DockerSandboxSessionState,
    ) -> None:
        self._docker_client = docker_client
        self._container = container
        self.state = state
        self._workspace_root_ready = state.workspace_root_ready
        self._resume_workspace_probe_pending = False
        self._resume_preserves_system_state = False

    @classmethod
    def from_state(
        cls,
        state: DockerSandboxSessionState,
        *,
        container: Container,
        docker_client: DockerSDKClient,
    ) -> "DockerSandboxSession":
        return cls(docker_client=docker_client, container=container, state=state)

    @property
    def container_id(self) -> str:
        return self.state.container_id

    def _archive_stage_path(self, *, name_hint: str) -> Path:
        # Unique name avoids clashes across concurrent reads/writes.
        return self._ARCHIVE_STAGING_DIR / f"{uuid.uuid4().hex}_{name_hint}"

    async def _stage_workspace_copy(self) -> tuple[Path, Path]:
        root = Path(self.state.manifest.root)
        root_name = root.name or "workspace"
        staging_parent = self._archive_stage_path(name_hint="workspace")
        staging_workspace = staging_parent / root_name

        await self._exec_checked(
            "mkdir",
            "-p",
            str(staging_parent),
            error_cls=WorkspaceArchiveReadError,
            error_path=root,
        )
        await self._exec_checked(
            "cp",
            "-R",
            "--",
            str(root),
            str(staging_workspace),
            error_cls=WorkspaceArchiveReadError,
            error_path=root,
        )
        return staging_parent, staging_workspace

    async def _rm_best_effort(self, path: Path) -> None:
        try:
            await self.exec("rm", "-rf", "--", str(path), shell=False)
        except Exception:
            pass

    async def _exec_checked(
        self,
        *cmd: str | Path,
        error_cls: type[WorkspaceArchiveReadError] | type[WorkspaceArchiveWriteError],
        error_path: Path,
    ) -> ExecResult:
        res = await self.exec(*cmd, shell=False)
        if not res.ok():
            raise error_cls(
                path=error_path,
                context={
                    "command": [str(c) for c in cmd],
                    "stdout": res.stdout.decode("utf-8", errors="replace"),
                    "stderr": res.stderr.decode("utf-8", errors="replace"),
                },
            )
        return res

    async def start(self) -> None:
        self._container.reload()
        if not await self.running():
            self._container.start()
        await super().start()
        self._workspace_root_ready = True
        self.state.workspace_root_ready = True
        self._resume_workspace_probe_pending = False

    async def _exec_run(
        self,
        *,
        cmd: list[str],
        workdir: str | None,
        timeout: float | None,
        command_for_errors: tuple[str | Path, ...],
        kill_on_timeout: bool,
    ) -> ExecResult:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            _DOCKER_EXECUTOR,
            lambda: self._container.exec_run(cmd=cmd, demux=True, workdir=workdir),
        )
        try:
            exec_result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as e:
            if kill_on_timeout:
                # Best-effort: kill processes matching the command line.
                # If this fails, the caller still gets a timeout error.
                try:
                    pattern = " ".join(str(c) for c in command_for_errors).replace("'", "'\\''")
                    self._container.exec_run(
                        cmd=[
                            "sh",
                            "-lc",
                            f"pkill -f -- '{pattern}' >/dev/null 2>&1 || true",
                        ],
                        demux=True,
                    )
                except Exception:
                    pass
            raise ExecTimeoutError(command=command_for_errors, timeout_s=timeout, cause=e) from e
        except Exception as e:
            raise ExecTransportError(command=command_for_errors, cause=e) from e

        stdout, stderr = exec_result.output
        return ExecResult(
            stdout=stdout or b"",
            stderr=stderr or b"",
            exit_code=exec_result.exit_code or 0,
        )

    async def _recover_workspace_root_ready(self, *, timeout: float | None) -> None:
        if self._workspace_root_ready or not self._resume_workspace_probe_pending:
            return

        root = self.state.manifest.root
        probe_command = ("test", "-d", "--", root)
        try:
            result = await self._exec_run(
                cmd=[str(c) for c in probe_command],
                workdir=None,
                timeout=timeout,
                command_for_errors=probe_command,
                kill_on_timeout=False,
            )
        except (ExecTimeoutError, ExecTransportError):
            return
        finally:
            self._resume_workspace_probe_pending = False

        if result.ok():
            self._workspace_root_ready = True
            self.state.workspace_root_ready = True

    async def _exec_internal(
        self, *command: str | Path, timeout: float | None = None
    ) -> ExecResult:
        # `docker-py` is synchronous and can block indefinitely (e.g. hung
        # process, daemon issues). Run in a worker thread so we can enforce a
        # timeout without requiring `timeout(1)` in the container image.
        # Use a shared bounded executor so repeated timeouts do not leak one
        # new thread per command.
        cmd: list[str] = [str(c) for c in command]
        await self._recover_workspace_root_ready(timeout=timeout)
        # The workspace root is created during `apply_manifest()`, so the first
        # bootstrap commands must not force Docker to chdir there yet.
        workdir = self.state.manifest.root if self._workspace_root_ready else None
        return await self._exec_run(
            cmd=cmd,
            workdir=workdir,
            timeout=timeout,
            command_for_errors=command,
            kill_on_timeout=True,
        )

    async def read(self, path: Path) -> io.IOBase:
        workspace_path = resolve_workspace_path(
            Path(self.state.manifest.root),
            path,
            allow_absolute_within_root=True,
        )

        # Docker's archive APIs (put/get) can be flaky for paths that exist *inside* the container
        # but are not visible to the Docker daemon (notably FUSE mounts like mount-s3/rclone).
        # Mirror `write()`: always stage into a daemon-visible directory, then `get_archive` there.
        staging_path = self._archive_stage_path(name_hint=workspace_path.name)

        await self._exec_checked(
            "mkdir",
            "-p",
            str(self._ARCHIVE_STAGING_DIR),
            error_cls=WorkspaceArchiveReadError,
            error_path=path,
        )

        cp_res = await self.exec("cp", "--", str(workspace_path), str(staging_path), shell=False)
        if not cp_res.ok():
            # Best-effort: treat stage failure as not-found. (It can also be permissions, but we
            # don't have a dedicated error type for that yet.)
            raise WorkspaceReadNotFoundError(
                path=path,
                context={
                    "command": ["cp", "--", str(workspace_path), str(staging_path)],
                    "stdout": cp_res.stdout.decode("utf-8", errors="replace"),
                    "stderr": cp_res.stderr.decode("utf-8", errors="replace"),
                },
            )

        try:
            stream, _ = self._container.get_archive(str(staging_path))
        except docker.errors.NotFound as e:
            raise WorkspaceReadNotFoundError(path=path, cause=e) from e
        except docker.errors.APIError as e:
            raise WorkspaceArchiveReadError(path=path, cause=e) from e
        finally:
            # Best-effort cleanup.
            await self._rm_best_effort(staging_path)

        # `get_archive` returns a tar stream. For a single-file read we buffer
        # the tar bytes so tarfile can operate in non-streaming mode (seeking
        # is required by some reads).
        try:
            raw = b"".join(stream)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
                members = tar.getmembers()
                if not members:
                    raise WorkspaceReadNotFoundError(path=path)
                extracted = tar.extractfile(members[0])
                if extracted is None:
                    raise WorkspaceReadNotFoundError(path=path)
                return io.BytesIO(extracted.read())
        except WorkspaceReadNotFoundError:
            raise
        except (tarfile.TarError, OSError) as e:
            raise WorkspaceArchiveReadError(path=path, cause=e) from e

    async def write(self, path: Path, data: io.IOBase) -> None:
        # Buffer the file first so we can set TarInfo.size correctly.
        payload = coerce_write_payload(path=path, data=data)

        path = resolve_workspace_path(
            Path(self.state.manifest.root),
            path,
            allow_absolute_within_root=True,
        )

        parent = path.parent
        await self.mkdir(parent, parents=True)

        # Docker's archive APIs (put/get) can be flaky for paths that exist *inside* the container
        # but are not visible to the Docker daemon (notably FUSE mounts like mount-s3/rclone).
        # To make writes robust across normal dirs and mountpoints, always stage the payload in
        # a daemon-visible directory and then copy into place from inside the container.
        staging_path = self._archive_stage_path(name_hint=path.name)
        staging_name = staging_path.name

        await self._exec_checked(
            "mkdir",
            "-p",
            str(self._ARCHIVE_STAGING_DIR),
            error_cls=WorkspaceArchiveWriteError,
            error_path=self._ARCHIVE_STAGING_DIR,
        )

        info = tarfile.TarInfo(name=staging_name)
        info.size = len(payload)

        tar_buf = io.BytesIO()
        with tarfile.open(fileobj=tar_buf, mode="w") as tar:
            tar.addfile(info, io.BytesIO(bytes(payload)))

        tar_buf.seek(0)
        try:
            self._container.put_archive(str(self._ARCHIVE_STAGING_DIR), tar_buf)
        except docker.errors.APIError as e:
            raise WorkspaceArchiveWriteError(path=self._ARCHIVE_STAGING_DIR, cause=e) from e

        # Copy into place using a process inside the container, which can see mounts.
        cp_res = await self.exec("cp", "--", str(staging_path), str(path), shell=False)
        if not cp_res.ok():
            raise WorkspaceArchiveWriteError(
                path=parent,
                context={
                    "command": ["cp", "--", str(staging_path), str(path)],
                    "stdout": cp_res.stdout.decode("utf-8", errors="replace"),
                    "stderr": cp_res.stderr.decode("utf-8", errors="replace"),
                },
            )

        # Best-effort cleanup. Ignore failures (e.g. concurrent cleanup).
        await self._rm_best_effort(staging_path)

    async def running(self) -> bool:
        # docker-py caches container attributes; refresh to avoid stale status,
        # especially right after start/stop.
        try:
            self._container.reload()
        except docker.errors.APIError:
            # Best-effort: if we can't reload, fall back to last known status.
            pass
        return cast(str, self._container.status) == "running"

    async def stop(self) -> None:
        # Persistence-only. Container teardown is handled in `shutdown()`.
        await super().stop()

    async def shutdown(self) -> None:
        # Best-effort: stop the container if it exists.
        try:
            self._container.reload()
        except Exception:
            pass
        try:
            if await self.running():
                self._container.stop()
        except Exception:
            # If the container is already gone/stopped, ignore.
            pass

    def should_provision_manifest_accounts_on_resume(self) -> bool:
        return not self._resume_preserves_system_state

    async def exists(self) -> bool:
        try:
            self._docker_client.containers.get(self.state.container_id)
            return True
        except docker.errors.NotFound:
            return False

    @retry_async(
        retry_if=lambda exc, self: exception_chain_has_status_code(exc, TRANSIENT_HTTP_STATUS_CODES)
    )
    async def persist_workspace(self) -> io.IOBase:
        def _error_context_summary(error: WorkspaceArchiveReadError) -> dict[str, str]:
            summary = {"message": error.message}
            if error.cause is not None:
                summary["cause_type"] = type(error.cause).__name__
                summary["cause"] = str(error.cause)
            return summary

        skip = self.state.manifest.ephemeral_persistence_paths()
        root = Path(self.state.manifest.root)
        unmounted_mounts: list[tuple[Mount, Path]] = []
        unmount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in self.state.manifest.ephemeral_mount_targets():
            try:
                await mount_entry.unmount_path(self, mount_path)
            except Exception as e:
                unmount_error = WorkspaceArchiveReadError(path=root, cause=e)
                break
            unmounted_mounts.append((mount_entry, mount_path))

        snapshot_error: WorkspaceArchiveReadError | None = None
        archive: io.IOBase | None = None
        staging_parent: Path | None = None
        if unmount_error is None:
            try:
                try:
                    staging_parent, staging_workspace = await self._stage_workspace_copy()
                    for rel_path in skip:
                        await self._rm_best_effort(staging_workspace / rel_path)

                    bits, _ = self._container.get_archive(str(staging_workspace))
                    root_name = root.name or "workspace"
                    if not skip:
                        archive = IteratorIO(it=bits)
                    else:
                        in_stream = IteratorIO(it=bits)
                        out_stream = tempfile.SpooledTemporaryFile(
                            max_size=16 * 1024 * 1024, mode="w+b"
                        )
                        try:
                            with (
                                tarfile.open(fileobj=in_stream, mode="r|*") as in_tar,
                                tarfile.open(fileobj=out_stream, mode="w") as out_tar,
                            ):
                                for member in in_tar:
                                    if should_skip_tar_member(
                                        member.name, skip_rel_paths=skip, root_name=root_name
                                    ):
                                        continue
                                    fileobj = in_tar.extractfile(member) if member.isreg() else None
                                    out_tar.addfile(member, fileobj)
                                    if fileobj is not None:
                                        fileobj.close()
                        except (tarfile.TarError, OSError) as e:
                            out_stream.close()
                            raise WorkspaceArchiveReadError(path=root, cause=e) from e

                        out_stream.seek(0)
                        archive = cast(io.IOBase, out_stream)
                except docker.errors.NotFound as e:
                    snapshot_error = WorkspaceArchiveReadError(path=root, cause=e)
                except docker.errors.APIError as e:
                    snapshot_error = WorkspaceArchiveReadError(path=root, cause=e)
                except WorkspaceArchiveReadError as e:
                    snapshot_error = e
            finally:
                if staging_parent is not None:
                    await self._rm_best_effort(staging_parent)

        remount_error: WorkspaceArchiveReadError | None = None
        for mount_entry, mount_path in reversed(unmounted_mounts):
            try:
                await mount_entry.mount(self, mount_path)
            except Exception as e:
                current_error = WorkspaceArchiveReadError(path=root, cause=e)
                if remount_error is None:
                    remount_error = current_error
                    if unmount_error is not None:
                        remount_error.context["earlier_unmount_error"] = _error_context_summary(
                            unmount_error
                        )
                else:
                    additional_remount_errors = remount_error.context.setdefault(
                        "additional_remount_errors", []
                    )
                    assert isinstance(additional_remount_errors, list)
                    additional_remount_errors.append(_error_context_summary(current_error))

        if remount_error is not None:
            if snapshot_error is not None:
                remount_error.context["snapshot_error_before_remount_corruption"] = (
                    _error_context_summary(snapshot_error)
                )
            raise remount_error
        if unmount_error is not None:
            raise unmount_error
        if snapshot_error is not None:
            raise snapshot_error

        assert archive is not None
        return archive

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self.state.manifest.root
        hydration_target = Path(root).parent
        try:
            ok = self._container.put_archive(str(hydration_target), data)
        except docker.errors.APIError as e:
            raise WorkspaceArchiveWriteError(path=Path(root), cause=e) from e
        if not ok:
            raise WorkspaceArchiveWriteError(
                path=Path(root), context={"reason": "put_archive_returned_false"}
            )


class DockerSandboxClient(BaseSandboxClient[DockerSandboxClientOptions]):
    backend_id = "docker"
    docker_client: DockerSDKClient
    _instrumentation: Instrumentation

    def __init__(
        self,
        docker_client: DockerSDKClient,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        super().__init__()
        self.docker_client = docker_client
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | None = None,
        manifest: Manifest | None = None,
        options: DockerSandboxClientOptions,
    ) -> SandboxSession:
        image = options.image

        container = await self._create_container(image, manifest=manifest)
        container.start()

        session_id = uuid.uuid4()
        container_id = container.id
        assert container_id is not None
        snapshot_id = str(session_id)
        snapshot_instance = resolve_snapshot(snapshot, snapshot_id)
        state = DockerSandboxSessionState(
            session_id=session_id,
            manifest=manifest or Manifest(),
            image=image,
            snapshot=snapshot_instance,
            container_id=container_id,
        )

        inner = DockerSandboxSession(
            docker_client=self.docker_client,
            container=container,
            state=state,
        )
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, DockerSandboxSession):
            raise TypeError("DockerSandboxClient.delete expects a DockerSandboxSession")
        try:
            container = self.docker_client.containers.get(inner.state.container_id)
        except docker.errors.NotFound:
            return session
        # Ensure teardown happens before removal.
        try:
            await inner.shutdown()
        except Exception:
            pass
        try:
            container.remove()
        except docker.errors.NotFound:
            return session
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, DockerSandboxSessionState):
            raise TypeError("DockerSandboxClient.resume expects a DockerSandboxSessionState")
        container = self.get_container(state.container_id)
        reused_existing_container = container is not None
        if container is None:
            container = await self._create_container(state.image, manifest=state.manifest)
            container_id = container.id
            assert container_id is not None
            state.container_id = container_id
            state.workspace_root_ready = False

        # Use the existing container (or the one we just created).
        inner = DockerSandboxSession(
            container=container, docker_client=self.docker_client, state=state
        )
        inner._resume_workspace_probe_pending = True
        inner._resume_preserves_system_state = reused_existing_container
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return DockerSandboxSessionState.model_validate(payload)

    async def _create_container(self, image: str, *, manifest: Manifest | None = None) -> Container:
        # create image if it does not exist
        if not self.image_exists(image):
            repo, tag = parse_repository_tag(image)
            self.docker_client.images.pull(repo, tag=tag or None, all_tags=False)

        assert self.image_exists(image)
        environment: dict[str, str] | None = None
        if manifest:
            environment = await manifest.environment.resolve()
        create_kwargs: dict[str, object] = {
            "entrypoint": ["tail"],
            "image": image,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": environment,
        }
        if _manifest_requires_fuse(manifest):
            create_kwargs.update(
                devices=["/dev/fuse"],
                cap_add=["SYS_ADMIN"],
                security_opt=["apparmor:unconfined"],
            )
        elif _manifest_requires_sys_admin(manifest):
            create_kwargs.update(
                cap_add=["SYS_ADMIN"],
                security_opt=["apparmor:unconfined"],
            )
        return self.docker_client.containers.create(**create_kwargs)

    def image_exists(self, image: str) -> bool:
        try:
            self.docker_client.images.get(image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def get_container(self, container_id: str) -> Container | None:
        try:
            return self.docker_client.containers.get(container_id)
        except docker.errors.NotFound:
            return None


def _manifest_requires_fuse(manifest: Manifest | None) -> bool:
    if manifest is None:
        return False
    for _path, artifact in manifest.iter_entries():
        if isinstance(artifact, Mount):
            mount_pattern = getattr(artifact, "mount_pattern", None)
            if isinstance(mount_pattern, (FuseMountPattern, MountpointMountPattern)):
                return True
            if isinstance(mount_pattern, RcloneMountPattern) and mount_pattern.mode == "fuse":
                return True
    return False


def _manifest_requires_sys_admin(manifest: Manifest | None) -> bool:
    if manifest is None:
        return False
    for _path, artifact in manifest.iter_entries():
        if isinstance(artifact, Mount):
            mount_pattern = getattr(artifact, "mount_pattern", None)
            if isinstance(mount_pattern, RcloneMountPattern) and mount_pattern.mode == "nfs":
                return True
    return False
