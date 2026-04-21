import abc
import hashlib
import io
import json
import shlex
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path, PurePath
from typing import Literal, TypeVar, cast

from typing_extensions import Self

from ...editor import ApplyPatchOperation
from ...run_config import (
    DEFAULT_MAX_LOCAL_DIR_FILE_CONCURRENCY,
    DEFAULT_MAX_MANIFEST_ENTRY_CONCURRENCY,
    SandboxConcurrencyLimits,
)
from ..apply_patch import PatchFormat, WorkspaceEditor
from ..entries import BaseEntry
from ..errors import (
    ExecNonZeroError,
    ExecTransportError,
    ExposedPortUnavailableError,
    InvalidCompressionSchemeError,
    InvalidManifestPathError,
    MountConfigError,
    PtySessionNotFoundError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
)
from ..files import EntryKind, FileEntry
from ..manifest import Manifest
from ..materialization import MaterializationResult, MaterializedFile
from ..snapshot import NoopSnapshot
from ..types import ExecResult, ExposedPortEndpoint, User
from ..util.parse_utils import parse_ls_la
from ..workspace_paths import (
    WorkspacePathPolicy,
    coerce_posix_path,
    posix_path_as_path,
    posix_path_for_error,
    sandbox_path_str,
)
from .archive_extraction import (
    WorkspaceArchiveExtractor,
    safe_zip_member_rel_path,
)
from .dependencies import Dependencies
from .manifest_application import ManifestApplier
from .pty_types import PtyExecUpdate
from .runtime_helpers import (
    RESOLVE_WORKSPACE_PATH_HELPER,
    WORKSPACE_FINGERPRINT_HELPER,
    RuntimeHelperScript,
)
from .sandbox_session_state import SandboxSessionState

_PtyEntryT = TypeVar("_PtyEntryT")
_RUNTIME_HELPER_CACHE_KEY_UNSET = object()
_SNAPSHOT_FINGERPRINT_VERSION = "workspace_tar_sha256_v1"
_WORKSPACE_ROOT_PROBE_TIMEOUT_S = 10.0
_WRITE_ACCESS_CHECK_SCRIPT = (
    'target="$1"\n'
    'if [ -e "$target" ]; then\n'
    '    [ -f "$target" ] && [ -w "$target" ]\n'
    "    exit $?\n"
    "fi\n"
    'parent=$(dirname "$target")\n'
    'while [ ! -e "$parent" ]; do\n'
    '    next=$(dirname "$parent")\n'
    '    if [ "$next" = "$parent" ]; then\n'
    "        exit 1\n"
    "    fi\n"
    '    parent="$next"\n'
    "done\n"
    '[ -d "$parent" ] && [ -w "$parent" ] && [ -x "$parent" ]\n'
)
_MKDIR_ACCESS_CHECK_SCRIPT = (
    'target="$1"\n'
    'parents="$2"\n'
    'if [ -e "$target" ] || [ -L "$target" ]; then\n'
    '    [ -d "$target" ] && [ -x "$target" ]\n'
    "    exit $?\n"
    "fi\n"
    'parent=$(dirname "$target")\n'
    'if [ "$parents" = "1" ]; then\n'
    '    while [ ! -e "$parent" ]; do\n'
    '        next=$(dirname "$parent")\n'
    '        if [ "$next" = "$parent" ]; then\n'
    "            exit 1\n"
    "        fi\n"
    '        parent="$next"\n'
    "    done\n"
    "fi\n"
    '[ -d "$parent" ] && [ -w "$parent" ] && [ -x "$parent" ]\n'
)
_RM_ACCESS_CHECK_SCRIPT = (
    'target="$1"\n'
    'recursive="$2"\n'
    'if [ ! -e "$target" ] && [ ! -L "$target" ]; then\n'
    '    [ "$recursive" = "1" ]\n'
    "    exit $?\n"
    "fi\n"
    'parent=$(dirname "$target")\n'
    '[ -d "$parent" ] && [ -w "$parent" ] && [ -x "$parent" ]\n'
)


class BaseSandboxSession(abc.ABC):
    state: SandboxSessionState
    _dependencies: Dependencies | None = None
    _dependencies_closed: bool = False
    _runtime_persist_workspace_skip_relpaths: set[Path] | None = None
    _pre_stop_hooks: list[Callable[[], Awaitable[None]]] | None = None
    _pre_stop_hooks_ran: bool = False
    _runtime_helpers_installed: set[PurePath] | None = None
    _runtime_helper_cache_key: object = _RUNTIME_HELPER_CACHE_KEY_UNSET
    _workspace_path_policy_cache: (
        tuple[str, tuple[tuple[str, bool], ...], WorkspacePathPolicy] | None
    ) = None
    # True when start() is reusing a backend whose workspace files may still be present.
    # This controls whether start() can avoid a full manifest apply for non-snapshot resumes.
    _start_workspace_state_preserved: bool = False
    # True when start() is reusing a backend whose OS users and groups may still be present.
    # This controls whether snapshot restore needs to reprovision manifest-managed accounts.
    _start_system_state_preserved: bool = False
    # Snapshot of serialized workspace readiness after backend startup/reconnect.
    # Providers may set this to True during start only after a preserved-backend probe succeeds.
    _start_workspace_root_ready: bool | None = None
    _max_manifest_entry_concurrency: int | None = DEFAULT_MAX_MANIFEST_ENTRY_CONCURRENCY
    _max_local_dir_file_concurrency: int | None = DEFAULT_MAX_LOCAL_DIR_FILE_CONCURRENCY

    async def start(self) -> None:
        try:
            await self._ensure_backend_started()
            self._start_workspace_root_ready = self.state.workspace_root_ready
            await self._probe_workspace_root_for_preserved_resume()
            await self._prepare_backend_workspace()
            await self._ensure_runtime_helpers()
            await self._start_workspace()
        except Exception as e:
            await self._after_start_failed()
            wrapped = self._wrap_start_error(e)
            if wrapped is e:
                raise
            raise wrapped from e
        await self._after_start()
        self.state.workspace_root_ready = True

    def _set_concurrency_limits(self, limits: SandboxConcurrencyLimits) -> None:
        limits.validate()
        self._max_manifest_entry_concurrency = limits.manifest_entries
        self._max_local_dir_file_concurrency = limits.local_dir_files

    async def _ensure_backend_started(self) -> None:
        """Start, reconnect, or recreate the backend before workspace setup runs."""

        return

    async def _prepare_backend_workspace(self) -> None:
        """Prepare provider-specific workspace prerequisites before manifest or snapshot work."""

        return

    async def _probe_workspace_root_for_preserved_resume(self) -> bool:
        """Probe whether a preserved backend already has a usable workspace root."""

        if not self._workspace_state_preserved_on_start() or self._start_workspace_root_ready:
            return self._can_reuse_preserved_workspace_on_resume()

        try:
            result = await self.exec(
                "test",
                "-d",
                self.state.manifest.root,
                timeout=_WORKSPACE_ROOT_PROBE_TIMEOUT_S,
                shell=False,
            )
        except Exception:
            return False

        if not result.ok():
            return False

        self._mark_workspace_root_ready_from_probe()
        return True

    def _mark_workspace_root_ready_from_probe(self) -> None:
        """Record that the preserved-backend workspace root was proven ready."""

        self.state.workspace_root_ready = True
        self._start_workspace_root_ready = True

    def _set_start_state_preserved(self, workspace: bool, *, system: bool | None = None) -> None:
        """Record whether this start begins with preserved backend state."""

        self._start_workspace_state_preserved = workspace
        self._start_system_state_preserved = workspace if system is None else system

    def _workspace_state_preserved_on_start(self) -> bool:
        """Return whether start begins with previously persisted workspace state."""

        return self._start_workspace_state_preserved

    def _system_state_preserved_on_start(self) -> bool:
        """Return whether start begins with previously provisioned OS/user state."""

        return self._start_system_state_preserved

    async def _start_workspace(self) -> None:
        """Restore snapshot or apply manifest state after backend startup is complete."""

        if await self.state.snapshot.restorable(dependencies=self.dependencies):
            can_reuse_workspace = await self._can_reuse_restorable_snapshot_workspace()
            if can_reuse_workspace:
                # The preserved workspace already matches the snapshot, so only rebuild ephemeral
                # manifest state that intentionally was not persisted.
                await self._reapply_ephemeral_manifest_on_resume()
            else:
                # Fresh workspaces and drifted preserved workspaces both need the durable snapshot
                # restored before ephemeral state is rebuilt.
                await self._restore_snapshot_into_workspace_on_resume()
                if self.should_provision_manifest_accounts_on_resume():
                    await self.provision_manifest_accounts()
                await self._reapply_ephemeral_manifest_on_resume()
        elif self._can_reuse_preserved_workspace_on_resume():
            # There is no durable snapshot to restore, but a reconnected backend may still need
            # ephemeral mounts/files refreshed without reapplying the full manifest.
            await self._reapply_ephemeral_manifest_on_resume()
        else:
            # A fresh backend without a restorable snapshot needs the full manifest materialized.
            await self._apply_manifest(
                provision_accounts=self.should_provision_manifest_accounts_on_resume()
            )

    async def _can_reuse_restorable_snapshot_workspace(self) -> bool:
        """Return whether a restorable snapshot can be skipped for this start."""

        if not self._can_reuse_preserved_workspace_on_resume():
            return False
        is_running = await self.running()
        return await self._can_skip_snapshot_restore_on_resume(is_running=is_running)

    def _can_reuse_preserved_workspace_on_resume(self) -> bool:
        """Return whether preserved workspace state is proven safe to reuse."""

        workspace_root_ready = self._start_workspace_root_ready
        if workspace_root_ready is None:
            workspace_root_ready = self.state.workspace_root_ready
        return self._workspace_state_preserved_on_start() and workspace_root_ready

    async def _after_start(self) -> None:
        """Run provider bookkeeping after workspace setup succeeds."""

        return

    async def _after_start_failed(self) -> None:
        """Run provider bookkeeping after workspace setup fails."""

        return

    def _wrap_start_error(self, error: Exception) -> Exception:
        """Return a provider-specific start error, or the original error."""

        return error

    async def stop(self) -> None:
        """
        Persist/snapshot the workspace.

        Note: `stop()` is intentionally persistence-only. Sandboxes that need to tear down
        sandbox resources (Docker containers, remote sessions, etc.) should implement
        `shutdown()` instead.
        """
        try:
            try:
                await self._before_stop()
                await self._persist_snapshot()
            except Exception as e:
                wrapped = self._wrap_stop_error(e)
                if wrapped is e:
                    raise
                raise wrapped from e
        finally:
            await self._after_stop()

    async def _before_stop(self) -> None:
        """Run transient process cleanup before snapshot persistence."""

        await self.pty_terminate_all()

    async def _persist_snapshot(self) -> None:
        """Persist/snapshot the workspace."""

        if isinstance(self.state.snapshot, NoopSnapshot):
            return

        fingerprint_record: dict[str, str] | None = None
        try:
            fingerprint_record = await self._compute_and_cache_snapshot_fingerprint()
        except Exception:
            fingerprint_record = None

        workspace_archive = await self.persist_workspace()
        try:
            await self.state.snapshot.persist(workspace_archive, dependencies=self.dependencies)
        except Exception:
            if fingerprint_record is not None:
                await self._delete_cached_snapshot_fingerprint_best_effort()
            raise
        finally:
            try:
                workspace_archive.close()
            except Exception:
                pass

        if fingerprint_record is None:
            self.state.snapshot_fingerprint = None
            self.state.snapshot_fingerprint_version = None
            return

        self.state.snapshot_fingerprint = fingerprint_record["fingerprint"]
        self.state.snapshot_fingerprint_version = fingerprint_record["version"]

    def _wrap_stop_error(self, error: Exception) -> Exception:
        """Return a provider-specific stop error, or the original error."""

        return error

    async def _after_stop(self) -> None:
        """Run provider bookkeeping after stop finishes or fails."""

        return

    def supports_docker_volume_mounts(self) -> bool:
        """Return whether this backend attaches Docker volume mounts before manifest apply."""

        return False

    def supports_pty(self) -> bool:
        return False

    async def shutdown(self) -> None:
        """
        Tear down sandbox resources (best-effort).

        Default is a no-op. Sandbox-specific sessions (e.g. Docker) should override.
        """
        await self._before_shutdown()
        await self._shutdown_backend()
        await self._after_shutdown()

    async def _before_shutdown(self) -> None:
        """Run transient process cleanup before backend shutdown."""

        await self.pty_terminate_all()

    async def _shutdown_backend(self) -> None:
        """Tear down provider-specific backend resources."""

        return

    async def _after_shutdown(self) -> None:
        """Run provider bookkeeping after backend shutdown."""

        return

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def aclose(self) -> None:
        """Run the session cleanup lifecycle outside of ``async with``.

        This performs the same session-owned cleanup as ``__aexit__()``: persist/snapshot the
        workspace via ``stop()``, tear down session resources via ``shutdown()``, and close
        session-scoped dependencies. If the session came from a sandbox client, call the client's
        ``delete()`` separately for backend-specific deletion such as removing a Docker container
        or deleting a temporary host workspace.
        """
        try:
            await self.run_pre_stop_hooks()
            await self.stop()
            await self.shutdown()
        finally:
            await self._aclose_dependencies()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        await self.aclose()

    @property
    def dependencies(self) -> Dependencies:
        dependencies = self._dependencies
        if dependencies is None:
            dependencies = Dependencies()
            self._dependencies = dependencies
            self._dependencies_closed = False
        return dependencies

    def set_dependencies(self, dependencies: Dependencies | None) -> None:
        if dependencies is None:
            return
        self._dependencies = dependencies
        self._dependencies_closed = False

    def register_pre_stop_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register an async hook to run once before the session workspace is persisted."""

        hooks = self._pre_stop_hooks
        if hooks is None:
            hooks = []
            self._pre_stop_hooks = hooks
        hooks.append(hook)
        self._pre_stop_hooks_ran = False

    async def run_pre_stop_hooks(self) -> None:
        """Run registered pre-stop hooks once before workspace persistence."""

        hooks = self._pre_stop_hooks
        if hooks is None or self._pre_stop_hooks_ran:
            return
        self._pre_stop_hooks_ran = True
        cleanup_error: BaseException | None = None
        for hook in hooks:
            try:
                await hook()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error

    async def _run_pre_stop_hooks(self) -> None:
        await self.run_pre_stop_hooks()

    async def _aclose_dependencies(self) -> None:
        dependencies = self._dependencies
        if dependencies is None or self._dependencies_closed:
            return
        self._dependencies_closed = True
        await dependencies.aclose()

    @staticmethod
    def _workspace_relpaths_overlap(lhs: Path, rhs: Path) -> bool:
        return lhs == rhs or lhs in rhs.parents or rhs in lhs.parents

    def _mount_relpaths_within_workspace(self) -> set[Path]:
        root = self._workspace_root_path()
        mount_relpaths: set[Path] = set()
        for _mount_entry, mount_path in self.state.manifest.mount_targets():
            try:
                mount_relpaths.add(mount_path.relative_to(root))
            except ValueError:
                continue
        return mount_relpaths

    def _overlapping_mount_relpaths(self, rel_path: Path) -> set[Path]:
        return {
            mount_relpath
            for mount_relpath in self._mount_relpaths_within_workspace()
            if self._workspace_relpaths_overlap(rel_path, mount_relpath)
        }

    def _native_snapshot_requires_tar_fallback(self) -> bool:
        for mount_entry, _mount_path in self.state.manifest.mount_targets():
            if not mount_entry.mount_strategy.supports_native_snapshot_detach(mount_entry):
                return True
        return False

    def register_persist_workspace_skip_path(self, path: Path | str) -> Path:
        """Exclude a runtime-created workspace path from future workspace snapshots.

        Use this for session side effects that are not part of durable workspace state, such as
        generated mount config or ephemeral sink output.
        """

        rel_path = Manifest._coerce_rel_path(path)
        Manifest._validate_rel_path(rel_path)
        if rel_path in (Path(""), Path(".")):
            raise ValueError("Persist workspace skip paths must target a concrete relative path.")
        overlapping_mounts = self._overlapping_mount_relpaths(rel_path)
        if overlapping_mounts:
            overlapping_mount = min(overlapping_mounts, key=lambda p: (len(p.parts), p.as_posix()))
            raise MountConfigError(
                message="persist workspace skip path must not overlap mount path",
                context={
                    "skip_path": rel_path.as_posix(),
                    "mount_path": overlapping_mount.as_posix(),
                },
            )

        if self._runtime_persist_workspace_skip_relpaths is None:
            self._runtime_persist_workspace_skip_relpaths = set()
        self._runtime_persist_workspace_skip_relpaths.add(rel_path)
        return rel_path

    def _persist_workspace_skip_relpaths(self) -> set[Path]:
        skip_paths = set(self.state.manifest.ephemeral_persistence_paths())
        if self._runtime_persist_workspace_skip_relpaths:
            skip_paths.update(self._runtime_persist_workspace_skip_relpaths)
        return skip_paths

    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
    ) -> ExecResult:
        """Execute a command inside the session.

        :param command: Command and args (will be stringified).
        :param timeout: Optional wall-clock timeout in seconds.
        :param shell: Whether to run this command in a shell. If ``True`` is provided,
            the command will be run prefixed by ``sh -lc``. A custom shell prefix may be used
            by providing a list.

        :returns: An ``ExecResult`` containing stdout/stderr and exit code.

        :raises TimeoutError: If the sandbox cannot complete within `timeout`.
        """

        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=user)
        return await self._exec_internal(*sanitized_command, timeout=timeout)

    async def resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        self._assert_exposed_port_configured(port)
        return await self._resolve_exposed_port(port)

    def _assert_exposed_port_configured(self, port: int) -> None:
        if port not in self.state.exposed_ports:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="not_configured",
            )

    def _prepare_exec_command(
        self,
        *command: str | Path,
        shell: bool | list[str],
        user: str | User | None,
    ) -> list[str]:
        sanitized_command = [str(c) for c in command]

        if shell:
            joined = (
                sanitized_command[0]
                if len(sanitized_command) == 1
                else shlex.join(sanitized_command)
            )
            if isinstance(shell, list):
                sanitized_command = shell + [joined]
            else:
                sanitized_command = ["sh", "-lc", joined]

        if user:
            if isinstance(user, User):
                user = user.name

            assert isinstance(user, str)

            sanitized_command = ["sudo", "-u", user, "--"] + sanitized_command

        return sanitized_command

    def _resolve_pty_session_entry(
        self, *, pty_processes: Mapping[int, _PtyEntryT], session_id: int
    ) -> _PtyEntryT:
        entry = pty_processes.get(session_id)
        if entry is None:
            raise PtySessionNotFoundError(session_id=session_id)
        return entry

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        _ = (command, timeout, shell, user, tty, yield_time_s, max_output_tokens)
        raise NotImplementedError("PTY execution is not supported by this sandbox session")

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        _ = (session_id, chars, yield_time_s, max_output_tokens)
        raise NotImplementedError("PTY execution is not supported by this sandbox session")

    async def pty_terminate_all(self) -> None:
        return

    @abc.abstractmethod
    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult: ...

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        raise ExposedPortUnavailableError(
            port=port,
            exposed_ports=self.state.exposed_ports,
            reason="backend_unavailable",
            context={"backend": type(self).__name__},
        )

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return ()

    def _current_runtime_helper_cache_key(self) -> object | None:
        return None

    def _sync_runtime_helper_install_cache(self) -> None:
        current_key = self._current_runtime_helper_cache_key()
        cached_key = self._runtime_helper_cache_key
        if cached_key is _RUNTIME_HELPER_CACHE_KEY_UNSET:
            self._runtime_helper_cache_key = current_key
            return
        if cached_key != current_key:
            self._runtime_helpers_installed = None
            self._runtime_helper_cache_key = current_key

    async def _ensure_runtime_helper_installed(self, helper: RuntimeHelperScript) -> PurePath:
        self._sync_runtime_helper_install_cache()
        installed = self._runtime_helpers_installed
        if installed is None:
            installed = set()
            self._runtime_helpers_installed = installed

        install_path = helper.install_path
        if install_path in installed:
            probe = await self.exec(*helper.present_command(), shell=False)
            if probe.ok():
                return install_path
            self._sync_runtime_helper_install_cache()
            installed = self._runtime_helpers_installed
            if installed is None:
                installed = set()
                self._runtime_helpers_installed = installed
            installed.discard(install_path)

        result = await self.exec(*helper.install_command(), shell=False)
        if not result.ok():
            raise ExecNonZeroError(
                result,
                command=("install_runtime_helper", str(install_path)),
            )

        self._sync_runtime_helper_install_cache()
        installed = self._runtime_helpers_installed
        if installed is None:
            installed = set()
            self._runtime_helpers_installed = installed
        installed.add(install_path)
        return install_path

    async def _ensure_runtime_helpers(self) -> None:
        for helper in self._runtime_helpers():
            await self._ensure_runtime_helper_installed(helper)

    def _workspace_path_policy(self) -> WorkspacePathPolicy:
        root = self.state.manifest.root
        grants_key = tuple(
            (grant.path, grant.read_only) for grant in self.state.manifest.extra_path_grants
        )
        cached = self._workspace_path_policy_cache
        if cached is not None and cached[0] == root and cached[1] == grants_key:
            return cached[2]

        policy = WorkspacePathPolicy(
            root=root,
            extra_path_grants=self.state.manifest.extra_path_grants,
        )
        self._workspace_path_policy_cache = (root, grants_key, policy)
        return policy

    def _workspace_root_path(self) -> Path:
        return posix_path_as_path(self._workspace_path_policy().sandbox_root())

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return self.normalize_path(path, for_write=for_write)

    async def _validate_remote_path_access(
        self,
        path: Path | str,
        *,
        for_write: bool = False,
    ) -> Path:
        """Validate an SDK file path against the remote sandbox filesystem before IO.

        The returned path is the normalized workspace path, not the resolved realpath. This keeps
        safe leaf symlink operations working normally, such as removing a symlink instead of its
        target, while still rejecting paths whose resolved remote target escapes all allowed roots.
        """

        path_policy = self._workspace_path_policy()
        root = path_policy.sandbox_root()
        workspace_path = path_policy.normalize_sandbox_path(path, for_write=for_write)
        original_path = coerce_posix_path(path)
        helper_path = await self._ensure_runtime_helper_installed(RESOLVE_WORKSPACE_PATH_HELPER)
        extra_grant_args = tuple(
            arg
            for root, read_only in path_policy.extra_path_grant_rules()
            for arg in (root.as_posix(), "1" if read_only else "0")
        )
        command = (
            str(helper_path),
            root.as_posix(),
            workspace_path.as_posix(),
            "1" if for_write else "0",
            *extra_grant_args,
        )
        result = await self.exec(*command, shell=False)
        if result.ok():
            resolved = result.stdout.decode("utf-8", errors="replace").strip()
            if resolved:
                # Preserve the requested workspace path so leaf symlinks keep their normal
                # semantics while the remote realpath check still enforces path confinement.
                return posix_path_as_path(workspace_path)
            raise ExecTransportError(
                command=(
                    "resolve_workspace_path",
                    root.as_posix(),
                    workspace_path.as_posix(),
                    "1" if for_write else "0",
                    *extra_grant_args,
                ),
                context={
                    "reason": "empty_stdout",
                    "exit_code": result.exit_code,
                    "stdout": "",
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )

        reason: Literal["absolute", "escape_root"] = (
            "absolute" if original_path.is_absolute() else "escape_root"
        )
        if result.exit_code == 111:
            raise InvalidManifestPathError(
                rel=original_path.as_posix(),
                reason=reason,
                context={
                    "resolved_path": result.stderr.decode("utf-8", errors="replace").strip(),
                },
            )
        if result.exit_code == 113:
            raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
        if result.exit_code == 114:
            stderr = result.stderr.decode("utf-8", errors="replace")
            context: dict[str, object] = {"reason": "read_only_extra_path_grant"}
            for line in stderr.splitlines():
                if line.startswith("read-only extra path grant: "):
                    context["grant_path"] = line.removeprefix("read-only extra path grant: ")
                elif line.startswith("resolved path: "):
                    context["resolved_path"] = line.removeprefix("resolved path: ")
            raise WorkspaceArchiveWriteError(
                path=posix_path_for_error(workspace_path), context=context
            )
        raise ExecNonZeroError(
            result,
            command=(
                "resolve_workspace_path",
                root.as_posix(),
                workspace_path.as_posix(),
                "1" if for_write else "0",
                *extra_grant_args,
            ),
        )

    @abc.abstractmethod
    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        """Read a file from the session's workspace.

        :param path: Absolute path in the container or path relative to the
                workspace root.
        :param user: Optional sandbox user to perform the read as.
        :returns: A readable file-like object.
        :raises: FileNotFoundError: If the path does not exist.
        """

    @abc.abstractmethod
    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        """Write a file into the session's workspace.

        :param path: Absolute path in the container or path relative to the
                workspace root.
        :param data: A file-like object positioned at the start of the payload.
        :param user: Optional sandbox user to perform the write as.
        """

    async def _check_read_with_exec(
        self, path: Path | str, *, user: str | User | None = None
    ) -> Path:
        workspace_path = await self._validate_path_access(path)
        path_arg = sandbox_path_str(workspace_path)
        cmd = ("sh", "-lc", '[ -r "$1" ]', "sh", path_arg)
        result = await self.exec(*cmd, shell=False, user=user)
        if not result.ok():
            raise WorkspaceReadNotFoundError(
                path=posix_path_as_path(coerce_posix_path(path)),
                context={
                    "command": ["sh", "-lc", "<read_access_check>", path_arg],
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )
        return workspace_path

    async def _check_write_with_exec(
        self, path: Path | str, *, user: str | User | None = None
    ) -> Path:
        workspace_path = await self._validate_path_access(path, for_write=True)
        path_arg = sandbox_path_str(workspace_path)
        cmd = ("sh", "-lc", _WRITE_ACCESS_CHECK_SCRIPT, "sh", path_arg)
        result = await self.exec(*cmd, shell=False, user=user)
        if not result.ok():
            raise WorkspaceArchiveWriteError(
                path=workspace_path,
                context={
                    "command": ["sh", "-lc", "<write_access_check>", path_arg],
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )
        return workspace_path

    async def _check_mkdir_with_exec(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> Path:
        workspace_path = await self._validate_path_access(path, for_write=True)
        parents_flag = "1" if parents else "0"
        path_arg = sandbox_path_str(workspace_path)
        cmd = ("sh", "-lc", _MKDIR_ACCESS_CHECK_SCRIPT, "sh", path_arg, parents_flag)
        result = await self.exec(*cmd, shell=False, user=user)
        if not result.ok():
            raise WorkspaceArchiveWriteError(
                path=workspace_path,
                context={
                    "command": [
                        "sh",
                        "-lc",
                        "<mkdir_access_check>",
                        path_arg,
                        parents_flag,
                    ],
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )
        return workspace_path

    async def _check_rm_with_exec(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> Path:
        workspace_path = await self._validate_path_access(path, for_write=True)
        recursive_flag = "1" if recursive else "0"
        path_arg = sandbox_path_str(workspace_path)
        cmd = ("sh", "-lc", _RM_ACCESS_CHECK_SCRIPT, "sh", path_arg, recursive_flag)
        result = await self.exec(*cmd, shell=False, user=user)
        if not result.ok():
            raise WorkspaceArchiveWriteError(
                path=workspace_path,
                context={
                    "command": [
                        "sh",
                        "-lc",
                        "<rm_access_check>",
                        path_arg,
                        recursive_flag,
                    ],
                    "stdout": result.stdout.decode("utf-8", errors="replace"),
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )
        return workspace_path

    @abc.abstractmethod
    async def running(self) -> bool:
        """
        :returns: whether the underlying sandbox is currently running.
        """

    @abc.abstractmethod
    async def persist_workspace(self) -> io.IOBase:
        """Serialize the session's workspace into a byte stream.

        :returns: A readable byte stream representing the workspace contents.
            Portable tar streams must use workspace-relative member paths rather than
            embedding the source backend's workspace root directory.
        """

    @abc.abstractmethod
    async def hydrate_workspace(self, data: io.IOBase) -> None:
        """Populate the session's workspace from a serialized byte stream.

        :param data: A readable byte stream as produced by `persist_workspace`.
            Portable tar streams are extracted underneath this session's workspace root.
        """

    async def ls(
        self,
        path: Path | str,
        *,
        user: str | User | None = None,
    ) -> list[FileEntry]:
        """List directory contents.

        :param path: Path to list.
        :param user: Optional sandbox user to list as.
        :returns: A list of `FileEntry` objects.
        """
        path = await self._validate_path_access(path)

        path_arg = sandbox_path_str(path)
        cmd = ("ls", "-la", "--", path_arg)
        result = await self.exec(*cmd, shell=False, user=user)
        if not result.ok():
            raise ExecNonZeroError(result, command=cmd)

        return parse_ls_la(result.stdout.decode("utf-8", errors="replace"), base=path_arg)

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        """Remove a file or directory.

        :param path: Path to remove.
        :param recursive: If true, remove directories recursively.
        :param user: Optional sandbox user to remove as.
        """
        path = await self._validate_path_access(path, for_write=True)

        cmd: list[str] = ["rm"]
        if recursive:
            cmd.append("-rf")
        cmd.extend(["--", sandbox_path_str(path)])

        result = await self.exec(*cmd, shell=False, user=user)
        if not result.ok():
            raise ExecNonZeroError(result, command=cmd)

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        """Create a directory.

        :param path: Directory to create on the remote.
        :param parents: If true, create missing parents.
        :param user: Optional sandbox user to create the directory as.
        """
        path = await self._validate_path_access(path, for_write=True)

        cmd: list[str] = ["mkdir"]
        if parents:
            cmd.append("-p")
        cmd.append(sandbox_path_str(path))

        result = await self.exec(*cmd, shell=False, user=user)
        if not result.ok():
            raise ExecNonZeroError(result, command=cmd)

    async def extract(
        self,
        path: Path | str,
        data: io.IOBase,
        *,
        compression_scheme: Literal["tar", "zip"] | None = None,
    ) -> None:
        """
        Write a compressed archive to a destination on the remote.
        Optionally extract the archive once written.

        :param path: Path on the host machine to extract to
        :param data: a file-like io stream.
        :param compression_scheme: either "tar" or "zip". If not provided,
            it will try to infer from the path.
        """
        if isinstance(path, str):
            path = Path(path)

        if compression_scheme is None:
            suffix = path.suffix.removeprefix(".")
            compression_scheme = cast(Literal["tar", "zip"], suffix) if suffix else None

        if compression_scheme is None or compression_scheme not in ["zip", "tar"]:
            raise InvalidCompressionSchemeError(path=path, scheme=compression_scheme)

        normalized_path = await self._validate_path_access(path, for_write=True)
        destination_root = normalized_path.parent

        # Materialize the archive into a local spool once because both `write()` and the
        # extraction step consume the stream, and zip extraction may require seeking.
        spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
        try:
            shutil.copyfileobj(data, spool)
            spool.seek(0)
            await self.write(normalized_path, spool)
            spool.seek(0)

            if compression_scheme == "tar":
                await self._extract_tar_archive(
                    archive_path=normalized_path,
                    destination_root=destination_root,
                    data=spool,
                )
            else:
                await self._extract_zip_archive(
                    archive_path=normalized_path,
                    destination_root=destination_root,
                    data=spool,
                )
        finally:
            spool.close()

    async def apply_patch(
        self,
        operations: ApplyPatchOperation
        | dict[str, object]
        | list[ApplyPatchOperation | dict[str, object]],
        *,
        patch_format: PatchFormat | Literal["v4a"] = "v4a",
    ) -> str:
        return await WorkspaceEditor(self).apply_patch(operations, patch_format=patch_format)

    def normalize_path(self, path: Path | str, *, for_write: bool = False) -> Path:
        policy = self._workspace_path_policy()
        return policy.normalize_path(path, for_write=for_write)

    def describe(self) -> str:
        return self.state.manifest.describe()

    async def _extract_tar_archive(
        self,
        *,
        archive_path: Path,
        destination_root: Path,
        data: io.IOBase,
    ) -> None:
        extractor = WorkspaceArchiveExtractor(
            mkdir=lambda path: self.mkdir(path, parents=True),
            write=self.write,
            ls=lambda path: self.ls(path),
        )
        await extractor.extract_tar_archive(
            archive_path=archive_path,
            destination_root=destination_root,
            data=data,
        )

    async def _extract_zip_archive(
        self,
        *,
        archive_path: Path,
        destination_root: Path,
        data: io.IOBase,
    ) -> None:
        extractor = WorkspaceArchiveExtractor(
            mkdir=lambda path: self.mkdir(path, parents=True),
            write=self.write,
            ls=lambda path: self.ls(path),
        )
        await extractor.extract_zip_archive(
            archive_path=archive_path,
            destination_root=destination_root,
            data=data,
        )

    @staticmethod
    def _safe_zip_member_rel_path(member) -> Path | None:
        return safe_zip_member_rel_path(member)

    async def _apply_manifest(
        self,
        *,
        only_ephemeral: bool = False,
        provision_accounts: bool = True,
    ) -> MaterializationResult:
        applier = ManifestApplier(
            mkdir=lambda path: self.mkdir(path, parents=True),
            exec_checked_nonzero=self._exec_checked_nonzero,
            apply_entry=lambda artifact, dest, base_dir: artifact.apply(self, dest, base_dir),
            max_entry_concurrency=self._max_manifest_entry_concurrency,
        )
        return await applier.apply_manifest(
            self.state.manifest,
            only_ephemeral=only_ephemeral,
            provision_accounts=provision_accounts,
            base_dir=self._manifest_base_dir(),
        )

    async def apply_manifest(self, *, only_ephemeral: bool = False) -> MaterializationResult:
        return await self._apply_manifest(
            only_ephemeral=only_ephemeral,
            provision_accounts=not only_ephemeral,
        )

    async def provision_manifest_accounts(self) -> None:
        applier = ManifestApplier(
            mkdir=lambda path: self.mkdir(path, parents=True),
            exec_checked_nonzero=self._exec_checked_nonzero,
            apply_entry=lambda artifact, dest, base_dir: artifact.apply(self, dest, base_dir),
        )
        await applier.provision_accounts(self.state.manifest)

    def should_provision_manifest_accounts_on_resume(self) -> bool:
        """Return whether resume should reprovision manifest-managed users and groups."""

        return not self._system_state_preserved_on_start()

    async def _reapply_ephemeral_manifest_on_resume(self) -> None:
        """Rebuild ephemeral manifest state without touching persisted workspace files."""

        await self.apply_manifest(only_ephemeral=True)

    async def _restore_snapshot_into_workspace_on_resume(self) -> None:
        """Clear the live workspace contents and repopulate them from the persisted snapshot."""

        await self._clear_workspace_root_on_resume()
        workspace_archive = await self.state.snapshot.restore(dependencies=self.dependencies)
        try:
            await self.hydrate_workspace(workspace_archive)
        finally:
            try:
                workspace_archive.close()
            except Exception:
                pass

    async def _live_workspace_matches_snapshot_on_resume(self) -> bool:
        """Return whether the running sandbox workspace definitely matches the stored snapshot."""

        stored_fingerprint = self.state.snapshot_fingerprint
        stored_version = self.state.snapshot_fingerprint_version
        if not stored_fingerprint or not stored_version:
            return False

        try:
            cached_record = await self._compute_and_cache_snapshot_fingerprint()
        except Exception:
            return False

        return (
            cached_record.get("fingerprint") == stored_fingerprint
            and cached_record.get("version") == stored_version
        )

    async def _can_skip_snapshot_restore_on_resume(self, *, is_running: bool) -> bool:
        """Return whether resume can safely reuse the running workspace without restore."""

        if not is_running:
            return False
        return await self._live_workspace_matches_snapshot_on_resume()

    def _snapshot_fingerprint_cache_path(self) -> Path:
        """Return the runtime-owned path for this session's cached snapshot fingerprint."""

        cache_path = coerce_posix_path(
            f"/tmp/openai-agents/session-state/{self.state.session_id.hex}/fingerprint.json"
        )
        if self._workspace_path_policy().root_is_existing_host_path():
            return Path(cache_path.as_posix())
        return posix_path_as_path(cache_path)

    def _workspace_fingerprint_skip_relpaths(self) -> set[Path]:
        """Return workspace paths that should be omitted from snapshot fingerprinting."""

        skip_paths = self._persist_workspace_skip_relpaths()
        skip_paths.update(self._workspace_resume_mount_skip_relpaths())
        return skip_paths

    async def _compute_and_cache_snapshot_fingerprint(self) -> dict[str, str]:
        """Compute the current workspace fingerprint in-container and atomically cache it."""

        helper_path = await self._ensure_runtime_helper_installed(WORKSPACE_FINGERPRINT_HELPER)
        command = [
            str(helper_path),
            self._workspace_root_path().as_posix(),
            self._snapshot_fingerprint_version(),
            self._snapshot_fingerprint_cache_path().as_posix(),
            self._resume_manifest_digest(),
        ]
        command.extend(
            rel_path.as_posix()
            for rel_path in sorted(
                self._workspace_fingerprint_skip_relpaths(),
                key=lambda path: path.as_posix(),
            )
        )
        result = await self.exec(*command, shell=False)
        if not result.ok():
            raise ExecNonZeroError(result, command=("compute_workspace_fingerprint", *command[1:]))
        return self._parse_snapshot_fingerprint_record(result.stdout)

    async def _read_cached_snapshot_fingerprint(self) -> dict[str, str]:
        """Read the cached snapshot fingerprint record from the running sandbox."""

        result = await self.exec(
            "cat",
            "--",
            self._snapshot_fingerprint_cache_path().as_posix(),
            shell=False,
        )
        if not result.ok():
            raise ExecNonZeroError(
                result,
                command=("cat", self._snapshot_fingerprint_cache_path().as_posix()),
            )
        return self._parse_snapshot_fingerprint_record(result.stdout)

    def _parse_snapshot_fingerprint_record(
        self, payload: bytes | bytearray | str
    ) -> dict[str, str]:
        """Validate and normalize a cached snapshot fingerprint JSON payload."""

        raw = payload.decode("utf-8") if isinstance(payload, bytes | bytearray) else payload
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("snapshot fingerprint payload must be a JSON object")
        fingerprint = data.get("fingerprint")
        version = data.get("version")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("snapshot fingerprint payload is missing `fingerprint`")
        if not isinstance(version, str) or not version:
            raise ValueError("snapshot fingerprint payload is missing `version`")
        return {"fingerprint": fingerprint, "version": version}

    async def _delete_cached_snapshot_fingerprint_best_effort(self) -> None:
        """Remove the cached snapshot fingerprint file without raising on cleanup failure."""

        try:
            await self.exec(
                "rm",
                "-f",
                "--",
                self._snapshot_fingerprint_cache_path().as_posix(),
                shell=False,
            )
        except Exception:
            return

    def _snapshot_fingerprint_version(self) -> str:
        """Return the version tag for the current snapshot fingerprint algorithm."""

        return _SNAPSHOT_FINGERPRINT_VERSION

    def _resume_manifest_digest(self) -> str:
        """Return a stable digest of the manifest state that affects resume correctness."""

        manifest_payload = json.dumps(
            self.state.manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(manifest_payload).hexdigest()

    async def _apply_entry_batch(
        self,
        entries: Sequence[tuple[Path, BaseEntry]],
        *,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        applier = ManifestApplier(
            mkdir=lambda path: self.mkdir(path, parents=True),
            exec_checked_nonzero=self._exec_checked_nonzero,
            apply_entry=lambda artifact, dest, current_base_dir: artifact.apply(
                self,
                dest,
                current_base_dir,
            ),
            max_entry_concurrency=self._max_manifest_entry_concurrency,
        )
        return await applier._apply_entry_batch(entries, base_dir=base_dir)

    def _manifest_base_dir(self) -> Path:
        return Path.cwd()

    async def _exec_checked_nonzero(self, *command: str | Path) -> ExecResult:
        result = await self.exec(*command, shell=False)
        if not result.ok():
            raise ExecNonZeroError(result, command=command)
        return result

    async def _clear_workspace_root_on_resume(self) -> None:
        """
        Best-effort cleanup step for snapshot resume.

        We intentionally clear *contents* of the workspace root rather than deleting the root
        directory itself. Some sandboxes configure their process working directory to the workspace
        root (e.g. Modal sandboxes), and deleting the directory can make subsequent exec() calls
        fail with "failed to find initial working directory".
        """

        skip_rel_paths = self._workspace_resume_mount_skip_relpaths()
        if any(rel_path in (Path(""), Path(".")) for rel_path in skip_rel_paths):
            return

        await self._clear_workspace_dir_on_resume_pruned(
            current_dir=self._workspace_root_path(),
            skip_rel_paths=skip_rel_paths,
        )

    def _workspace_resume_mount_skip_relpaths(self) -> set[Path]:
        root = self._workspace_root_path()
        skip_rel_paths: set[Path] = set()
        for _mount, mount_path in self.state.manifest.ephemeral_mount_targets():
            try:
                skip_rel_paths.add(mount_path.relative_to(root))
            except ValueError:
                continue
        return skip_rel_paths

    async def _clear_workspace_dir_on_resume_pruned(
        self,
        *,
        current_dir: Path,
        skip_rel_paths: set[Path],
    ) -> None:
        root = self._workspace_root_path()
        try:
            entries = await self.ls(current_dir)
        except ExecNonZeroError:
            # If the root or subtree doesn't exist (or isn't listable), treat it as empty and let
            # hydrate/apply create it as needed.
            return

        for entry in entries:
            child = Path(entry.path)
            try:
                child_rel = child.relative_to(root)
            except ValueError:
                await self.rm(child, recursive=True)
                continue

            if child_rel in skip_rel_paths:
                continue
            if any(child_rel in skip_rel_path.parents for skip_rel_path in skip_rel_paths):
                if entry.kind == EntryKind.DIRECTORY:
                    await self._clear_workspace_dir_on_resume_pruned(
                        current_dir=child,
                        skip_rel_paths=skip_rel_paths,
                    )
                else:
                    await self.rm(child, recursive=True)
                continue
            # `parse_ls_la` filters "." and ".." already; remove everything else recursively.
            await self.rm(child, recursive=True)
