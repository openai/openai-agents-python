import sys

if sys.platform == "win32":  # pragma: no cover
    raise ImportError(
        "UnixLocalSandbox is not supported on Windows. "
        "Use DockerSandboxClient or another sandbox backend."
    )

import asyncio
import errno
import fcntl
import io
import logging
import os
import shlex
import shutil
import signal
import tarfile
import tempfile
import termios
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from ..errors import (
    ExecNonZeroError,
    ExecTimeoutError,
    ExecTransportError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceRootNotFoundError,
    WorkspaceStartError,
    WorkspaceStopError,
)
from ..files import EntryKind, FileEntry
from ..manifest import Manifest
from ..materialization import MaterializationResult
from ..session import SandboxSession, SandboxSessionState
from ..session.base_sandbox_session import BaseSandboxSession
from ..session.dependencies import Dependencies
from ..session.manager import Instrumentation
from ..session.pty_types import (
    PTY_PROCESSES_MAX,
    PTY_PROCESSES_WARNING,
    PtyExecUpdate,
    allocate_pty_process_id,
    clamp_pty_yield_time_ms,
    process_id_to_prune_from_meta,
    resolve_pty_write_yield_time_ms,
    truncate_text_by_tokens,
)
from ..session.sandbox_client import BaseSandboxClient, BaseSandboxClientOptions
from ..session.workspace_payloads import coerce_write_payload
from ..snapshot import SnapshotBase, SnapshotSpec, resolve_snapshot
from ..types import ExecResult, ExposedPortEndpoint, Permissions, User
from ..util.tar_utils import (
    UnsafeTarMemberError,
    safe_extract_tarfile,
    should_skip_tar_member,
)
from ..workspace_paths import _raise_if_filesystem_root

_DEFAULT_WORKSPACE_PREFIX = "sandbox-local-"
_DEFAULT_MANIFEST_ROOT = cast(str, Manifest.model_fields["root"].default)
_PTY_READ_CHUNK_BYTES = 16_384
_PTY_CHILD_SIGNAL_DEFAULTS = (signal.SIGINT, signal.SIGQUIT)

logger = logging.getLogger(__name__)


def _close_fd_quietly(fd: int) -> None:
    with suppress(OSError):
        os.close(fd)


def _restore_pty_child_signal_defaults() -> None:
    for signum in _PTY_CHILD_SIGNAL_DEFAULTS:
        signal.signal(signum, signal.SIG_DFL)


class UnixLocalSandboxSessionState(SandboxSessionState):
    type: Literal["unix_local"] = "unix_local"
    workspace_root_owned: bool = False


class UnixLocalSandboxClientOptions(BaseSandboxClientOptions):
    type: Literal["unix_local"] = "unix_local"
    exposed_ports: tuple[int, ...] = ()

    def __init__(
        self,
        exposed_ports: tuple[int, ...] = (),
        *,
        type: Literal["unix_local"] = "unix_local",
    ) -> None:
        super().__init__(
            type=type,
            exposed_ports=exposed_ports,
        )


@dataclass
class _UnixPtyProcessEntry:
    process: asyncio.subprocess.Process
    tty: bool
    primary_fd: int | None = None
    last_used: float = field(default_factory=time.monotonic)
    output_chunks: deque[bytes] = field(default_factory=deque)
    output_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output_notify: asyncio.Event = field(default_factory=asyncio.Event)
    output_closed: asyncio.Event = field(default_factory=asyncio.Event)
    pump_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    wait_task: asyncio.Task[None] | None = None


class UnixLocalSandboxSession(BaseSandboxSession):
    """
    Unix-only session implementation that runs commands on the host and uses the host filesystem
    as the workspace (rooted at `self.state.manifest.root`).
    """

    state: UnixLocalSandboxSessionState
    _running: bool
    _pty_lock: asyncio.Lock
    _pty_processes: dict[int, _UnixPtyProcessEntry]
    _reserved_pty_process_ids: set[int]

    def __init__(self, *, state: UnixLocalSandboxSessionState) -> None:
        self.state = state
        self._running = False
        self._pty_lock = asyncio.Lock()
        self._pty_processes = {}
        self._reserved_pty_process_ids = set()

    @classmethod
    def from_state(cls, state: UnixLocalSandboxSessionState) -> "UnixLocalSandboxSession":
        return cls(state=state)

    async def _prepare_backend_workspace(self) -> None:
        workspace = Path(self.state.manifest.root)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise WorkspaceStartError(path=workspace, cause=e) from e

    async def _after_start(self) -> None:
        # Mark the session live only after restore/apply completes. A resumed UnixLocal session may
        # recreate an empty workspace after cleanup deleted the previous root, so reporting
        # "running" too early can incorrectly skip snapshot restoration based on a stale
        # fingerprint cache file.
        self._running = True

    async def _after_start_failed(self) -> None:
        self._running = False

    def _wrap_stop_error(self, error: Exception) -> Exception:
        return WorkspaceStopError(path=Path(self.state.manifest.root), cause=error)

    async def _apply_manifest(
        self,
        *,
        only_ephemeral: bool = False,
        provision_accounts: bool = True,
    ) -> MaterializationResult:
        if self.state.manifest.users or self.state.manifest.groups:
            raise ValueError(
                "UnixLocalSandboxSession does not support manifest users or groups because "
                "provisioning would run on the host machine"
            )
        return await super()._apply_manifest(
            only_ephemeral=only_ephemeral,
            provision_accounts=provision_accounts,
        )

    async def apply_manifest(self, *, only_ephemeral: bool = False) -> MaterializationResult:
        return await self._apply_manifest(
            only_ephemeral=only_ephemeral,
            provision_accounts=not only_ephemeral,
        )

    async def provision_manifest_accounts(self) -> None:
        if self.state.manifest.users or self.state.manifest.groups:
            raise ValueError(
                "UnixLocalSandboxSession does not support manifest users or groups because "
                "provisioning would run on the host machine"
            )

    async def _after_shutdown(self) -> None:
        # Best-effort: mark session not running. We intentionally do not delete the workspace
        # directory here; cleanup is handled by the Client.delete().
        self._running = False

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        return ExposedPortEndpoint(host="127.0.0.1", port=port, tls=False)

    def supports_pty(self) -> bool:
        return True

    def _prepare_exec_command(
        self,
        *command: str | Path,
        shell: bool | list[str],
        user: str | User | None,
    ) -> list[str]:
        if shell is True:
            shell = ["sh", "-c"]
        return super()._prepare_exec_command(*command, shell=shell, user=user)

    async def _exec_internal(
        self, *command: str | Path, timeout: float | None = None
    ) -> ExecResult:
        env, cwd = await self._resolved_exec_context()
        workspace_root = Path(cwd).resolve()
        command_parts = self._workspace_relative_command_parts(command, workspace_root)
        process_cwd, command_parts = self._shell_workspace_process_context(
            command_parts=command_parts,
            workspace_root=workspace_root,
            cwd=cwd,
        )
        exec_command = self._confined_exec_command(
            command_parts=command_parts,
            workspace_root=workspace_root,
            env=env,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=process_cwd,
                env=env,
                start_new_session=True,
            )

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError as e:
                try:
                    # process tree cleanup
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                raise ExecTimeoutError(command=command, timeout_s=timeout, cause=e) from e
        except ExecTimeoutError:
            raise
        except Exception as e:
            raise ExecTransportError(command=command, cause=e) from e

        return ExecResult(
            stdout=stdout or b"", stderr=stderr or b"", exit_code=proc.returncode or 0
        )

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
        _ = timeout
        env, cwd = await self._resolved_exec_context()
        workspace_root = Path(cwd).resolve()
        sanitized_command = self._prepare_exec_command(*command, shell=shell, user=user)
        command_parts = self._workspace_relative_command_parts(sanitized_command, workspace_root)
        process_cwd, command_parts = self._shell_workspace_process_context(
            command_parts=command_parts,
            workspace_root=workspace_root,
            cwd=cwd,
        )
        exec_command = self._confined_exec_command(
            command_parts=command_parts,
            workspace_root=workspace_root,
            env=env,
        )

        if tty:
            primary_fd, secondary_fd = os.openpty()

            def _preexec() -> None:
                os.setsid()
                fcntl.ioctl(secondary_fd, termios.TIOCSCTTY, 0)
                # PTY children should use default terminal signal behavior even if the parent
                # process temporarily ignores signals under the test runner.
                _restore_pty_child_signal_defaults()

            try:
                process = await asyncio.create_subprocess_exec(
                    *exec_command,
                    stdin=secondary_fd,
                    stdout=secondary_fd,
                    stderr=secondary_fd,
                    cwd=process_cwd,
                    env=env,
                    preexec_fn=_preexec,
                )
            except Exception:
                with suppress(OSError):
                    os.close(primary_fd)
                with suppress(OSError):
                    os.close(secondary_fd)
                raise
            else:
                with suppress(OSError):
                    os.close(secondary_fd)
            entry = _UnixPtyProcessEntry(process=process, tty=True, primary_fd=primary_fd)
            entry.pump_tasks = [asyncio.create_task(self._pump_pty_primary_fd(entry))]
        else:
            process = await asyncio.create_subprocess_exec(
                *exec_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=process_cwd,
                env=env,
                start_new_session=True,
            )
            entry = _UnixPtyProcessEntry(process=process, tty=False)
            entry.pump_tasks = [
                asyncio.create_task(self._pump_process_stream(entry, process.stdout)),
                asyncio.create_task(self._pump_process_stream(entry, process.stderr)),
            ]

        entry.wait_task = asyncio.create_task(self._watch_process_exit(entry))

        pruned_entry: _UnixPtyProcessEntry | None = None
        async with self._pty_lock:
            process_id = allocate_pty_process_id(self._reserved_pty_process_ids)
            self._reserved_pty_process_ids.add(process_id)
            pruned_entry = self._prune_pty_processes_if_needed()
            self._pty_processes[process_id] = entry
            process_count = len(self._pty_processes)

        if pruned_entry is not None:
            await self._terminate_pty_entry(pruned_entry)

        if process_count >= PTY_PROCESSES_WARNING:
            logger.warning(
                "PTY process count reached warning threshold: %s active sessions",
                process_count,
            )

        yield_time_ms = 10_000 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=clamp_pty_yield_time_ms(yield_time_ms),
            max_output_tokens=max_output_tokens,
        )
        return await self._finalize_pty_update(
            process_id=process_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        async with self._pty_lock:
            entry = self._resolve_pty_session_entry(
                pty_processes=self._pty_processes,
                session_id=session_id,
            )

        if chars:
            if not entry.tty or entry.primary_fd is None:
                raise RuntimeError("stdin is not available for this process")
            try:
                os.write(entry.primary_fd, chars.encode("utf-8"))
            except OSError as e:
                if e.errno not in {
                    errno.EIO,
                    errno.EBADF,
                    errno.EPIPE,
                    errno.ECONNRESET,
                }:
                    raise
            await asyncio.sleep(0.1)

        yield_time_ms = 250 if yield_time_s is None else int(yield_time_s * 1000)
        output, original_token_count = await self._collect_pty_output(
            entry=entry,
            yield_time_ms=resolve_pty_write_yield_time_ms(
                yield_time_ms=yield_time_ms, input_empty=chars == ""
            ),
            max_output_tokens=max_output_tokens,
        )
        entry.last_used = time.monotonic()
        return await self._finalize_pty_update(
            process_id=session_id,
            entry=entry,
            output=output,
            original_token_count=original_token_count,
        )

    async def pty_terminate_all(self) -> None:
        async with self._pty_lock:
            entries = list(self._pty_processes.values())
            self._pty_processes.clear()
            self._reserved_pty_process_ids.clear()

        for entry in entries:
            await self._terminate_pty_entry(entry)

    async def _resolved_exec_context(self) -> tuple[dict[str, str], str]:
        env = os.environ.copy()
        env.update(await self.state.manifest.environment.resolve())

        workspace = Path(self.state.manifest.root)
        if not workspace.exists():
            raise WorkspaceRootNotFoundError(path=workspace)

        env["HOME"] = str(workspace)
        return env, str(workspace)

    async def _pump_process_stream(
        self,
        entry: _UnixPtyProcessEntry,
        stream: asyncio.StreamReader | None,
    ) -> None:
        if stream is None:
            return

        while True:
            chunk = await stream.read(_PTY_READ_CHUNK_BYTES)
            if chunk == b"":
                break
            async with entry.output_lock:
                entry.output_chunks.append(chunk)
            entry.output_notify.set()

    async def _watch_process_exit(self, entry: _UnixPtyProcessEntry) -> None:
        await entry.process.wait()
        if entry.pump_tasks:
            await asyncio.gather(*entry.pump_tasks, return_exceptions=True)
        entry.output_closed.set()
        entry.output_notify.set()

    async def _pump_pty_primary_fd(self, entry: _UnixPtyProcessEntry) -> None:
        primary_fd = entry.primary_fd
        if primary_fd is None:
            return

        loop = asyncio.get_running_loop()
        while True:
            try:
                chunk = await loop.run_in_executor(None, os.read, primary_fd, _PTY_READ_CHUNK_BYTES)
            except OSError as e:
                if e.errno in {errno.EIO, errno.EBADF}:
                    break
                raise

            if chunk == b"":
                break
            async with entry.output_lock:
                entry.output_chunks.append(chunk)
            entry.output_notify.set()

    async def _collect_pty_output(
        self,
        *,
        entry: _UnixPtyProcessEntry,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> tuple[bytes, int | None]:
        deadline = time.monotonic() + (yield_time_ms / 1000)
        output = bytearray()

        while True:
            async with entry.output_lock:
                while entry.output_chunks:
                    output.extend(entry.output_chunks.popleft())

            if time.monotonic() >= deadline:
                break

            if entry.output_closed.is_set():
                async with entry.output_lock:
                    while entry.output_chunks:
                        output.extend(entry.output_chunks.popleft())
                break

            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break

            try:
                await asyncio.wait_for(entry.output_notify.wait(), timeout=remaining_s)
            except asyncio.TimeoutError:
                break
            entry.output_notify.clear()

        text = output.decode("utf-8", errors="replace")
        truncated_text, original_token_count = truncate_text_by_tokens(text, max_output_tokens)
        return truncated_text.encode("utf-8", errors="replace"), original_token_count

    async def _finalize_pty_update(
        self,
        *,
        process_id: int,
        entry: _UnixPtyProcessEntry,
        output: bytes,
        original_token_count: int | None,
    ) -> PtyExecUpdate:
        exit_code: int | None = entry.process.returncode
        live_process_id: int | None = process_id

        if exit_code is not None:
            async with self._pty_lock:
                removed = self._pty_processes.pop(process_id, None)
                self._reserved_pty_process_ids.discard(process_id)
            if removed is not None:
                await self._terminate_pty_entry(removed)
            live_process_id = None

        return PtyExecUpdate(
            process_id=live_process_id,
            output=output,
            exit_code=exit_code,
            original_token_count=original_token_count,
        )

    def _prune_pty_processes_if_needed(self) -> _UnixPtyProcessEntry | None:
        if len(self._pty_processes) < PTY_PROCESSES_MAX:
            return None

        meta = [
            (process_id, entry.last_used, entry.process.returncode is not None)
            for process_id, entry in self._pty_processes.items()
        ]
        process_id = process_id_to_prune_from_meta(meta)
        if process_id is None:
            return None

        self._reserved_pty_process_ids.discard(process_id)
        return self._pty_processes.pop(process_id, None)

    async def _terminate_pty_entry(self, entry: _UnixPtyProcessEntry) -> None:
        process = entry.process
        primary_fd = entry.primary_fd
        entry.primary_fd = None

        if process.returncode is None and process.pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

        for task in entry.pump_tasks:
            task.cancel()
        if entry.wait_task is not None:
            entry.wait_task.cancel()
        if entry.tty:
            if primary_fd is not None:
                # On macOS we have observed os.close() on the PTY master fd block while a
                # background reader thread is still inside os.read(). Close it off-thread so
                # session teardown remains best-effort and non-blocking.
                asyncio.create_task(asyncio.to_thread(_close_fd_quietly, primary_fd))
            entry.output_closed.set()
            entry.output_notify.set()
            return

        if primary_fd is not None:
            _close_fd_quietly(primary_fd)
        await asyncio.gather(*entry.pump_tasks, return_exceptions=True)
        if entry.wait_task is not None:
            await asyncio.gather(entry.wait_task, return_exceptions=True)

    def _confined_exec_command(
        self,
        *,
        command_parts: list[str],
        workspace_root: Path,
        env: Mapping[str, str],
    ) -> list[str]:
        if sys.platform != "darwin":
            return command_parts

        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise ExecTransportError(
                command=command_parts,
                context={
                    "reason": "unix_local_confinement_unavailable",
                    "platform": sys.platform,
                    "workspace_root": str(workspace_root),
                },
            )

        profile = self._darwin_exec_profile(
            workspace_root,
            extra_read_paths=self._darwin_additional_read_paths(
                command_parts=command_parts,
                env=env,
            ),
            extra_path_grants=self._darwin_extra_path_grant_roots(),
        )
        return [sandbox_exec, "-p", profile, *command_parts]

    @staticmethod
    def _workspace_relative_command_parts(
        command: Sequence[str | Path],
        workspace_root: Path,
    ) -> list[str]:
        command_parts = [str(part) for part in command]
        rewritten = [command_parts[0]]
        for part in command_parts[1:]:
            path_part = Path(part)
            if not path_part.is_absolute():
                rewritten.append(part)
                continue
            try:
                relative = path_part.relative_to(workspace_root)
            except ValueError:
                rewritten.append(part)
                continue
            rewritten.append("." if not relative.parts else relative.as_posix())
        return rewritten

    @staticmethod
    def _darwin_allowable_read_roots(path: Path, *, host_home: Path) -> list[Path]:
        candidates: set[Path] = set()
        normalized = path.expanduser()
        try:
            resolved = normalized.resolve(strict=False)
        except OSError:
            resolved = normalized

        if normalized.is_dir():
            candidates.add(normalized)
        else:
            candidates.add(normalized.parent)

        if resolved.is_dir():
            candidates.add(resolved)
        else:
            candidates.add(resolved.parent)

        resolved_text = resolved.as_posix()
        if resolved_text == "/opt/homebrew" or resolved_text.startswith("/opt/homebrew/"):
            candidates.add(Path("/opt/homebrew"))
        if resolved_text == "/usr/local" or resolved_text.startswith("/usr/local/"):
            candidates.add(Path("/usr/local"))
        if resolved_text == "/Library/Frameworks" or resolved_text.startswith(
            "/Library/Frameworks/"
        ):
            candidates.add(Path("/Library/Frameworks"))

        try:
            relative_to_home = resolved.relative_to(host_home)
        except ValueError:
            relative_to_home = None
        if relative_to_home is not None and relative_to_home.parts:
            first_segment = relative_to_home.parts[0]
            if first_segment.startswith("."):
                candidates.add(host_home / first_segment)
            elif len(relative_to_home.parts) >= 2 and relative_to_home.parts[:2] == (
                "Library",
                "Python",
            ):
                candidates.add(host_home / "Library" / "Python")

        return sorted(
            candidates, key=lambda candidate: (len(candidate.parts), candidate.as_posix())
        )

    def _darwin_additional_read_paths(
        self,
        *,
        command_parts: list[str],
        env: Mapping[str, str],
    ) -> list[Path]:
        host_home = Path.home().resolve()
        allowed: list[Path] = []
        seen: set[str] = set()

        def _append(path: str | Path | None) -> None:
            if path is None:
                return
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                return
            for root in self._darwin_allowable_read_roots(candidate, host_home=host_home):
                key = root.as_posix()
                if key in seen:
                    continue
                seen.add(key)
                allowed.append(root)

        for path_entry in env.get("PATH", "").split(os.pathsep):
            if path_entry:
                _append(path_entry)

        executable = shutil.which(command_parts[0], path=env.get("PATH"))
        _append(executable)
        return allowed

    def _darwin_extra_path_grant_roots(self) -> list[tuple[Path, bool]]:
        roots: list[tuple[Path, bool]] = []
        seen: set[tuple[str, bool]] = set()

        def _append(path: Path, *, read_only: bool) -> None:
            _raise_if_filesystem_root(path, resolved=True)
            key = (path.as_posix(), read_only)
            if key in seen:
                return
            seen.add(key)
            roots.append((path, read_only))

        for grant in self.state.manifest.extra_path_grants:
            grant_path = Path(grant.path).expanduser()
            try:
                resolved = grant_path.resolve(strict=False)
            except OSError:
                _append(grant_path, read_only=grant.read_only)
                continue
            _raise_if_filesystem_root(resolved, resolved=True)
            _append(grant_path, read_only=grant.read_only)
            if resolved != grant_path:
                _append(resolved, read_only=grant.read_only)

        return roots

    def _darwin_exec_profile(
        self,
        workspace_root: Path,
        *,
        extra_read_paths: Sequence[Path] = (),
        extra_path_grants: Sequence[tuple[Path, bool]] = (),
    ) -> str:
        def _literal(path: Path | str) -> str:
            escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'

        denied_paths = [
            Path("/Users"),
            Path("/Volumes"),
            Path("/Applications"),
            Path("/Library"),
            Path("/opt"),
            Path("/etc"),
            Path("/private/etc"),
            Path("/tmp"),
            Path("/private/tmp"),
            Path("/private"),
            Path("/var"),
            Path("/usr"),
        ]
        allow_rules = [
            f"(allow file-read-data file-read-metadata (subpath {_literal(workspace_root)}))",
            f"(allow file-write* (subpath {_literal(workspace_root)}))",
            *[
                f"(allow file-read-data file-read-metadata (subpath {_literal(path)}))"
                for path in extra_read_paths
            ],
            *[
                f"(allow file-read-data file-read-metadata (subpath {_literal(path)}))"
                for path, _read_only in extra_path_grants
            ],
            *[
                f"(allow file-write* (subpath {_literal(path)}))"
                for path, read_only in extra_path_grants
                if not read_only
            ],
            *[
                f"(deny file-write* (subpath {_literal(path)}))"
                for path, read_only in extra_path_grants
                if read_only
            ],
            '(allow file-read-data file-read-metadata (subpath "/usr/bin"))',
            '(allow file-read-data file-read-metadata (subpath "/usr/lib"))',
            '(allow file-read-data file-read-metadata (subpath "/bin"))',
            '(allow file-read-data file-read-metadata (subpath "/System"))',
            '(allow file-read-data file-read-metadata (literal "/private/var/select/sh"))',
            '(allow file-write* (literal "/dev/null"))',
        ]
        deny_rules = "\n".join(
            f"(deny file-read-data (subpath {_literal(path)}))\n"
            f"(deny file-write* (subpath {_literal(path)}))"
            for path in denied_paths
        )
        return "\n".join(
            [
                "(version 1)",
                "(allow default)",
                deny_rules,
                *allow_rules,
            ]
        )

    @staticmethod
    def _shell_workspace_process_context(
        *,
        command_parts: list[str],
        workspace_root: Path,
        cwd: str,
    ) -> tuple[str, list[str]]:
        if len(command_parts) < 3 or command_parts[0] != "sh" or command_parts[1] != "-c":
            return cwd, command_parts

        workspace_cd = f"cd {shlex.quote(str(workspace_root))} && {command_parts[2]}"
        rewritten = [*command_parts]
        rewritten[2] = workspace_cd
        return "/", rewritten

    def normalize_path(self, path: Path | str, *, for_write: bool = False) -> Path:
        policy = self._workspace_path_policy()
        return policy.normalize_path(path, for_write=for_write, resolve_symlinks=True)

    async def ls(
        self,
        path: Path | str,
        *,
        user: str | User | None = None,
    ) -> list[FileEntry]:
        if user is not None:
            return await super().ls(path, user=user)

        normalized = self.normalize_path(path)
        command = ("ls", "-la", "--", str(normalized))
        try:
            with os.scandir(normalized) as entries:
                listed: list[FileEntry] = []
                for entry in entries:
                    stat_result = entry.stat(follow_symlinks=False)
                    if entry.is_symlink():
                        kind = EntryKind.SYMLINK
                    elif entry.is_dir(follow_symlinks=False):
                        kind = EntryKind.DIRECTORY
                    elif entry.is_file(follow_symlinks=False):
                        kind = EntryKind.FILE
                    else:
                        kind = EntryKind.OTHER
                    listed.append(
                        FileEntry(
                            path=entry.path,
                            permissions=Permissions.from_mode(stat_result.st_mode),
                            owner=str(stat_result.st_uid),
                            group=str(stat_result.st_gid),
                            size=stat_result.st_size,
                            kind=kind,
                        )
                    )
                return listed
        except OSError as e:
            raise ExecNonZeroError(
                ExecResult(stdout=b"", stderr=str(e).encode("utf-8"), exit_code=1),
                command=command,
                cause=e,
            ) from e

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        if user is not None:
            normalized = await self._check_mkdir_with_exec(path, parents=parents, user=user)
        else:
            normalized = self.normalize_path(path, for_write=True)
        try:
            normalized.mkdir(parents=parents, exist_ok=True)
        except OSError as e:
            raise WorkspaceArchiveWriteError(path=normalized, cause=e) from e

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        if user is not None:
            normalized = await self._check_rm_with_exec(path, recursive=recursive, user=user)
        else:
            normalized = self.normalize_path(path, for_write=True)
        try:
            if normalized.is_dir() and not normalized.is_symlink():
                if recursive:
                    shutil.rmtree(normalized)
                else:
                    normalized.rmdir()
            else:
                normalized.unlink()
        except FileNotFoundError as e:
            if recursive:
                return
            raise ExecNonZeroError(
                ExecResult(stdout=b"", stderr=str(e).encode("utf-8"), exit_code=1),
                command=("rm", "-rf" if recursive else "--", str(normalized)),
                cause=e,
            ) from e
        except OSError as e:
            raise WorkspaceArchiveWriteError(path=normalized, cause=e) from e

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        if user is not None:
            await self._check_read_with_exec(path, user=user)

        workspace_path = self.normalize_path(path)
        try:
            return workspace_path.open("rb")
        except FileNotFoundError as e:
            raise WorkspaceReadNotFoundError(path=path, cause=e) from e
        except OSError as e:
            raise WorkspaceArchiveReadError(path=path, cause=e) from e

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        payload = coerce_write_payload(path=path, data=data)

        workspace_path = self.normalize_path(path, for_write=True)
        if user is not None:
            await self._write_stream_with_exec(workspace_path, payload.stream, user=user)
            return

        try:
            workspace_path.parent.mkdir(parents=True, exist_ok=True)
            with workspace_path.open("wb") as f:
                shutil.copyfileobj(payload.stream, f)
        except OSError as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def _write_stream_with_exec(
        self,
        path: Path,
        stream: io.IOBase,
        *,
        user: str | User,
    ) -> None:
        env, cwd = await self._resolved_exec_context()
        workspace_root = Path(cwd).resolve()
        command_parts = self._prepare_exec_command(
            "sh",
            "-c",
            'mkdir -p "$(dirname "$1")" && cat > "$1"',
            "sh",
            str(path),
            shell=False,
            user=user,
        )
        command_parts = self._workspace_relative_command_parts(command_parts, workspace_root)
        process_cwd, command_parts = self._shell_workspace_process_context(
            command_parts=command_parts,
            workspace_root=workspace_root,
            cwd=cwd,
        )
        exec_command = self._confined_exec_command(
            command_parts=command_parts,
            workspace_root=workspace_root,
            env=env,
        )

        payload = stream.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif not isinstance(payload, bytes):
            payload = bytes(payload)

        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=process_cwd,
                env=env,
                start_new_session=True,
            )
            stdout, stderr = await proc.communicate(payload)
        except OSError as e:
            raise WorkspaceArchiveWriteError(path=path, cause=e) from e

        if proc.returncode:
            raise WorkspaceArchiveWriteError(
                path=path,
                context={
                    "command": command_parts,
                    "stdout": stdout.decode("utf-8", errors="replace"),
                    "stderr": stderr.decode("utf-8", errors="replace"),
                },
            )

    async def running(self) -> bool:
        return self._running

    async def persist_workspace(self) -> io.IOBase:
        root = Path(self.state.manifest.root)
        if not root.exists():
            raise WorkspaceArchiveReadError(
                path=root, context={"reason": "workspace_root_not_found"}
            )

        skip = self._persist_workspace_skip_relpaths()
        buf = io.BytesIO()
        try:
            with tarfile.open(fileobj=buf, mode="w") as tar:
                tar.add(
                    root,
                    arcname=".",
                    filter=lambda ti: (
                        None
                        if should_skip_tar_member(
                            ti.name,
                            skip_rel_paths=skip,
                            root_name=None,
                        )
                        else ti
                    ),
                )
        except (tarfile.TarError, OSError) as e:
            raise WorkspaceArchiveReadError(path=root, cause=e) from e

        buf.seek(0)
        return buf

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        root = Path(self.state.manifest.root)
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=data, mode="r:*") as tar:
                safe_extract_tarfile(
                    tar,
                    root=root,
                    allow_external_symlink_targets=False,
                )
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root, context={"reason": e.reason, "member": e.member}, cause=e
            ) from e
        except (tarfile.TarError, OSError) as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e


class UnixLocalSandboxClient(BaseSandboxClient[UnixLocalSandboxClientOptions | None]):
    backend_id = "unix_local"
    supports_default_options = True
    _instrumentation: Instrumentation

    def __init__(
        self,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._instrumentation = instrumentation or Instrumentation()
        self._dependencies = dependencies

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: UnixLocalSandboxClientOptions | None = None,
    ) -> SandboxSession:
        resolved_options = options or UnixLocalSandboxClientOptions()
        # For local execution, runner-created sessions should always get an isolated temp root
        # unless the caller explicitly chose a custom host path.
        workspace_root_owned = False
        if manifest is None or manifest.root == _DEFAULT_MANIFEST_ROOT:
            workspace_dir = tempfile.mkdtemp(prefix=_DEFAULT_WORKSPACE_PREFIX)
            workspace_root_owned = True
            if manifest is None:
                manifest = Manifest(root=workspace_dir)
            else:
                manifest = manifest.model_copy(update={"root": workspace_dir}, deep=True)

        session_id = uuid.uuid4()
        snapshot_id = str(session_id)
        snapshot_instance = resolve_snapshot(snapshot, snapshot_id)
        state = UnixLocalSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=snapshot_instance,
            workspace_root_owned=workspace_root_owned,
            exposed_ports=resolved_options.exposed_ports,
        )
        inner = UnixLocalSandboxSession.from_state(state)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        """Best-effort cleanup of the on-disk workspace directory."""
        inner = session._inner
        if not isinstance(inner, UnixLocalSandboxSession):
            raise TypeError("UnixLocalSandboxClient.delete expects a UnixLocalSandboxSession")
        if not inner.state.workspace_root_owned:
            return session
        unmount_failed = False
        for mount_entry, mount_path in inner.state.manifest.ephemeral_mount_targets():
            try:
                await mount_entry.unmount(inner, mount_path, Path("/"))
            except Exception:
                unmount_failed = True
                logger.warning(
                    "Failed to unmount UnixLocal workspace mount before deleting root: %s",
                    mount_path,
                    exc_info=True,
                )
        if unmount_failed:
            return session
        try:
            shutil.rmtree(Path(inner.state.manifest.root), ignore_errors=False)
        except FileNotFoundError:
            pass
        except Exception:
            pass
        return session

    async def resume(
        self,
        state: SandboxSessionState,
    ) -> SandboxSession:
        if not isinstance(state, UnixLocalSandboxSessionState):
            raise TypeError("UnixLocalSandboxClient.resume expects a UnixLocalSandboxSessionState")
        inner = UnixLocalSandboxSession.from_state(state)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return UnixLocalSandboxSessionState.model_validate(payload)
