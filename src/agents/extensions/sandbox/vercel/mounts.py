"""Create-time-only S3 mounts for Vercel sandboxes."""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Literal, NoReturn

from ....sandbox.entries import Mount, S3Mount
from ....sandbox.entries.mounts.base import MountStrategyBase
from ....sandbox.errors import MountCommandError, MountConfigError
from ....sandbox.materialization import MaterializedFile
from ....sandbox.session.base_sandbox_session import BaseSandboxSession
from ....sandbox.session.runtime_helpers import RESOLVE_WORKSPACE_PATH_HELPER
from ....sandbox.types import ExecResult
from ....sandbox.workspace_paths import sandbox_path_str
from .sandbox import VercelSandboxSession

_MOUNTPOINT_BINARY = "/usr/bin/mount-s3"
_MOUNTPOINT_PACKAGE = "mount-s3"
_MOUNTPOINT_SOURCE = "mountpoint-s3"
_MOUNTPOINT_MINIMUM_VERSION = (1, 21, 0)
_MOUNTPOINT_INSTALL_TIMEOUT_S = 300.0
_MOUNTPOINT_COMMAND_TIMEOUT_S = 120.0


def _require_vercel_session(session: BaseSandboxSession) -> VercelSandboxSession:
    if not isinstance(session, VercelSandboxSession):
        raise MountConfigError(
            message=(
                "Vercel S3 mount topology is fixed when the sandbox is created; "
                "dynamic manifest application is not supported"
            ),
            context={"backend": "vercel", "session_type": type(session).__name__},
        )
    return session


def _redact_sensitive_values(text: str, values: tuple[str, ...]) -> str:
    redacted = text
    for value in sorted({value for value in values if value}, key=len, reverse=True):
        redacted = redacted.replace(value, "REDACTED")
    return redacted


async def _run_vercel_command(
    session: VercelSandboxSession,
    command: str,
    args: list[str],
    *,
    sudo: bool = False,
    timeout: float = _MOUNTPOINT_COMMAND_TIMEOUT_S,
) -> ExecResult:
    command_text = shlex.join([command, *args])
    try:
        sandbox = await session._ensure_sandbox()

        async def run_and_collect_output() -> ExecResult:
            finished = await sandbox.run_command(
                command,
                args,
                sudo=sudo,
            )
            stdout = (await finished.stdout()).encode("utf-8")
            stderr = (await finished.stderr()).encode("utf-8")
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=finished.exit_code)

        return await asyncio.wait_for(run_and_collect_output(), timeout=timeout)
    except Exception as exc:
        raise MountCommandError(
            command=command_text,
            stderr=f"{type(exc).__name__}: {exc}",
            context={"backend": "vercel"},
            retryable=session._runtime_provider_retryability(exc),
        ) from None


def _raise_command_failure(
    command: str,
    args: list[str],
    result: ExecResult,
    *,
    context: dict[str, object] | None = None,
) -> NoReturn:
    raise MountCommandError(
        command=shlex.join([command, *args]),
        stderr=result.stderr.decode("utf-8", errors="replace"),
        context={
            "backend": "vercel",
            "exit_code": result.exit_code,
            **(context or {}),
        },
    )


async def _run_required_command(
    session: VercelSandboxSession,
    command: str,
    args: list[str],
    *,
    sudo: bool = False,
    timeout: float = _MOUNTPOINT_COMMAND_TIMEOUT_S,
    context: dict[str, object] | None = None,
) -> ExecResult:
    result = await _run_vercel_command(
        session,
        command,
        args,
        sudo=sudo,
        timeout=timeout,
    )
    if not result.ok():
        _raise_command_failure(command, args, result, context=context)
    return result


async def _run_credentialed_mount_command(
    session: VercelSandboxSession,
    mount_path: Path,
    args: list[str],
    *,
    context: dict[str, object],
) -> ExecResult | MountCommandError | asyncio.CancelledError:
    env = session._runtime_s3_mount_environment(mount_path)
    sensitive_values = tuple(env.values())
    command_text = shlex.join([_MOUNTPOINT_BINARY, *args])
    try:
        sandbox = await session._ensure_sandbox()

        async def run_and_collect_output() -> ExecResult:
            finished = await sandbox.run_command(
                _MOUNTPOINT_BINARY,
                args,
                env=env,
                sudo=True,
            )
            stdout = (await finished.stdout()).encode("utf-8")
            stderr = (await finished.stderr()).encode("utf-8")
            return ExecResult(stdout=stdout, stderr=stderr, exit_code=finished.exit_code)

        result = await asyncio.wait_for(
            run_and_collect_output(),
            timeout=_MOUNTPOINT_COMMAND_TIMEOUT_S,
        )
    except (Exception, asyncio.CancelledError) as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        retryable = session._runtime_provider_retryability(exc)
        failure_message = _redact_sensitive_values(
            f"{type(exc).__name__}: {exc}",
            sensitive_values,
        )
        exc.__traceback__ = None
        exc.__context__ = None
        exc.__cause__ = None
        if cancelled:
            return asyncio.CancelledError()
        return MountCommandError(
            command=command_text,
            stderr=failure_message,
            context={"backend": "vercel", **context},
            retryable=retryable,
        )

    if result.ok():
        return result

    failure_message = _redact_sensitive_values(
        result.stderr.decode("utf-8", errors="replace"),
        sensitive_values,
    )
    return MountCommandError(
        command=command_text,
        stderr=failure_message,
        context={
            "backend": "vercel",
            "exit_code": result.exit_code,
            **context,
        },
    )


def _parse_mountpoint_version(raw: str) -> tuple[int, int, int] | None:
    parts = raw.strip().split(".")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


async def _ensure_mountpoint(session: VercelSandboxSession) -> None:
    version_args = ["--query", "--queryformat", "%{VERSION}", _MOUNTPOINT_PACKAGE]
    version_result = await _run_vercel_command(
        session,
        "/usr/bin/rpm",
        version_args,
    )
    version_text = version_result.stdout.decode("utf-8", errors="replace").strip()
    version = _parse_mountpoint_version(version_text) if version_result.ok() else None
    binary_check = await _run_vercel_command(
        session,
        "/usr/bin/test",
        ["-x", _MOUNTPOINT_BINARY],
    )
    supported = (
        version is not None
        and version[0] == _MOUNTPOINT_MINIMUM_VERSION[0]
        and version >= _MOUNTPOINT_MINIMUM_VERSION
    )
    if not binary_check.ok() or not supported:
        await _run_required_command(
            session,
            "/usr/bin/dnf",
            [
                "install",
                "-y",
                "--setopt=gpgcheck=1",
                "fuse",
                _MOUNTPOINT_PACKAGE,
            ],
            sudo=True,
            timeout=_MOUNTPOINT_INSTALL_TIMEOUT_S,
            context={"package": _MOUNTPOINT_PACKAGE},
        )
        await _run_required_command(
            session,
            "/usr/bin/test",
            ["-x", _MOUNTPOINT_BINARY],
            context={"package": _MOUNTPOINT_PACKAGE},
        )
        version_result = await _run_required_command(
            session,
            "/usr/bin/rpm",
            version_args,
            context={"package": _MOUNTPOINT_PACKAGE},
        )
        version_text = version_result.stdout.decode("utf-8", errors="replace").strip()
        version = _parse_mountpoint_version(version_text)
        supported = (
            version is not None
            and version[0] == _MOUNTPOINT_MINIMUM_VERSION[0]
            and version >= _MOUNTPOINT_MINIMUM_VERSION
        )

    if not supported:
        raise MountConfigError(
            message="unsupported Mountpoint for Amazon S3 version",
            context={
                "backend": "vercel",
                "actual_version": version_text,
                "minimum_version": ".".join(map(str, _MOUNTPOINT_MINIMUM_VERSION)),
            },
        )


def _validate_s3_mount(mount: Mount) -> S3Mount:
    if not isinstance(mount, S3Mount):
        raise MountConfigError(
            message="VercelCloudBucketMountStrategy only supports S3Mount",
            context={"backend": "vercel", "mount_type": mount.type},
        )
    if not mount.ephemeral:
        raise MountConfigError(
            message="Vercel S3 mounts must be ephemeral",
            context={"backend": "vercel", "mount_type": mount.type},
        )
    if (mount.access_key_id is None) != (mount.secret_access_key is None):
        raise MountConfigError(
            message="Vercel S3 mounts require both access_key_id and secret_access_key",
            context={"backend": "vercel", "mount_type": mount.type},
        )
    if mount.session_token is not None and mount.access_key_id is None:
        raise MountConfigError(
            message=(
                "Vercel S3 mounts require access_key_id and secret_access_key "
                "when session_token is provided"
            ),
            context={"backend": "vercel", "mount_type": mount.type},
        )
    for name, value in (
        ("access_key_id", mount.access_key_id),
        ("secret_access_key", mount.secret_access_key),
        ("session_token", mount.session_token),
    ):
        if value is not None and not value.strip():
            raise MountConfigError(
                message=f"Vercel S3 mount {name} must not be blank",
                context={"backend": "vercel", "mount_type": mount.type},
            )
    return mount


async def _command_user_ids(session: VercelSandboxSession) -> tuple[str, str]:
    uid_result = await _run_required_command(session, "/usr/bin/id", ["-u"])
    gid_result = await _run_required_command(session, "/usr/bin/id", ["-g"])
    uid = uid_result.stdout.decode("utf-8", errors="replace").strip()
    gid = gid_result.stdout.decode("utf-8", errors="replace").strip()
    if not uid.isdecimal() or not gid.isdecimal():
        raise MountCommandError(
            command="/usr/bin/id",
            stderr="Vercel returned a non-numeric user or group ID",
            context={"backend": "vercel"},
        )
    return uid, gid


def _mount_args(
    mount: S3Mount,
    mount_path: Path,
    *,
    authenticated: bool,
    user_ids: tuple[str, str] | None,
) -> list[str]:
    args = [mount.bucket, sandbox_path_str(mount_path), "--allow-other"]
    if not authenticated:
        args.append("--no-sign-request")
    if mount.read_only:
        args.append("--read-only")
    else:
        args.extend(["--allow-overwrite", "--allow-delete"])
        if user_ids is not None:
            uid, gid = user_ids
            args.extend(["--uid", uid, "--gid", gid])
    if mount.region is not None:
        args.extend(["--region", mount.region])
    if mount.endpoint_url is not None:
        args.extend(["--endpoint-url", mount.endpoint_url])
    if mount.prefix:
        prefix = mount.prefix if mount.prefix.endswith("/") else f"{mount.prefix}/"
        args.extend(["--prefix", prefix])
    return args


async def _assert_empty_mount_directory(
    session: VercelSandboxSession,
    mount_path: Path,
) -> None:
    mount_path_text = sandbox_path_str(mount_path)
    result = await _run_required_command(
        session,
        "/usr/bin/find",
        [mount_path_text, "-mindepth", "1", "-maxdepth", "1", "-print", "-quit"],
        context={"mount_path": mount_path_text},
    )
    if result.stdout.strip():
        raise MountConfigError(
            message="Vercel S3 mounts require an empty mount directory",
            context={"backend": "vercel", "mount_path": mount_path_text},
        )


async def _assert_canonical_mount_path(
    session: VercelSandboxSession,
    mount_path: Path,
) -> None:
    mount_path_text = sandbox_path_str(mount_path)
    helper_path = await session._ensure_runtime_helper_installed(RESOLVE_WORKSPACE_PATH_HELPER)
    root_path_text = sandbox_path_str(session._workspace_root_path())
    result = await _run_required_command(
        session,
        str(helper_path),
        [root_path_text, mount_path_text, "1"],
        context={"mount_path": mount_path_text},
    )
    resolved_path_text = result.stdout.decode("utf-8", errors="replace").strip()
    if resolved_path_text != mount_path_text:
        raise MountConfigError(
            message="Vercel S3 mount paths must not resolve through symlinks",
            context={
                "backend": "vercel",
                "mount_path": mount_path_text,
                "resolved_path": resolved_path_text,
            },
        )


async def _mount_s3(
    mount: S3Mount,
    session: VercelSandboxSession,
    mount_path: Path,
) -> None:
    normalized_path = await session._validate_path_access(mount_path, for_write=True)
    await _assert_canonical_mount_path(session, normalized_path)
    mount_path_text = sandbox_path_str(normalized_path)
    await _ensure_mountpoint(session)
    await _run_required_command(
        session,
        "/usr/bin/mkdir",
        ["-p", "--", mount_path_text],
        context={"mount_path": mount_path_text},
    )
    await _assert_empty_mount_directory(session, normalized_path)
    user_ids = await _command_user_ids(session) if not mount.read_only else None
    await _assert_canonical_mount_path(session, normalized_path)
    outcome = await _run_credentialed_mount_command(
        session,
        normalized_path,
        _mount_args(
            mount,
            normalized_path,
            authenticated=session._runtime_s3_mount_is_authenticated(normalized_path),
            user_ids=user_ids,
        ),
        context={"bucket": mount.bucket, "mount_path": mount_path_text},
    )
    if isinstance(outcome, BaseException):
        raise outcome from None


async def _is_mounted(session: VercelSandboxSession, mount_path: Path) -> bool:
    mount_path_text = sandbox_path_str(mount_path)
    args = ["--noheadings", "--output", "SOURCE", "--mountpoint", mount_path_text]
    result = await _run_vercel_command(session, "/usr/bin/findmnt", args)
    if result.exit_code == 1:
        return False
    if not result.ok():
        _raise_command_failure(
            "/usr/bin/findmnt",
            args,
            result,
            context={"mount_path": mount_path_text},
        )
    source = result.stdout.decode("utf-8", errors="replace").strip()
    if source != _MOUNTPOINT_SOURCE:
        raise MountConfigError(
            message="refusing to manage an unexpected filesystem at the Vercel S3 mount path",
            context={
                "backend": "vercel",
                "mount_path": mount_path_text,
                "expected_source": _MOUNTPOINT_SOURCE,
                "actual_source": source,
            },
        )
    return True


async def _unmount_s3(session: VercelSandboxSession, mount_path: Path) -> None:
    if not await _is_mounted(session, mount_path):
        raise MountConfigError(
            message="tracked Vercel S3 mount is missing from its configured path",
            context={
                "backend": "vercel",
                "mount_path": sandbox_path_str(mount_path),
            },
        )

    mount_path_text = sandbox_path_str(mount_path)
    args = [mount_path_text]
    result = await _run_vercel_command(
        session,
        "/usr/bin/umount",
        args,
        sudo=True,
    )
    if result.ok():
        return
    if not await _is_mounted(session, mount_path):
        # A mount can move with a renamed ancestor, so disappearance after umount is ambiguous.
        raise MountConfigError(
            message="Vercel S3 mount state became ambiguous during unmount",
            context={
                "backend": "vercel",
                "mount_path": mount_path_text,
            },
        )
    _raise_command_failure(
        "/usr/bin/umount",
        args,
        result,
        context={"mount_path": mount_path_text},
    )


class VercelCloudBucketMountStrategy(MountStrategyBase):
    """Select Vercel's create-time-only application of the remote mount policy.

    This strategy does not imply dynamic mount mutation, credential refresh, or resumable mounts.
    Those exclusions keep the provider lifecycle auditable.
    """

    type: Literal["vercel_cloud_bucket"] = "vercel_cloud_bucket"

    def validate_mount(self, mount: Mount) -> None:
        _validate_s3_mount(mount)

    def supports_native_snapshot_detach(self, mount: Mount) -> bool:
        _ = mount
        return False

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _ = base_dir
        vercel_session = _require_vercel_session(session)
        async with vercel_session._s3_mount_operation(force_lock=True):
            if not vercel_session._runtime_s3_mount_activation_allowed():
                raise MountConfigError(
                    message=(
                        "Vercel S3 mount topology is fixed when the sandbox is created; "
                        "dynamic manifest application is not supported"
                    ),
                    context={"backend": "vercel"},
                )
            declared_mount = _validate_s3_mount(mount)
            mount_path = declared_mount._resolve_mount_path(vercel_session, dest)
            s3_mount = vercel_session._runtime_trusted_s3_mount(mount_path)
            try:
                await _mount_s3(s3_mount, vercel_session, mount_path)
            except (Exception, asyncio.CancelledError) as exc:
                await vercel_session._runtime_fail_s3_mount_transition(exc)
                raise
            vercel_session._runtime_record_s3_mount_active(mount_path)
            return []

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _ = base_dir
        vercel_session = _require_vercel_session(session)
        declared_mount = _validate_s3_mount(mount)
        mount_path = declared_mount._resolve_mount_path(vercel_session, dest)
        if not vercel_session._runtime_s3_mount_is_active(mount_path):
            return
        try:
            await _unmount_s3(vercel_session, mount_path)
        except (Exception, asyncio.CancelledError) as exc:
            await vercel_session._runtime_fail_s3_mount_transition(exc)
            raise
        vercel_session._runtime_record_s3_mount_inactive(mount_path)

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _validate_s3_mount(mount)
        vercel_session = _require_vercel_session(session)
        if not vercel_session._runtime_s3_mount_is_active(path):
            return
        try:
            await _unmount_s3(vercel_session, path)
        except (Exception, asyncio.CancelledError) as exc:
            await vercel_session._runtime_fail_s3_mount_transition(exc)
            raise
        vercel_session._runtime_record_s3_mount_detached(path)

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _validate_s3_mount(mount)
        vercel_session = _require_vercel_session(session)
        if not vercel_session._runtime_s3_mount_is_detached(path):
            return
        s3_mount = vercel_session._runtime_trusted_s3_mount(path)
        try:
            await _mount_s3(s3_mount, vercel_session, path)
        except (Exception, asyncio.CancelledError) as exc:
            await vercel_session._runtime_fail_s3_mount_transition(exc)
            raise
        vercel_session._runtime_record_s3_mount_restored(path)

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        _ = mount
        return None


__all__ = [
    "VercelCloudBucketMountStrategy",
]
