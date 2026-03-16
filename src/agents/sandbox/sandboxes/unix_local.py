import asyncio
import io
import logging
import os
import shlex
import shutil
import signal
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

from ..codex_config import CodexConfig, apply_codex_to_manifest, apply_codex_to_session_state
from ..entries import resolve_workspace_path
from ..errors import (
    ExecNonZeroError,
    ExecTimeoutError,
    ExecTransportError,
    InvalidManifestPathError,
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
from ..session.sandbox_client import BaseSandboxClient
from ..session.workspace_payloads import coerce_write_payload
from ..snapshot import SnapshotSpec, resolve_snapshot
from ..types import ExecResult, Permissions, User
from ..util.tar_utils import (
    UnsafeTarMemberError,
    safe_extract_tarfile,
    should_skip_tar_member,
)

_DEFAULT_WORKSPACE_PREFIX = "uc-local-"
_DEFAULT_MANIFEST_ROOT = cast(str, Manifest.model_fields["root"].default)

logger = logging.getLogger(__name__)


class UnixLocalSandboxSessionState(SandboxSessionState):
    workspace_root_owned: bool = False


class UnixLocalSandboxSession(BaseSandboxSession):
    """
    Unix-only session implementation that runs commands on the host and uses the host filesystem
    as the workspace (rooted at `self.state.manifest.root`).
    """

    state: UnixLocalSandboxSessionState
    _running: bool

    def __init__(self, *, state: UnixLocalSandboxSessionState) -> None:
        self.state = state
        self._running = False

    @classmethod
    def from_state(cls, state: UnixLocalSandboxSessionState) -> "UnixLocalSandboxSession":
        return cls(state=state)

    async def start(self) -> None:
        workspace = Path(self.state.manifest.root)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise WorkspaceStartError(path=workspace, cause=e) from e

        self._running = True
        await super().start()

    async def stop(self) -> None:
        try:
            await super().stop()
        except Exception as e:
            raise WorkspaceStopError(path=Path(self.state.manifest.root), cause=e) from e

    async def apply_manifest(self, *, only_ephemeral: bool = False) -> MaterializationResult:
        if self.state.manifest.users or self.state.manifest.groups:
            raise ValueError(
                "UnixLocalSandboxSession does not support manifest users or groups because "
                "provisioning would run on the host machine"
            )
        return await super().apply_manifest(only_ephemeral=only_ephemeral)

    async def provision_manifest_accounts(self) -> None:
        if self.state.manifest.users or self.state.manifest.groups:
            raise ValueError(
                "UnixLocalSandboxSession does not support manifest users or groups because "
                "provisioning would run on the host machine"
            )

    async def shutdown(self) -> None:
        # Best-effort: mark session not running. We intentionally do not delete the workspace
        # directory here; cleanup is handled by the Client.delete().
        self._running = False

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

    async def _resolved_exec_context(self) -> tuple[dict[str, str], str]:
        env = os.environ.copy()
        env.update(await self.state.manifest.environment.resolve())

        workspace = Path(self.state.manifest.root)
        if not workspace.exists():
            raise WorkspaceRootNotFoundError(path=workspace)

        env["HOME"] = str(workspace)
        return env, str(workspace)

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
        )
        return [sandbox_exec, "-p", profile, *command_parts]

    @staticmethod
    def _workspace_relative_command_parts(
        command: tuple[str | Path, ...],
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

    def _darwin_exec_profile(
        self,
        workspace_root: Path,
        *,
        extra_read_paths: Sequence[Path] = (),
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

    def _resolve_workspace_path(self, path: Path) -> Path:
        workspace_root = Path(self.state.manifest.root).resolve()
        confined = resolve_workspace_path(
            workspace_root,
            path,
            allow_absolute_within_root=True,
        )
        resolved = confined.resolve(strict=False)
        try:
            resolved.relative_to(workspace_root)
        except ValueError as exc:
            reason: Literal["absolute", "escape_root"] = (
                "absolute" if path.is_absolute() else "escape_root"
            )
            raise InvalidManifestPathError(rel=path, reason=reason, cause=exc) from exc
        return resolved

    def normalize_path(self, path: Path | str) -> Path:
        if isinstance(path, str):
            path = Path(path)
        return self._resolve_workspace_path(path)

    async def ls(self, path: Path | str) -> list[FileEntry]:
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

    async def mkdir(self, path: Path | str, *, parents: bool = False) -> None:
        normalized = self.normalize_path(path)
        try:
            normalized.mkdir(parents=parents, exist_ok=True)
        except OSError as e:
            raise WorkspaceArchiveWriteError(path=normalized, cause=e) from e

    async def rm(self, path: Path | str, *, recursive: bool = False) -> None:
        normalized = self.normalize_path(path)
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

    async def read(self, path: Path) -> io.IOBase:
        workspace_path = self._resolve_workspace_path(path)
        try:
            return workspace_path.open("rb")
        except FileNotFoundError as e:
            raise WorkspaceReadNotFoundError(path=path, cause=e) from e
        except OSError as e:
            raise WorkspaceArchiveReadError(path=path, cause=e) from e

    async def write(self, path: Path, data: io.IOBase) -> None:
        payload = coerce_write_payload(path=path, data=data)

        workspace_path = self._resolve_workspace_path(path)
        try:
            workspace_path.parent.mkdir(parents=True, exist_ok=True)
            with workspace_path.open("wb") as f:
                shutil.copyfileobj(payload.stream, f)
        except OSError as e:
            raise WorkspaceArchiveWriteError(path=workspace_path, cause=e) from e

    async def running(self) -> bool:
        return self._running

    async def persist_workspace(self) -> io.IOBase:
        root = Path(self.state.manifest.root)
        if not root.exists():
            raise WorkspaceArchiveReadError(
                path=root, context={"reason": "workspace_root_not_found"}
            )

        skip = self.state.manifest.ephemeral_persistence_paths()
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
                safe_extract_tarfile(tar, root=root)
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=root, context={"reason": e.reason, "member": e.member}, cause=e
            ) from e
        except (tarfile.TarError, OSError) as e:
            raise WorkspaceArchiveWriteError(path=root, cause=e) from e


class UnixLocalSandboxClient(BaseSandboxClient[None]):
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
        snapshot: SnapshotSpec | None = None,
        manifest: Manifest | None = None,
        codex: bool | CodexConfig = False,
        options: None = None,
    ) -> SandboxSession:
        if options is not None:
            raise ValueError("UnixLocalSandboxClient.create does not accept options")
        manifest = apply_codex_to_manifest(manifest, codex)
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
        *,
        codex: bool | CodexConfig = False,
    ) -> SandboxSession:
        if not isinstance(state, UnixLocalSandboxSessionState):
            raise TypeError("UnixLocalSandboxClient.resume expects a UnixLocalSandboxSessionState")
        inner = UnixLocalSandboxSession.from_state(apply_codex_to_session_state(state, codex))
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return UnixLocalSandboxSessionState.model_validate(payload)
