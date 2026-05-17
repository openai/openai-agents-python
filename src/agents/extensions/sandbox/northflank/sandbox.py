"""Northflank-backed sandbox client + session for the openai-agents SDK.

The sandbox runs *inside* a Northflank service container. Two modes:

* **Attach** to an existing service (``service_id`` given). The client never
  deletes the service on cleanup — it only owns the exec/file connections.
* **Ephemeral** deployment (``image_path`` given, ``service_id`` omitted).
  The client creates a deployment service on ``create()`` and deletes it on
  ``delete()``. ``owned_by_client`` is recorded in the session state so a
  resumed session can recover the same ownership decision.

Implementation notes:

* All command execution goes through the Northflank V1 exec WebSocket via
  ``client.exec.arun_service_command``. The SDK's ``shell`` field is
  forwarded verbatim to the exec proxy and only ``"none"`` is meaningful
  for direct argv invocation, so commands always run as argv lists with
  ``shell="none"``.
* File IO uses ``client.files.aupload`` / ``adownload`` because the exec
  channel decodes stdout as text. Binary payloads (read, write, tar) round
  trip through ``/tmp`` in the container plus a local tempfile; the staging
  files are cleaned up best-effort.
"""

from __future__ import annotations

import base64
import io
import shlex
import tarfile
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from ....sandbox.errors import (
    ExecTimeoutError,
    ExecTransportError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
)
from ....sandbox.manifest import Manifest
from ....sandbox.session import (
    BaseSandboxSession,
    SandboxSession,
    SandboxSessionState,
)
from ....sandbox.session.manager import Instrumentation
from ....sandbox.session.runtime_helpers import (
    RESOLVE_WORKSPACE_PATH_HELPER,
    RuntimeHelperScript,
)
from ....sandbox.session.sandbox_client import (
    BaseSandboxClient,
    BaseSandboxClientOptions,
)
from ....sandbox.session.tar_workspace import shell_tar_exclude_args
from ....sandbox.snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ....sandbox.types import ExecResult, User

try:
    from northflank import ApiCallError, AsyncApiClient
except ImportError as exc:  # pragma: no cover - import path depends on optional extras
    raise ImportError(
        "Northflank sandbox support requires the optional `northflank` extra.\n"
        "Install it with: pip install 'openai-agents[northflank]'"
    ) from exc


__all__ = [
    "NorthflankSandboxClient",
    "NorthflankSandboxClientOptions",
    "NorthflankSandboxSession",
    "NorthflankSandboxSessionState",
]


# ---------------------------------------------------------------------------
# Options + state
# ---------------------------------------------------------------------------


class NorthflankSandboxClientOptions(BaseSandboxClientOptions):
    """Configuration for :class:`NorthflankSandboxClient`.

    Provide either ``service_id`` (attach to an existing service) or
    ``image_path`` (create an ephemeral deployment service that the client
    will delete on cleanup). Providing both is an error; providing neither
    is also an error.
    """

    type: Literal["northflank"] = "northflank"
    project_id: str
    service_id: str | None = None
    team_id: str | None = None
    image_path: str | None = None
    image_credentials: str | None = None
    deployment_plan: str = "nf-compute-20"
    instances: int = 1
    instance_name: str | None = None
    container_name: str | None = None
    wait_for_ready: bool = True
    wait_timeout_s: float = 300.0
    exec_timeout_s: float = 60.0
    service_name_prefix: str = "sandbox-"

    # Container CMD/ENTRYPOINT overrides for ephemeral deployments. Stock
    # base images such as ``ubuntu:24.04`` exit immediately because their
    # default CMD finishes; supply ``docker_command="sleep infinity"`` (or a
    # similar long-lived command) so the service stays up and exec calls
    # can attach. Ignored when ``service_id`` is set.
    docker_entrypoint: str | None = None
    docker_command: str | None = None

    # Workspace persistence strategy.
    #
    # * ``None`` (default): the workspace lives in the container filesystem.
    #   Deleting the service drops the workspace; resuming requires the
    #   service to still exist with its original storage.
    # * ``"volume"``: the client provisions a Northflank volume mounted at
    #   ``manifest.root`` and attaches it to the service. The volume
    #   survives service pause/resume; ``delete()`` removes it if the
    #   client created it.
    # * ``"tar"``: at ``stop()`` time the workspace is tarred via exec,
    #   pulled down through ``files.adownload``, and embedded into the
    #   session state. On ``resume()`` (or any subsequent ``start()``)
    #   the tar is uploaded and extracted into ``manifest.root``.
    workspace_persistence: Literal["volume", "tar"] | None = None

    # Volume spec passed to ``client.create.volume()``. Forwarded into the
    # ``spec`` block verbatim, so callers can set
    # ``storageSize`` / ``accessMode`` / ``storageClassName``. Ignored
    # unless ``workspace_persistence == "volume"``.
    volume_spec: dict[str, Any] | None = None

    # Attach a pre-existing caller-owned volume instead of creating one.
    # The volume's mount path (set when the caller created the volume)
    # must already cover ``manifest.root`` — Northflank's attach API
    # does not accept a mount path override. The provider records
    # ``owned_volume=False`` so ``delete()`` detaches but never deletes
    # the volume. Mutually exclusive with ``volume_spec``.
    volume_id: str | None = None


class NorthflankSandboxSessionState(SandboxSessionState):
    """Serializable state for a Northflank-backed session.

    ``owned_by_client`` records whether the client created the service and
    is therefore allowed to delete it on cleanup. Resumed sessions inherit
    this flag from the persisted payload.

    Persistence-mode bookkeeping:

    * ``workspace_persistence`` records which strategy ``create()`` picked
      so resumed sessions keep the same behaviour.
    * ``volume_id`` / ``owned_volume`` (volume mode) track the workspace
      volume the client provisioned and whether ``delete()`` should remove
      it on cleanup.
    * ``persisted_workspace_tar_b64`` (tar mode) holds the base64-encoded
      workspace tar captured at ``stop()``; on resume the session
      uploads and extracts it back into ``manifest.root``.
    """

    type: Literal["northflank"] = "northflank"
    project_id: str
    service_id: str
    team_id: str | None = None
    instance_name: str | None = None
    container_name: str | None = None
    owned_by_client: bool = False
    exec_timeout_s: float = 60.0
    workspace_persistence: Literal["volume", "tar"] | None = None
    volume_id: str | None = None
    owned_volume: bool = False
    persisted_workspace_tar_b64: str | None = None


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _build_docker_block(*, entrypoint: str | None, command: str | None) -> dict[str, Any] | None:
    """Build the ``deployment.docker`` block from optional ENTRYPOINT/CMD overrides.

    Matches Northflank's ``CreateJobResponseDeploymentDocker`` shape:
    ``configType`` is derived from which overrides are supplied.
    """
    if entrypoint is None and command is None:
        return None
    block: dict[str, Any] = {}
    if entrypoint is not None and command is not None:
        block["configType"] = "customEntrypointCustomCommand"
        block["customEntrypoint"] = entrypoint
        block["customCommand"] = command
    elif entrypoint is not None:
        block["configType"] = "customEntrypoint"
        block["customEntrypoint"] = entrypoint
    else:
        block["configType"] = "customCommand"
        block["customCommand"] = command
    return block


def _ensure_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace")
    return bytes(value)


def _link_target_stays_inside_archive(member_name: str, link_target: str) -> bool:
    """Return True if ``link_target`` resolves inside the archive root.

    ``member_name`` is the entry's path (workspace-relative; never absolute
    by the time we get here). ``link_target`` is its symlink/hardlink
    target. The check rejects absolute targets and targets that walk above
    the archive root via ``..``.
    """
    if not link_target or link_target.startswith("/"):
        return False
    member_dir = PurePosixPath(member_name).parent
    resolved_parts: list[str] = []
    for component in (member_dir / link_target).as_posix().split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not resolved_parts:
                return False
            resolved_parts.pop()
            continue
        resolved_parts.append(component)
    return True


def _validate_tar_payload(payload: bytes, *, target_root: Path) -> None:
    """Reject tar archives that would escape ``target_root`` on extract.

    The hydration flow uploads the tar to ``/tmp`` inside the container and
    then runs ``tar -xf`` against the workspace root. We can't easily
    inject Python's ``filter='data'`` extraction policy across that
    boundary, so the archive is inspected locally before being shipped.

    Rules mirror ``filter='data'``:

    * No absolute member names; no ``..`` traversal in names.
    * Regular files and directories pass through unchanged.
    * Symlinks and hardlinks are allowed only when the link target is
      relative and resolves inside the archive root.
    * Other entry types (devices, FIFOs, etc.) are rejected outright.
    """

    try:
        tf = tarfile.open(fileobj=io.BytesIO(payload), mode="r:*")
    except tarfile.TarError as exc:
        raise WorkspaceArchiveWriteError(
            path=target_root,
            context={"reason": "invalid_tar"},
            cause=exc,
        ) from exc

    try:
        try:
            members = tf.getmembers()
        except (tarfile.TarError, OSError, EOFError) as exc:
            # Truncated archives or malformed metadata can surface here
            # rather than at ``open()``; treat any failure walking the
            # member list as an unsafe archive.
            raise WorkspaceArchiveWriteError(
                path=target_root,
                context={"reason": "tar_read_failed"},
                cause=exc,
            ) from exc

        for member in members:
            name = member.name
            if not name or name.startswith("/"):
                raise WorkspaceArchiveWriteError(
                    path=target_root,
                    context={"reason": "absolute_member", "member": name},
                )
            parts = PurePosixPath(name).parts
            if any(part == ".." for part in parts):
                raise WorkspaceArchiveWriteError(
                    path=target_root,
                    context={"reason": "traversal_member", "member": name},
                )
            if member.isfile() or member.isdir():
                continue
            if member.issym() or member.islnk():
                if not _link_target_stays_inside_archive(name, member.linkname or ""):
                    raise WorkspaceArchiveWriteError(
                        path=target_root,
                        context={
                            "reason": "unsafe_link_target",
                            "member": name,
                            "link_target": member.linkname or "",
                        },
                    )
                continue
            raise WorkspaceArchiveWriteError(
                path=target_root,
                context={
                    "reason": "unsupported_member_type",
                    "member": name,
                    "type": str(member.type),
                },
            )
    finally:
        try:
            tf.close()
        except (tarfile.TarError, OSError):
            # ``close`` can raise on malformed archives; the validation
            # outcome has already been decided so swallow.
            pass


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class NorthflankSandboxSession(BaseSandboxSession):
    """Sandbox session that runs in a Northflank service container."""

    state: NorthflankSandboxSessionState
    _client: AsyncApiClient
    _running: bool

    def __init__(
        self,
        *,
        state: NorthflankSandboxSessionState,
        client: AsyncApiClient,
    ) -> None:
        self.state = state
        self._client = client
        self._running = True

    @classmethod
    def from_state(
        cls,
        state: NorthflankSandboxSessionState,
        *,
        client: AsyncApiClient,
    ) -> NorthflankSandboxSession:
        return cls(state=state, client=client)

    # -- lifecycle hooks --------------------------------------------------

    async def _prepare_backend_workspace(self) -> None:
        # Make sure manifest.root exists inside the container before the
        # base class tries to materialize the manifest there.
        root = self.state.manifest.root
        result = await self._client.exec.arun_service_command(
            project_id=self.state.project_id,
            service_id=self.state.service_id,
            team_id=self.state.team_id,
            command=["mkdir", "-p", root],
            shell="none",
            instance_name=self.state.instance_name,
            container_name=self.state.container_name,
            timeout=self.state.exec_timeout_s,
        )
        if result.exit_code not in (0, None):
            raise WorkspaceArchiveWriteError(
                path=Path(root),
                context={
                    "stage": "prepare_backend_workspace",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )

        # Tar-mode resume: replay the captured workspace tar before the
        # base session populates the manifest. Skip when there is no
        # captured payload — on first start there is nothing to restore.
        if self.state.workspace_persistence == "tar" and self.state.persisted_workspace_tar_b64:
            payload = base64.b64decode(self.state.persisted_workspace_tar_b64)
            await self.hydrate_workspace(io.BytesIO(payload))

    async def _after_start(self) -> None:
        self._running = True

    async def _persist_snapshot(self) -> None:
        # Tar mode: capture the workspace tar into session state before
        # the base lifecycle (which also drives any user-supplied
        # snapshot framework) runs. Stored as base64 so the state stays
        # JSON-serialisable.
        if self.state.workspace_persistence == "tar":
            archive = await self.persist_workspace()
            try:
                payload = archive.read()
            finally:
                try:
                    archive.close()
                except Exception:
                    pass
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            self.state.persisted_workspace_tar_b64 = base64.b64encode(payload).decode("ascii")

        await super()._persist_snapshot()

    async def _shutdown_backend(self) -> None:
        # No persistent exec channels to tear down — each command is a
        # one-shot WebSocket. Mark the session not-running so future
        # ``running()`` calls reflect cleanup.
        self._running = False

    # -- runtime helpers + remote path validation -----------------------

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        # ``_validate_remote_path_access`` resolves symlinks and ``..``
        # components against the workspace root using a small shell helper
        # installed inside the container.
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        # Invalidate the helper-installed cache whenever we point at a
        # different Northflank service.
        return self.state.service_id

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    # -- exec -------------------------------------------------------------

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        argv = [str(c) for c in command]
        try:
            result = await self._client.exec.arun_service_command(
                project_id=self.state.project_id,
                service_id=self.state.service_id,
                team_id=self.state.team_id,
                command=argv,
                shell="none",
                instance_name=self.state.instance_name,
                container_name=self.state.container_name,
                timeout=timeout or self.state.exec_timeout_s,
            )
        except TimeoutError as exc:
            raise ExecTimeoutError(command=command, timeout_s=timeout, cause=exc) from exc
        except Exception as exc:
            raise ExecTransportError(command=command, cause=exc) from exc

        return ExecResult(
            stdout=_ensure_bytes(result.stdout),
            stderr=_ensure_bytes(result.stderr),
            exit_code=result.exit_code if result.exit_code is not None else 0,
        )

    # -- file IO ----------------------------------------------------------

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        """Read ``path`` via Northflank's file-copy API.

        Per-user file operations are not supported: Northflank's
        ``files.adownload`` does not expose a per-call user override and
        runs as whatever identity the exec proxy decided for the container
        (typically the image's default WORKDIR user, often root). Passing
        a non-None ``user`` raises :class:`NotImplementedError` so the
        mismatch is loud — fall back to ``session.exec(..., user=...)``
        when user-scoped reads are required.
        """
        if user is not None:
            raise NotImplementedError(
                "Northflank sandbox does not support per-user file operations; "
                "use session.exec(..., user=...) instead."
            )
        workspace_path = await self._validate_path_access(path)
        remote = str(workspace_path)
        basename = PurePosixPath(remote).name
        # The Northflank SDK's ``_extract_download_tar`` treats local_path
        # as a *directory* when it has no file extension and as a *file*
        # otherwise. Always pass a temp directory and look the extracted
        # file up by basename so both extension-less and extension-bearing
        # remote names behave the same.
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                await self._client.files.adownload(
                    project_id=self.state.project_id,
                    remote_path=remote,
                    local_path=tmpdir,
                    service_id=self.state.service_id,
                    team_id=self.state.team_id,
                    instance_name=self.state.instance_name,
                    container_name=self.state.container_name,
                )
            except Exception as exc:
                raise WorkspaceReadNotFoundError(path=workspace_path, cause=exc) from exc
            extracted = Path(tmpdir) / basename
            if not extracted.is_file():
                raise WorkspaceReadNotFoundError(
                    path=workspace_path,
                    context={"reason": "extracted_missing", "basename": basename},
                )
            data = extracted.read_bytes()
        return io.BytesIO(data)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        """Write ``data`` to ``path`` via Northflank's file-copy API.

        Per-user file operations are not supported (see :meth:`read` for
        the rationale). A non-None ``user`` raises
        :class:`NotImplementedError`; use ``session.exec(..., user=...)``
        when the write must happen as a specific Unix user.
        """
        if user is not None:
            raise NotImplementedError(
                "Northflank sandbox does not support per-user file operations; "
                "use session.exec(..., user=...) instead."
            )
        workspace_path = await self._validate_path_access(path, for_write=True)
        remote = PurePosixPath(str(workspace_path))
        parent = str(remote.parent)
        basename = remote.name

        # Make sure the parent directory exists in the container.
        mkdir = await self._client.exec.arun_service_command(
            project_id=self.state.project_id,
            service_id=self.state.service_id,
            team_id=self.state.team_id,
            command=["mkdir", "-p", parent],
            shell="none",
            instance_name=self.state.instance_name,
            container_name=self.state.container_name,
            timeout=self.state.exec_timeout_s,
        )
        if mkdir.exit_code not in (0, None):
            raise WorkspaceArchiveWriteError(
                path=Path(str(workspace_path)),
                context={"stage": "mkdir_parent", "stderr": mkdir.stderr},
            )

        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        # ``_upload_target`` in the Northflank SDK uses the remote_path's
        # suffix to decide whether to rename the uploaded file. Extension-
        # less targets like ``/workspace/Makefile`` get treated as
        # directories and the local basename ends up inside them. Sidestep
        # the heuristic by uploading a *staging directory* whose only entry
        # has the correct basename, with ``remote_path`` pointing at the
        # parent. The SDK then tars the directory contents into
        # ``remote_path/`` verbatim, producing ``<parent>/<basename>``
        # regardless of suffix.
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir) / "staging"
            staging.mkdir()
            (staging / basename).write_bytes(payload)
            try:
                await self._client.files.aupload(
                    project_id=self.state.project_id,
                    local_path=str(staging),
                    remote_path=parent,
                    service_id=self.state.service_id,
                    team_id=self.state.team_id,
                    instance_name=self.state.instance_name,
                    container_name=self.state.container_name,
                )
            except Exception as exc:
                raise WorkspaceArchiveWriteError(path=Path(str(workspace_path)), cause=exc) from exc

    # -- workspace tar round-trip ----------------------------------------

    async def persist_workspace(self) -> io.IOBase:
        root = self.state.manifest.root
        archive_basename = f"nf-sandbox-{uuid.uuid4().hex}.tar"
        remote_tar = f"/tmp/{archive_basename}"

        # Honour manifest ephemeral / runtime skip paths via tar excludes.
        exclude_args = shell_tar_exclude_args(self._persist_workspace_skip_relpaths())
        tar_script = " ".join(
            [
                "tar",
                *exclude_args,
                "-C",
                shlex.quote(root),
                "-cf",
                shlex.quote(remote_tar),
                ".",
            ]
        )
        tar_cmd = ["sh", "-lc", tar_script]
        tar_result = await self._client.exec.arun_service_command(
            project_id=self.state.project_id,
            service_id=self.state.service_id,
            team_id=self.state.team_id,
            command=tar_cmd,
            shell="none",
            instance_name=self.state.instance_name,
            container_name=self.state.container_name,
            timeout=self.state.exec_timeout_s,
        )
        if tar_result.exit_code not in (0, None):
            raise WorkspaceArchiveReadError(
                path=Path(root),
                context={"stage": "tar", "stderr": tar_result.stderr},
            )

        # ``adownload`` treats local_path with no suffix as a directory and
        # extracts the tar's contents into it. Pass a temp directory and
        # look up the archive by remote basename afterwards.
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                try:
                    await self._client.files.adownload(
                        project_id=self.state.project_id,
                        remote_path=remote_tar,
                        local_path=tmpdir,
                        service_id=self.state.service_id,
                        team_id=self.state.team_id,
                        instance_name=self.state.instance_name,
                        container_name=self.state.container_name,
                    )
                except Exception as exc:
                    raise WorkspaceArchiveReadError(path=Path(root), cause=exc) from exc
                extracted = Path(tmpdir) / archive_basename
                if not extracted.is_file():
                    raise WorkspaceArchiveReadError(
                        path=Path(root),
                        context={
                            "reason": "archive_missing",
                            "basename": archive_basename,
                        },
                    )
                data = extracted.read_bytes()
            finally:
                # Best-effort cleanup of the in-container staging archive.
                try:
                    await self._client.exec.arun_service_command(
                        project_id=self.state.project_id,
                        service_id=self.state.service_id,
                        team_id=self.state.team_id,
                        command=["rm", "-f", remote_tar],
                        shell="none",
                        instance_name=self.state.instance_name,
                        container_name=self.state.container_name,
                        timeout=self.state.exec_timeout_s,
                    )
                except Exception:
                    pass

        return io.BytesIO(data)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = self.state.manifest.root
        archive_basename = f"nf-sandbox-{uuid.uuid4().hex}.tar"
        remote_tar = f"/tmp/{archive_basename}"

        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        # We invoke ``tar -xf`` on the remote container so the SDK's
        # ``filter='data'`` extraction policy does not apply. Inspect every
        # member here before shipping the archive over the wire.
        _validate_tar_payload(payload, target_root=Path(root))

        # Stage the tar via a directory upload so ``remote_path``'s suffix
        # heuristic does not rename it on the way in.
        with tempfile.TemporaryDirectory() as tmpdir:
            staging = Path(tmpdir) / "staging"
            staging.mkdir()
            (staging / archive_basename).write_bytes(payload)
            try:
                await self._client.files.aupload(
                    project_id=self.state.project_id,
                    local_path=str(staging),
                    remote_path="/tmp",
                    service_id=self.state.service_id,
                    team_id=self.state.team_id,
                    instance_name=self.state.instance_name,
                    container_name=self.state.container_name,
                )
            except Exception as exc:
                raise WorkspaceArchiveWriteError(path=Path(root), cause=exc) from exc

        try:
            extract_cmd = [
                "sh",
                "-lc",
                f"mkdir -p {shlex.quote(root)} && "
                f"tar -C {shlex.quote(root)} -xf {shlex.quote(remote_tar)}",
            ]
            extract_result = await self._client.exec.arun_service_command(
                project_id=self.state.project_id,
                service_id=self.state.service_id,
                team_id=self.state.team_id,
                command=extract_cmd,
                shell="none",
                instance_name=self.state.instance_name,
                container_name=self.state.container_name,
                timeout=self.state.exec_timeout_s,
            )
            if extract_result.exit_code not in (0, None):
                raise WorkspaceArchiveWriteError(
                    path=Path(root),
                    context={
                        "stage": "tar_extract",
                        "stderr": extract_result.stderr,
                    },
                )
        finally:
            try:
                await self._client.exec.arun_service_command(
                    project_id=self.state.project_id,
                    service_id=self.state.service_id,
                    team_id=self.state.team_id,
                    command=["rm", "-f", remote_tar],
                    shell="none",
                    instance_name=self.state.instance_name,
                    container_name=self.state.container_name,
                    timeout=self.state.exec_timeout_s,
                )
            except Exception:
                pass

    # -- status -----------------------------------------------------------

    async def running(self) -> bool:
        if not self._running:
            return False
        try:
            response = await self._client.get.service(
                project_id=self.state.project_id,
                service_id=self.state.service_id,
                team_id=self.state.team_id,
            )
        except Exception:
            return False
        data = getattr(response, "data", response) or {}
        if data.get("servicePaused"):
            return False
        deployment_status = ((data.get("status") or {}).get("deployment") or {}).get("status")
        return deployment_status == "COMPLETED"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class NorthflankSandboxClient(BaseSandboxClient[NorthflankSandboxClientOptions]):
    """Sandbox client backed by a Northflank service container.

    Pass a long-lived :class:`northflank.AsyncApiClient` at construction.
    The client manages service-resource lifecycle (create / attach /
    delete); the SDK client itself is not owned and must be closed by the
    caller.
    """

    backend_id = "northflank"
    supports_default_options = False

    def __init__(
        self,
        *,
        client: AsyncApiClient,
        instrumentation: Instrumentation | None = None,
    ) -> None:
        self._client = client
        self._instrumentation = instrumentation or Instrumentation()
        # ``BaseSandboxClient`` consults ``_dependencies`` via
        # ``_resolve_dependencies()``; default to None.
        self._dependencies = None

    # -- helpers ---------------------------------------------------------

    async def _create_ephemeral_service(
        self,
        options: NorthflankSandboxClientOptions,
        *,
        wait: bool = True,
    ) -> str:
        if not options.image_path:
            raise ValueError(
                "NorthflankSandboxClientOptions: either service_id or image_path must be set."
            )
        service_name = f"{options.service_name_prefix}{uuid.uuid4().hex[:12]}"
        deployment: dict[str, Any] = {
            "instances": options.instances,
            "external": {"imagePath": options.image_path},
        }
        if options.image_credentials:
            deployment["external"]["credentials"] = options.image_credentials

        docker_block = _build_docker_block(
            entrypoint=options.docker_entrypoint,
            command=options.docker_command,
        )
        if docker_block is not None:
            deployment["docker"] = docker_block

        payload = {
            "name": service_name,
            "billing": {"deploymentPlan": options.deployment_plan},
            "deployment": deployment,
        }
        response = await self._client.create.service.deployment(
            project_id=options.project_id,
            team_id=options.team_id,
            data=payload,
        )
        data = getattr(response, "data", response) or {}
        service_id = data.get("id") or data.get("name") or service_name
        if wait and options.wait_for_ready:
            await self._wait_for_service_or_cleanup(
                project_id=options.project_id,
                service_id=service_id,
                team_id=options.team_id,
                timeout_s=options.wait_timeout_s,
            )
        return service_id

    async def _wait_for_service_or_cleanup(
        self,
        *,
        project_id: str,
        service_id: str,
        team_id: str | None,
        timeout_s: float,
        extra_cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Wait for the deployment to become ready; on failure delete the service.

        The deployment was already accepted by the API, so the service
        exists on Northflank. Without a best-effort delete here a
        readiness failure (timeout, FAILED deployment, ctrl-C) leaves
        the service stranded with no SandboxSession to clean it up
        later. ``extra_cleanup`` runs *after* the service is gone so
        volume-mode can detach + delete the volume without fighting the
        live runtime.
        """
        try:
            await self._client.helpers.wait_for_service_ready(
                project_id=project_id,
                service_id=service_id,
                team_id=team_id,
                timeout_s=timeout_s,
                # Tight polling shaves wall-clock off ephemeral deployments
                # without tripping rate limits; the API tolerates much
                # higher request rates than this on the get.service path.
                poll_interval_s=0.1,
            )
        except BaseException:
            await self._best_effort_delete(
                project_id=project_id,
                service_id=service_id,
                team_id=team_id,
            )
            if extra_cleanup is not None:
                try:
                    await extra_cleanup()
                except Exception:
                    pass
            raise

    async def _create_workspace_volume(
        self,
        *,
        options: NorthflankSandboxClientOptions,
        service_id: str,
        root: str,
    ) -> str:
        """Provision a Northflank volume attached to ``service_id`` at ``root``.

        The volume is created with ``attachedObjects=[service]`` which both
        creates the volume and configures the service to mount it at
        ``root``. Returns the new volume id.
        """
        # nf-multi-rw is a ReadWriteMany class; pair it with the matching
        # access mode. 5120 MB is the smallest storageSize Northflank
        # accepts on that class — smaller values are rejected at create.
        spec = dict(
            options.volume_spec
            or {
                "storageSize": 5120,
                "accessMode": "ReadWriteMany",
                "storageClassName": "nf-multi-rw",
            }
        )
        volume_name = f"{options.service_name_prefix}vol-{uuid.uuid4().hex[:8]}"
        data: dict[str, Any] = {
            "name": volume_name,
            "mounts": [{"containerMountPath": root}],
            "spec": spec,
            "attachedObjects": [{"id": service_id, "type": "service"}],
        }
        response = await self._client.create.volume(
            project_id=options.project_id,
            team_id=options.team_id,
            data=data,
        )
        body = getattr(response, "data", response) or {}
        volume_id = body.get("id") or body.get("name") or volume_name
        return str(volume_id)

    async def _attach_workspace_volume(
        self,
        *,
        options: NorthflankSandboxClientOptions,
        service_id: str,
        volume_id: str,
    ) -> None:
        """Attach a pre-existing caller-owned volume to ``service_id``.

        The volume's mount path is fixed at create time and not
        overridable here, so the caller must have configured the volume
        to mount at ``manifest.root``. A 409 from the API is treated as
        "already attached" and tolerated so re-running create against
        the same service is idempotent.
        """
        try:
            await self._client.attach.volume(
                project_id=options.project_id,
                volume_id=volume_id,
                team_id=options.team_id,
                data={"nfObject": {"id": service_id, "type": "service"}},
            )
        except ApiCallError as exc:
            # 409 = already attached to this object. Anything else is a
            # real failure (unknown volume, auth, etc.) and must surface.
            if getattr(exc, "status", None) != 409:
                raise

    async def _best_effort_delete(
        self,
        *,
        project_id: str,
        service_id: str,
        team_id: str | None,
    ) -> None:
        try:
            await self._client.delete.service(
                project_id=project_id,
                service_id=service_id,
                team_id=team_id,
                delete_child_objects=True,
            )
        except Exception:
            # Cleanup-path: never let a delete failure mask the original
            # readiness error. The caller is already re-raising.
            pass

    async def _best_effort_delete_volume(
        self,
        *,
        project_id: str,
        volume_id: str,
        team_id: str | None,
        attached_service_id: str | None = None,
    ) -> None:
        # Northflank tracks the attachment as volume metadata, so even
        # after the service is gone the volume's ``attachedObjects`` may
        # still reference it and reject delete. Detach first if we know
        # the service id; tolerate every failure since this is a
        # best-effort cleanup path.
        if attached_service_id is not None:
            try:
                await self._client.detach.volume(
                    project_id=project_id,
                    volume_id=volume_id,
                    team_id=team_id,
                    data={"nfObject": {"id": attached_service_id, "type": "service"}},
                )
            except Exception:
                pass
        try:
            await self._client.delete.volume(
                project_id=project_id,
                volume_id=volume_id,
                team_id=team_id,
            )
        except Exception:
            pass

    async def _best_effort_detach_volume(
        self,
        *,
        project_id: str,
        volume_id: str,
        team_id: str | None,
        attached_service_id: str,
    ) -> None:
        """Detach ``volume_id`` from ``attached_service_id`` without deleting it.

        Used for caller-owned volumes on the cleanup path: we still
        unwire the volume from our (now-defunct) service so the caller
        can reattach or delete it themselves, but we never touch the
        volume itself.
        """
        try:
            await self._client.detach.volume(
                project_id=project_id,
                volume_id=volume_id,
                team_id=team_id,
                data={"nfObject": {"id": attached_service_id, "type": "service"}},
            )
        except Exception:
            pass

    # -- abstract methods ------------------------------------------------

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: NorthflankSandboxClientOptions,
    ) -> SandboxSession:
        if options.service_id and options.image_path:
            raise ValueError(
                "NorthflankSandboxClientOptions: pass either service_id or image_path, not both."
            )
        if options.workspace_persistence == "volume":
            if not options.image_path and not options.volume_id:
                raise ValueError(
                    "NorthflankSandboxClientOptions: workspace_persistence='volume' requires "
                    "either image_path (client provisions a fresh service and volume) or "
                    "volume_id (attach a caller-owned volume to either a client-created "
                    "or pre-existing service)."
                )
            if options.volume_id and options.volume_spec:
                raise ValueError(
                    "NorthflankSandboxClientOptions: volume_spec is ignored when volume_id "
                    "is set — remove one of the two."
                )
        elif options.volume_id:
            raise ValueError(
                "NorthflankSandboxClientOptions: volume_id requires workspace_persistence='volume'."
            )

        resolved_manifest = manifest or Manifest()

        volume_id: str | None = None
        owned_volume = False

        if options.service_id:
            service_id = options.service_id
            owned_by_client = False
        elif options.workspace_persistence == "volume":
            # Create the service without waiting so we can attach the
            # volume first; readiness is checked after the volume mount
            # is in place.
            service_id = await self._create_ephemeral_service(options, wait=False)
            owned_by_client = True
        else:
            service_id = await self._create_ephemeral_service(options)
            owned_by_client = True

        if options.workspace_persistence == "volume":
            try:
                if options.volume_id:
                    # Attach the caller's pre-existing volume. The volume
                    # is not provider-owned, so delete() will only detach
                    # it on cleanup.
                    await self._attach_workspace_volume(
                        options=options,
                        service_id=service_id,
                        volume_id=options.volume_id,
                    )
                    volume_id = options.volume_id
                    owned_volume = False
                else:
                    volume_id = await self._create_workspace_volume(
                        options=options,
                        service_id=service_id,
                        root=resolved_manifest.root,
                    )
                    owned_volume = True
            except BaseException:
                if owned_by_client:
                    await self._best_effort_delete(
                        project_id=options.project_id,
                        service_id=service_id,
                        team_id=options.team_id,
                    )
                raise

            if owned_by_client and options.wait_for_ready:
                resolved_volume_id = volume_id
                cleanup_service_id = service_id
                volume_is_owned = owned_volume

                async def _cleanup_volume() -> None:
                    if resolved_volume_id is None:
                        return
                    if volume_is_owned:
                        await self._best_effort_delete_volume(
                            project_id=options.project_id,
                            volume_id=resolved_volume_id,
                            team_id=options.team_id,
                            attached_service_id=cleanup_service_id,
                        )
                    else:
                        # Caller-owned volume: detach only, never delete.
                        await self._best_effort_detach_volume(
                            project_id=options.project_id,
                            volume_id=resolved_volume_id,
                            team_id=options.team_id,
                            attached_service_id=cleanup_service_id,
                        )

                await self._wait_for_service_or_cleanup(
                    project_id=options.project_id,
                    service_id=service_id,
                    team_id=options.team_id,
                    timeout_s=options.wait_timeout_s,
                    extra_cleanup=_cleanup_volume,
                )

        session_id = uuid.uuid4()
        snapshot_instance = resolve_snapshot(snapshot, str(session_id))
        state = NorthflankSandboxSessionState(
            session_id=session_id,
            manifest=resolved_manifest,
            snapshot=snapshot_instance,
            project_id=options.project_id,
            service_id=service_id,
            team_id=options.team_id,
            instance_name=options.instance_name,
            container_name=options.container_name,
            owned_by_client=owned_by_client,
            exec_timeout_s=options.exec_timeout_s,
            workspace_persistence=options.workspace_persistence,
            volume_id=volume_id,
            owned_volume=owned_volume,
        )
        inner = NorthflankSandboxSession.from_state(state, client=self._client)
        # Attach mode reuses a service that already exists, so its
        # workspace is whatever the user left there — mark it preserved
        # so the base session does not clear it on first start.
        if options.service_id:
            inner._set_start_state_preserved(True)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, NorthflankSandboxSessionState):
            raise TypeError(
                "NorthflankSandboxClient.resume expects a NorthflankSandboxSessionState"
            )
        inner = NorthflankSandboxSession.from_state(state, client=self._client)
        # Resume reattaches to a service that already exists, whose
        # workspace either lives on a Northflank volume (volume mode), is
        # restored from the tar embedded in state (tar mode), or simply
        # carries over in-container (attach mode). In every case the base
        # session must treat the workspace as preserved so it does not
        # ``ls`` + clear the root — that path chokes on volume-mount
        # setuid bits and would also drop the persisted content.
        inner._set_start_state_preserved(True)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = session._inner
        if not isinstance(inner, NorthflankSandboxSession):
            raise TypeError("NorthflankSandboxClient.delete expects a NorthflankSandboxSession")
        # Cleanup order: stop the runtime first, then unwire the volume.
        #
        # 1. Delete the service so nothing is still using the volume mount.
        # 2. Detach any volume from the (now-defunct) service. Northflank
        #    does not auto-detach volumes when their attached service is
        #    deleted — the volume's ``attachedObjects`` still references
        #    the dead service id, which would block a subsequent delete
        #    and leave the caller's volume wired to a ghost service.
        # 3. Delete the volume *only* if the client created it. A
        #    caller-supplied volume (owned_volume=False) is left in
        #    place: detach yes, delete never.
        if inner.state.owned_by_client:
            try:
                await self._client.delete.service(
                    project_id=inner.state.project_id,
                    service_id=inner.state.service_id,
                    team_id=inner.state.team_id,
                    delete_child_objects=True,
                )
            except ApiCallError as exc:
                # Treat "already gone" as success. Surface every other API
                # error (auth, server-side, etc.) so cleanup failures are
                # visible — otherwise leaked services accumulate silently.
                if getattr(exc, "status", None) != 404:
                    raise
        if inner.state.volume_id:
            try:
                await self._client.detach.volume(
                    project_id=inner.state.project_id,
                    volume_id=inner.state.volume_id,
                    team_id=inner.state.team_id,
                    data={"nfObject": {"id": inner.state.service_id, "type": "service"}},
                )
            except ApiCallError as exc:
                # 404 = volume already gone; 409 / 4xx with "not attached"
                # = the attachment was already gone. Either way we want
                # to fall through to the (optional) delete.
                if getattr(exc, "status", None) not in (404, 409):
                    raise
            if inner.state.owned_volume:
                try:
                    await self._client.delete.volume(
                        project_id=inner.state.project_id,
                        volume_id=inner.state.volume_id,
                        team_id=inner.state.team_id,
                    )
                except ApiCallError as exc:
                    if getattr(exc, "status", None) != 404:
                        raise
        return session

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return NorthflankSandboxSessionState.model_validate(payload)
