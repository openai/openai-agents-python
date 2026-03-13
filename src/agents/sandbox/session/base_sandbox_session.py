import abc
import io
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

from typing_extensions import Self

from ..entries import BaseEntry, resolve_workspace_path
from ..errors import (
    ExecNonZeroError,
    InvalidCompressionSchemeError,
)
from ..files import FileEntry
from ..materialization import MaterializationResult, MaterializedFile
from ..snapshot import NoopSnapshot
from ..types import ExecResult, User
from ..util.parse_utils import parse_ls_la
from .archive_extraction import (
    WorkspaceArchiveExtractor,
    safe_zip_member_rel_path,
    zipfile_compatible_stream,
)
from .dependencies import Dependencies
from .manifest_application import ManifestApplier
from .sandbox_session_state import SandboxSessionState


class BaseSandboxSession(abc.ABC):
    state: SandboxSessionState
    _dependencies: Dependencies | None = None
    _dependencies_closed: bool = False

    async def start(self) -> None:
        if await self.state.snapshot.restorable():
            # Ensure the snapshot is the single source of truth on resume.
            await self._clear_workspace_root_on_resume()
            await self.hydrate_workspace(await self.state.snapshot.restore())
            if self.should_provision_manifest_accounts_on_resume():
                await self.provision_manifest_accounts()
            # Reapply only ephemeral manifest entries on resume so persisted workspace state wins
            # for durable files while temporary scaffolding is rebuilt for the new process.
            await self.apply_manifest(only_ephemeral=True)
        else:
            await self.apply_manifest()

    async def stop(self) -> None:
        """
        Persist/snapshot the workspace.

        Note: `stop()` is intentionally persistence-only. Sandboxes that need to tear down
        sandbox resources (Docker containers, remote sessions, etc.) should implement
        `shutdown()` instead.
        """
        if isinstance(self.state.snapshot, NoopSnapshot):
            return
        await self.state.snapshot.persist(await self.persist_workspace())

    @abc.abstractmethod
    async def shutdown(self) -> None:
        """
        Tear down sandbox resources (best-effort).

        Default is a no-op. Sandbox-specific sessions (e.g. Docker) should override.
        """

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

    async def _aclose_dependencies(self) -> None:
        dependencies = self._dependencies
        if dependencies is None or self._dependencies_closed:
            return
        self._dependencies_closed = True
        await dependencies.aclose()

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

    @abc.abstractmethod
    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult: ...

    @abc.abstractmethod
    async def read(self, path: Path) -> io.IOBase:
        """Read a file from the session's workspace.

        :param path: Absolute path in the container or path relative to the
                workspace root.
        :returns: A readable file-like object.
        :raises: FileNotFoundError: If the path does not exist.
        """

    @abc.abstractmethod
    async def write(self, path: Path, data: io.IOBase) -> None:
        """Write a file into the session's workspace.

        :param path: Absolute path in the container or path relative to the
                workspace root.
        :param data: A file-like object positioned at the start of the payload.
        """

    @abc.abstractmethod
    async def running(self) -> bool:
        """
        :returns: whether the underlying sandbox is currently running.
        """

    @abc.abstractmethod
    async def persist_workspace(self) -> io.IOBase:
        """Serialize the session's workspace into a byte stream.

        :returns: A readable tar binary stream representing the full workspace.
        """

    @abc.abstractmethod
    async def hydrate_workspace(self, data: io.IOBase) -> None:
        """Populate the session's workspace from a serialized byte stream.

        :param data: A readable tar binary stream as produced by `persist_workspace`.
        """

    async def ls(self, path: Path | str) -> list[FileEntry]:
        """List directory contents.

        :param path: Path to list.
        :returns: A list of `FileEntry` objects.
        """
        path = self.normalize_path(path)

        cmd = ("ls", "-la", "--", str(path))
        result = await self.exec(*cmd, shell=False)
        if not result.ok():
            raise ExecNonZeroError(result, command=cmd)

        return parse_ls_la(result.stdout.decode("utf-8", errors="replace"), base=str(path))

    async def rm(self, path: Path | str, *, recursive: bool = False) -> None:
        """Remove a file or directory.

        :param path: Path to remove.
        :param recursive: If true, remove directories recursively.
        """
        path = self.normalize_path(path)

        cmd: list[str] = ["rm"]
        if recursive:
            cmd.append("-rf")
        cmd.extend(["--", str(path)])

        result = await self.exec(*cmd, shell=False)
        if not result.ok():
            raise ExecNonZeroError(result, command=cmd)

    async def mkdir(self, path: Path | str, *, parents: bool = False) -> None:
        """Create a directory.

        :param path: Directory to create on the remote.
        :param parents: If true, create missing parents.
        """
        path = self.normalize_path(path)

        cmd: list[str] = ["mkdir"]
        if parents:
            cmd.append("-p")
        cmd.append(str(path))

        result = await self.exec(*cmd, shell=False)
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

        normalized_path = self.normalize_path(path)
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

    def normalize_path(self, path: Path | str) -> Path:
        if isinstance(path, str):
            path = Path(path)

        root = Path(self.state.manifest.root)
        return resolve_workspace_path(root, path, allow_absolute_within_root=True)

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
    def _zipfile_compatible_stream(stream: io.IOBase) -> io.IOBase:
        return zipfile_compatible_stream(stream)

    @staticmethod
    def _safe_zip_member_rel_path(member) -> Path | None:
        return safe_zip_member_rel_path(member)

    async def apply_manifest(self, *, only_ephemeral: bool = False) -> MaterializationResult:
        applier = ManifestApplier(
            mkdir=lambda path: self.mkdir(path, parents=True),
            exec_checked_nonzero=self._exec_checked_nonzero,
            apply_entry=lambda artifact, dest, base_dir: artifact.apply(self, dest, base_dir),
        )
        return await applier.apply_manifest(
            self.state.manifest,
            only_ephemeral=only_ephemeral,
            base_dir=self._manifest_base_dir(),
        )

    async def provision_manifest_accounts(self) -> None:
        applier = ManifestApplier(
            mkdir=lambda path: self.mkdir(path, parents=True),
            exec_checked_nonzero=self._exec_checked_nonzero,
            apply_entry=lambda artifact, dest, base_dir: artifact.apply(self, dest, base_dir),
        )
        await applier.provision_accounts(self.state.manifest)

    def should_provision_manifest_accounts_on_resume(self) -> bool:
        return True

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

        root = Path(self.state.manifest.root)
        try:
            entries = await self.ls(root)
        except ExecNonZeroError:
            # If the root doesn't exist (or isn't listable), treat it as empty and let hydrate/apply
            # create it as needed.
            return

        for entry in entries:
            # `parse_ls_la` filters "." and ".." already; remove everything else recursively.
            await self.rm(Path(entry.path), recursive=True)
