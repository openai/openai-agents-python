from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from .types import ExecResult


class ErrorCode(str, Enum):
    """Stable, machine-readable error codes for `SandboxError`."""

    def __str__(self) -> str:
        return str(self.value)

    INVALID_MANIFEST_PATH = "invalid_manifest_path"
    INVALID_COMPRESSION_SCHEME = "invalid_compression_scheme"
    EXPOSED_PORT_UNAVAILABLE = "exposed_port_unavailable"
    EXEC_NONZERO = "exec_nonzero"
    EXEC_TIMEOUT = "exec_timeout"
    EXEC_TRANSPORT_ERROR = "exec_transport_error"
    PTY_SESSION_NOT_FOUND = "pty_session_not_found"
    APPLY_PATCH_INVALID_PATH = "apply_patch_invalid_path"
    APPLY_PATCH_INVALID_DIFF = "apply_patch_invalid_diff"
    APPLY_PATCH_FILE_NOT_FOUND = "apply_patch_file_not_found"
    APPLY_PATCH_DECODE_ERROR = "apply_patch_decode_error"

    WORKSPACE_READ_NOT_FOUND = "workspace_read_not_found"
    WORKSPACE_ARCHIVE_READ_ERROR = "workspace_archive_read_error"
    WORKSPACE_ARCHIVE_WRITE_ERROR = "workspace_archive_write_error"
    WORKSPACE_WRITE_TYPE_ERROR = "workspace_write_type_error"
    WORKSPACE_STOP_ERROR = "workspace_stop_error"
    WORKSPACE_START_ERROR = "workspace_start_error"
    WORKSPACE_ROOT_NOT_FOUND = "workspace_root_not_found"

    LOCAL_FILE_READ_ERROR = "local_file_read_error"
    LOCAL_DIR_READ_ERROR = "local_dir_read_error"
    LOCAL_CHECKSUM_ERROR = "local_checksum_error"

    GIT_MISSING_IN_IMAGE = "git_missing_in_image"
    GIT_CLONE_ERROR = "git_clone_error"
    GIT_COPY_ERROR = "git_copy_error"

    MOUNT_MISSING_TOOL = "mount_missing_tool"
    MOUNT_FAILED = "mount_failed"
    MOUNT_CONFIG_INVALID = "mount_config_invalid"
    SKILLS_CONFIG_INVALID = "skills_config_invalid"
    SANDBOX_CONFIG_INVALID = "sandbox_config_invalid"

    SNAPSHOT_PERSIST_ERROR = "snapshot_persist_error"
    SNAPSHOT_RESTORE_ERROR = "snapshot_restore_error"
    SNAPSHOT_NOT_RESTORABLE = "snapshot_not_restorable"


OpName = Literal[
    "start",
    "stop",
    "exec",
    "read",
    "write",
    "shutdown",
    "running",
    "persist_workspace",
    "hydrate_workspace",
    "resolve_exposed_port",
    "materialize",
    "snapshot_persist",
    "snapshot_restore",
    "apply_patch",
]


@dataclass(eq=False)
class SandboxError(Exception):
    """Base class for structured, user-facing sandbox errors.

    Attributes:
        message: Human-readable error message.
        error_code: Stable, machine-readable code for programmatic handling.
        op: The operation where the error occurred.
        context: Structured metadata to aid debugging.
        cause: Optional underlying exception.
    """

    message: str
    error_code: ErrorCode
    op: OpName
    context: dict[str, object]
    cause: BaseException | None = None

    def __post_init__(self) -> None:
        super().__init__(self.message)
        if self.cause is not None:
            self.__cause__ = self.cause

    @property
    def code(self) -> str:
        """Backward-compatible alias for `error_code`."""

        return str(self.error_code)


class ConfigurationError(SandboxError):
    """Raised when validating user-provided configuration and inputs."""


class SandboxRuntimeError(SandboxError):
    """Raised for sandbox failures (e.g., Docker/IO/transport)."""


class ArtifactError(SandboxError):
    """Raised while materializing input artifacts (local files, git repos)."""


class SnapshotError(SandboxError):
    """Raised for snapshot persist/restore errors."""


class ApplyPatchError(ConfigurationError):
    """Base class for apply_patch validation errors."""


def _as_context(context: Mapping[str, object] | None) -> dict[str, object]:
    return dict(context or {})


def _format_command(command: Sequence[str | Path]) -> str:
    return " ".join(str(p) for p in command)


class InvalidManifestPathError(ConfigurationError):
    """Manifest path was invalid (absolute or escaped the workspace root)."""

    def __init__(
        self,
        *,
        rel: str | Path,
        reason: Literal["absolute", "escape_root"],
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        msg = (
            f"manifest path must be relative: {rel}"
            if reason == "absolute"
            else f"manifest path must not escape root: {rel}"
        )
        super().__init__(
            message=msg,
            error_code=ErrorCode.INVALID_MANIFEST_PATH,
            op="materialize",
            context={"rel": str(rel), "reason": reason, **_as_context(context)},
            cause=cause,
        )


class InvalidCompressionSchemeError(ConfigurationError):
    """Compression scheme was missing or unsupported for a workspace write."""

    def __init__(
        self,
        *,
        path: Path,
        scheme: str | None,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        msg = (
            "could not determine compression scheme"
            if not scheme
            else "compression scheme must be one of 'zip' 'tar'"
        )
        super().__init__(
            message=msg,
            error_code=ErrorCode.INVALID_COMPRESSION_SCHEME,
            op="write",
            context={"path": str(path), "scheme": scheme, **_as_context(context)},
            cause=cause,
        )


class ExposedPortUnavailableError(SandboxRuntimeError):
    """Requested port is not configured or cannot be resolved for host access."""

    def __init__(
        self,
        *,
        port: int,
        exposed_ports: Sequence[int],
        reason: str,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        if reason == "not_configured":
            message = f"port {port} is not configured for host exposure"
        else:
            message = f"port {port} could not be resolved for host exposure"
        super().__init__(
            message=message,
            error_code=ErrorCode.EXPOSED_PORT_UNAVAILABLE,
            op="resolve_exposed_port",
            context={
                "port": port,
                "exposed_ports": list(exposed_ports),
                "reason": reason,
                **_as_context(context),
            },
            cause=cause,
        )


class ExecFailureError(SandboxRuntimeError):
    """Base class for exec()-related failures."""

    command: tuple[str, ...]

    def __init__(
        self,
        *,
        message: str,
        error_code: ErrorCode,
        command: Sequence[str | Path],
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        cmd = tuple(str(c) for c in command)
        super().__init__(
            message=message,
            error_code=error_code,
            op="exec",
            context={"command": cmd, "command_str": _format_command(cmd), **_as_context(context)},
            cause=cause,
        )
        self.command = cmd


class ExecNonZeroError(ExecFailureError):
    """exec() returned a non-zero exit status."""

    exit_code: int
    stdout: bytes
    stderr: bytes

    def __init__(
        self,
        exec_result: ExecResult,
        *,
        command: Sequence[str | Path],
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        decoded_stdout = exec_result.stdout.decode("utf-8", errors="replace")
        decoded_stderr = exec_result.stderr.decode("utf-8", errors="replace")
        if decoded_stdout and decoded_stderr:
            message = f"stdout: {decoded_stdout}\nstderr: {decoded_stderr}"
        elif decoded_stdout:
            message = decoded_stdout
        elif decoded_stderr:
            message = decoded_stderr
        else:
            message = f"command exited with code {exec_result.exit_code}"
        super().__init__(
            message=message,
            error_code=ErrorCode.EXEC_NONZERO,
            command=command,
            context={
                "exit_code": exec_result.exit_code,
                "stdout": decoded_stdout,
                "stderr": decoded_stderr,
                **_as_context(context),
            },
            cause=cause,
        )
        self.exit_code = exec_result.exit_code
        self.stdout = exec_result.stdout
        self.stderr = exec_result.stderr


class ExecTimeoutError(ExecFailureError):
    """exec() exceeded its timeout."""

    timeout_s: float | None

    def __init__(
        self,
        *,
        command: Sequence[str | Path],
        timeout_s: float | None,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="command timed out",
            error_code=ErrorCode.EXEC_TIMEOUT,
            command=command,
            context={"timeout_s": timeout_s, **_as_context(context)},
            cause=cause,
        )
        self.timeout_s = timeout_s


class ExecTransportError(ExecFailureError):
    """exec() failed due to a transport-level error (e.g., Docker API)."""

    def __init__(
        self,
        *,
        command: Sequence[str | Path],
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="exec transport error",
            error_code=ErrorCode.EXEC_TRANSPORT_ERROR,
            command=command,
            context=_as_context(context),
            cause=cause,
        )


class PtySessionNotFoundError(SandboxRuntimeError):
    """PTY session lookup failed for a provided session id."""

    session_id: int

    def __init__(
        self,
        *,
        session_id: int,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"PTY session not found: {session_id}",
            error_code=ErrorCode.PTY_SESSION_NOT_FOUND,
            op="exec",
            context={"session_id": session_id, **_as_context(context)},
            cause=cause,
        )
        self.session_id = session_id


class WorkspaceIOError(SandboxRuntimeError):
    """Base class for workspace read/write errors."""


class ApplyPatchPathError(ApplyPatchError):
    """Apply patch path was invalid (absolute or escaped the workspace root)."""

    def __init__(
        self,
        *,
        path: str | Path,
        reason: Literal["absolute", "escape_root", "empty"],
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        if reason == "absolute":
            message = f"apply_patch path must be relative: {path}"
        elif reason == "escape_root":
            message = f"apply_patch path must not escape root: {path}"
        else:
            message = "apply_patch path must be non-empty"
        super().__init__(
            message=message,
            error_code=ErrorCode.APPLY_PATCH_INVALID_PATH,
            op="apply_patch",
            context={"path": str(path), "reason": reason, **_as_context(context)},
            cause=cause,
        )


class ApplyPatchDiffError(ApplyPatchError):
    """Apply patch diff was malformed or could not be applied."""

    def __init__(
        self,
        *,
        message: str,
        path: str | Path | None = None,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        resolved_context = _as_context(context)
        if path is not None:
            resolved_context["path"] = str(path)
        super().__init__(
            message=message,
            error_code=ErrorCode.APPLY_PATCH_INVALID_DIFF,
            op="apply_patch",
            context=resolved_context,
            cause=cause,
        )


class ApplyPatchFileNotFoundError(WorkspaceIOError):
    """Apply patch failed because a file was missing."""

    def __init__(
        self,
        *,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"apply_patch missing file: {path}",
            error_code=ErrorCode.APPLY_PATCH_FILE_NOT_FOUND,
            op="apply_patch",
            context={"path": str(path), **_as_context(context)},
            cause=cause,
        )


class ApplyPatchDecodeError(WorkspaceIOError):
    """Apply patch failed because a file could not be decoded as UTF-8."""

    def __init__(
        self,
        *,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"apply_patch could not decode file: {path}",
            error_code=ErrorCode.APPLY_PATCH_DECODE_ERROR,
            op="apply_patch",
            context={"path": str(path), **_as_context(context)},
            cause=cause,
        )


class WorkspaceReadNotFoundError(WorkspaceIOError):
    """Workspace read failed because the path does not exist."""

    def __init__(
        self,
        *,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"file not found: {path}",
            error_code=ErrorCode.WORKSPACE_READ_NOT_FOUND,
            op="read",
            context={"path": str(path), **_as_context(context)},
            cause=cause,
        )


class WorkspaceArchiveReadError(WorkspaceIOError):
    """Workspace read failed while reading or decoding the archive stream."""

    def __init__(
        self,
        *,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"failed to read archive for path: {path}",
            error_code=ErrorCode.WORKSPACE_ARCHIVE_READ_ERROR,
            op="read",
            context={"path": str(path), **_as_context(context)},
            cause=cause,
        )


class WorkspaceArchiveWriteError(WorkspaceIOError):
    """Workspace write failed while creating or sending the archive stream."""

    def __init__(
        self,
        *,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"failed to write archive for path: {path}",
            error_code=ErrorCode.WORKSPACE_ARCHIVE_WRITE_ERROR,
            op="write",
            context={"path": str(path), **_as_context(context)},
            cause=cause,
        )


class WorkspaceWriteTypeError(WorkspaceIOError):
    """Workspace write payload was not a binary file-like object."""

    def __init__(
        self,
        *,
        path: Path,
        actual_type: str,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="write() expects a binary file-like object",
            error_code=ErrorCode.WORKSPACE_WRITE_TYPE_ERROR,
            op="write",
            context={"path": str(path), "actual_type": actual_type, **_as_context(context)},
            cause=cause,
        )


class WorkspaceStopError(SandboxRuntimeError):
    """SandboxSession stop failed (typically during snapshot persistence)."""

    def __init__(
        self,
        *,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="failed to stop session",
            error_code=ErrorCode.WORKSPACE_STOP_ERROR,
            op="stop",
            context={"path": str(path), **_as_context(context)},
            cause=cause,
        )


class WorkspaceStartError(SandboxRuntimeError):
    """SandboxSession start failed (typically while ensuring the workspace root exists)."""

    def __init__(
        self,
        *,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="failed to start session",
            error_code=ErrorCode.WORKSPACE_START_ERROR,
            op="start",
            context={"path": str(path), **_as_context(context)},
            cause=cause,
        )


class WorkspaceRootNotFoundError(SandboxRuntimeError):
    """Workspace root is missing on disk (e.g. deleted mid-session)."""

    def __init__(
        self,
        *,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"workspace root not found: {path}",
            error_code=ErrorCode.WORKSPACE_ROOT_NOT_FOUND,
            op="exec",
            context={"path": str(path), **_as_context(context)},
            cause=cause,
        )


class LocalArtifactError(ArtifactError):
    """Base class for errors while reading local artifacts."""


class LocalFileReadError(LocalArtifactError):
    """Failed to read a local file artifact from disk."""

    def __init__(
        self,
        *,
        src: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"failed to read local file artifact: {src}",
            error_code=ErrorCode.LOCAL_FILE_READ_ERROR,
            op="materialize",
            context={"src": str(src), **_as_context(context)},
            cause=cause,
        )


class LocalDirReadError(LocalArtifactError):
    """Failed to read a local directory artifact from disk."""

    def __init__(
        self,
        *,
        src: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"failed to read local dir artifact: {src}",
            error_code=ErrorCode.LOCAL_DIR_READ_ERROR,
            op="materialize",
            context={"src": str(src), **_as_context(context)},
            cause=cause,
        )


class LocalChecksumError(LocalArtifactError):
    """Failed to compute a checksum for a local artifact."""

    def __init__(
        self,
        *,
        src: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"failed to checksum local artifact: {src}",
            error_code=ErrorCode.LOCAL_CHECKSUM_ERROR,
            op="materialize",
            context={"src": str(src), **_as_context(context)},
            cause=cause,
        )


class GitArtifactError(ArtifactError):
    """Base class for errors while materializing git_repo artifacts."""


class GitMissingInImageError(GitArtifactError):
    """Container image is missing git, so git_repo artifacts cannot be materialized."""

    def __init__(
        self,
        *,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="git is required in the container image to materialize git_repo artifacts",
            error_code=ErrorCode.GIT_MISSING_IN_IMAGE,
            op="materialize",
            context=_as_context(context),
            cause=cause,
        )


class GitCloneError(GitArtifactError):
    """Failed to clone a git repository while materializing an artifact."""

    def __init__(
        self,
        *,
        url: str,
        ref: str,
        stderr: str | None = None,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"git clone failed for {url}@{ref}",
            error_code=ErrorCode.GIT_CLONE_ERROR,
            op="materialize",
            context={"url": url, "ref": ref, "stderr": stderr, **_as_context(context)},
            cause=cause,
        )


class GitCopyError(GitArtifactError):
    """Failed to copy files from a cloned repo into the workspace."""

    def __init__(
        self,
        *,
        src_root: str,
        dest: Path,
        stderr: str | None = None,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="copy from git repo failed",
            error_code=ErrorCode.GIT_COPY_ERROR,
            op="materialize",
            context={
                "src_root": src_root,
                "dest": str(dest),
                "stderr": stderr,
                **_as_context(context),
            },
            cause=cause,
        )


class MountArtifactError(ArtifactError):
    """Base class for mount-related errors while materializing artifacts."""


class MountToolMissingError(MountArtifactError):
    """Required mount tool is missing in the sandbox."""

    def __init__(
        self,
        *,
        tool: str,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=f"required mount tool missing: {tool}",
            error_code=ErrorCode.MOUNT_MISSING_TOOL,
            op="materialize",
            context={"tool": tool, **_as_context(context)},
            cause=cause,
        )


class MountConfigError(MountArtifactError):
    """Mount configuration was invalid or incomplete."""

    def __init__(
        self,
        *,
        message: str,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            message=message,
            error_code=ErrorCode.MOUNT_CONFIG_INVALID,
            op="materialize",
            context=_as_context(context),
        )


class MountCommandError(MountArtifactError):
    """Mount command failed to execute successfully."""

    def __init__(
        self,
        *,
        command: str,
        stderr: str | None,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="mount command failed",
            error_code=ErrorCode.MOUNT_FAILED,
            op="materialize",
            context={"command": command, "stderr": stderr, **_as_context(context)},
            cause=cause,
        )


class SkillsConfigError(ConfigurationError):
    """Skills capability configuration was invalid."""

    def __init__(
        self,
        *,
        message: str,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message=message,
            error_code=ErrorCode.SKILLS_CONFIG_INVALID,
            op="materialize",
            context=_as_context(context),
            cause=cause,
        )


class SnapshotPersistError(SnapshotError):
    """Failed to persist snapshot bytes to durable storage."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="failed to persist snapshot",
            error_code=ErrorCode.SNAPSHOT_PERSIST_ERROR,
            op="snapshot_persist",
            context={"snapshot_id": snapshot_id, "path": str(path), **_as_context(context)},
            cause=cause,
        )


class SnapshotRestoreError(SnapshotError):
    """Failed to restore snapshot bytes from durable storage."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        path: Path,
        context: Mapping[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(
            message="failed to restore snapshot",
            error_code=ErrorCode.SNAPSHOT_RESTORE_ERROR,
            op="snapshot_restore",
            context={"snapshot_id": snapshot_id, "path": str(path), **_as_context(context)},
            cause=cause,
        )


class SnapshotNotRestorableError(SnapshotError):
    """Snapshot cannot be restored because the underlying storage is missing."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        path: Path,
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(
            message="snapshot is not restorable",
            error_code=ErrorCode.SNAPSHOT_NOT_RESTORABLE,
            op="snapshot_restore",
            context={"snapshot_id": snapshot_id, "path": str(path), **_as_context(context)},
        )
