from __future__ import annotations

import abc
import io
import re
import shlex
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, TypeVar

from pydantic import BaseModel, Field

from ...errors import (
    MountCommandError,
    MountConfigError,
    MountToolMissingError,
    WorkspaceReadNotFoundError,
)
from ...workspace_paths import (
    coerce_posix_path,
    posix_path_as_path,
    sandbox_path_str,
    windows_absolute_path,
)

if TYPE_CHECKING:
    from ...session.base_sandbox_session import BaseSandboxSession


@dataclass(frozen=True)
class FuseMountConfig:
    account: str
    container: str
    endpoint: str | None
    identity_client_id: str | None
    account_key: str | None
    mount_type: str
    read_only: bool = True


@dataclass(frozen=True)
class MountpointMountConfig:
    bucket: str
    access_key_id: str | None
    secret_access_key: str | None
    session_token: str | None
    prefix: str | None
    region: str | None
    endpoint_url: str | None
    mount_type: str
    read_only: bool = True


@dataclass(frozen=True)
class RcloneMountConfig:
    remote_name: str
    remote_path: str
    remote_kind: str
    mount_type: str
    config_text: str | None = None
    read_only: bool = True


@dataclass(frozen=True)
class S3FilesMountConfig:
    file_system_id: str
    subpath: str | None
    mount_target_ip: str | None
    access_point: str | None
    region: str | None
    extra_options: dict[str, str | None]
    mount_type: str
    read_only: bool = True


MountPatternConfig = (
    FuseMountConfig | MountpointMountConfig | RcloneMountConfig | S3FilesMountConfig
)
MountPatternConfigT = TypeVar("MountPatternConfigT", bound=MountPatternConfig)


def _require_mount_config(
    config: MountPatternConfig,
    expected_type: type[MountPatternConfigT],
) -> MountPatternConfigT:
    if not isinstance(config, expected_type):
        raise MountConfigError(
            message="mount pattern received incompatible runtime config",
            context={
                "expected": expected_type.__name__,
                "actual": type(config).__name__,
            },
        )
    return config


async def _write_sensitive_config_file(
    session: BaseSandboxSession,
    path: Path,
    payload: bytes,
) -> None:
    """Write generated mount credentials/config with owner-only permissions."""

    await session.write(path, io.BytesIO(payload))
    await session._exec_checked_nonzero(
        "chmod", "0600", sandbox_path_str(session.normalize_path(path))
    )


class MountPatternBase(BaseModel, abc.ABC):
    @abc.abstractmethod
    async def apply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def unapply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        raise NotImplementedError


class FuseMountPattern(MountPatternBase):
    type: Literal["fuse"] = "fuse"
    allow_other: bool = Field(default=True)
    log_type: str = Field(default="syslog")
    log_level: str = Field(default="log_debug")
    cache_type: Literal["block_cache", "file_cache"] = Field(default="block_cache")
    cache_path: Path | None = None
    cache_size_mb: int | None = None
    block_cache_block_size_mb: int = Field(default=16)
    block_cache_disk_timeout_sec: int = Field(default=3600)
    file_cache_timeout_sec: int = Field(default=120)
    file_cache_max_size_mb: int | None = None
    attr_cache_timeout_sec: int | None = None
    entry_cache_timeout_sec: int | None = None
    negative_entry_cache_timeout_sec: int | None = None

    def model_post_init(self, __context: object, /) -> None:
        if self.cache_path is None:
            return
        if (windows_path := windows_absolute_path(self.cache_path)) is not None:
            raise MountConfigError(
                message="blobfuse cache_path must be relative to the workspace root",
                context={"cache_path": windows_path.as_posix()},
            )
        cache_path = coerce_posix_path(self.cache_path)
        if cache_path.is_absolute() or ".." in cache_path.parts:
            raise MountConfigError(
                message="blobfuse cache_path must be relative to the workspace root",
                context={"cache_path": cache_path.as_posix()},
            )

    @dataclass(frozen=True)
    class BlobfuseConfig:
        account: str
        container: str
        endpoint: str
        cache_type: str
        cache_size_mb: int
        block_cache_block_size_mb: int
        block_cache_disk_timeout_sec: int
        file_cache_timeout_sec: int
        file_cache_max_size_mb: int
        cache_dir: Path
        allow_other: bool
        log_type: str
        log_level: str
        entry_cache_timeout_sec: int | None
        negative_entry_cache_timeout_sec: int | None
        attr_cache_timeout_sec: int | None
        identity_client_id: str | None
        account_key: str | None

        def to_text(self) -> str:
            lines: list[str] = []
            if self.allow_other:
                lines.append("allow-other: true")
                lines.append("")
            lines.extend(
                [
                    "logging:",
                    f"  type: {self.log_type}",
                    f"  level: {self.log_level}",
                    "",
                    "components:",
                    "  - libfuse",
                    f"  - {self.cache_type}",
                    "  - attr_cache",
                    "  - azstorage",
                    "",
                ]
            )

            libfuse_lines: list[str] = []
            if self.entry_cache_timeout_sec is not None:
                libfuse_lines.append(f"  entry-expiration-sec: {self.entry_cache_timeout_sec}")
            if self.negative_entry_cache_timeout_sec is not None:
                libfuse_lines.append(
                    f"  negative-entry-expiration-sec: {self.negative_entry_cache_timeout_sec}"
                )
            if libfuse_lines:
                lines.append("libfuse:")
                lines.extend(libfuse_lines)
                lines.append("")

            if self.cache_type == "block_cache":
                lines.extend(
                    [
                        "block_cache:",
                        f"  block-size-mb: {self.block_cache_block_size_mb}",
                        f"  mem-size-mb: {self.cache_size_mb}",
                        f"  path: {sandbox_path_str(self.cache_dir)}",
                        f"  disk-size-mb: {self.cache_size_mb}",
                        f"  disk-timeout-sec: {self.block_cache_disk_timeout_sec}",
                        "",
                    ]
                )
            else:
                lines.extend(
                    [
                        "file_cache:",
                        f"  path: {sandbox_path_str(self.cache_dir)}",
                        f"  timeout-sec: {self.file_cache_timeout_sec}",
                        f"  max-size-mb: {self.file_cache_max_size_mb}",
                        "",
                    ]
                )

            attr_cache_timeout = self.attr_cache_timeout_sec or 7200
            lines.extend(
                [
                    "attr_cache:",
                    f"  timeout-sec: {attr_cache_timeout}",
                    "",
                    "azstorage:",
                    "  type: block",
                    f"  account-name: {self.account}",
                    f"  container: {self.container}",
                    f"  endpoint: {self.endpoint}",
                ]
            )
            if self.account_key:
                lines.extend(
                    [
                        "  auth-type: key",
                        f"  account-key: {self.account_key}",
                    ]
                )
            else:
                lines.append("  mode: msi")
            if self.identity_client_id:
                lines.append(f"  identity-client-id: {self.identity_client_id}")
            lines.append("")
            return "\n".join(lines)

    async def apply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        fuse_config = _require_mount_config(config, FuseMountConfig)
        account = fuse_config.account
        container = fuse_config.container

        tool_check = await session.exec("command -v blobfuse2 >/dev/null 2>&1")
        if not tool_check.ok():
            raise MountToolMissingError(
                tool="blobfuse2",
                context={"account": account, "container": container},
            )

        session_id = getattr(session.state, "session_id", None)
        if session_id is None:
            raise MountConfigError(
                message="mount session is missing session_id",
                context={"type": fuse_config.mount_type},
            )

        mount_path = path
        cache_dir = (
            posix_path_as_path(coerce_posix_path(self.cache_path))
            if self.cache_path is not None
            # Keep mount scratch state inside the workspace so session helpers can create/write it
            # through the normal workspace-scoped API.
            else posix_path_as_path(
                coerce_posix_path(f".sandbox-blobfuse-cache/{session_id.hex}/{account}/{container}")
            )
        )
        config_dir = posix_path_as_path(
            coerce_posix_path(f".sandbox-blobfuse-config/{session_id.hex}")
        )
        config_name = f"{account}_{container}".replace("/", "_")
        config_path = config_dir / f"{config_name}.yaml"
        command_mount_path = session.normalize_path(mount_path)
        command_cache_dir = session.normalize_path(cache_dir)
        if command_cache_dir == command_mount_path or command_cache_dir.is_relative_to(
            command_mount_path
        ):
            raise MountConfigError(
                message="blobfuse cache_path must be outside the mount path",
                context={
                    "mount_path": sandbox_path_str(command_mount_path),
                    "cache_path": sandbox_path_str(command_cache_dir),
                },
            )

        await session.mkdir(mount_path, parents=True)
        await session.mkdir(cache_dir, parents=True)
        await session.mkdir(config_dir, parents=True)
        session.register_persist_workspace_skip_path(cache_dir)
        session.register_persist_workspace_skip_path(config_dir)
        command_config_path = session.normalize_path(config_path)

        endpoint = fuse_config.endpoint or f"https://{account}.blob.core.windows.net"
        cache_type = self.cache_type
        cache_size_mb = self.cache_size_mb or (50_000 if cache_type == "block_cache" else 4_096)
        file_cache_max_size_mb = self.file_cache_max_size_mb or cache_size_mb
        blobfuse_config = self.BlobfuseConfig(
            account=account,
            container=container,
            endpoint=endpoint,
            cache_type=cache_type,
            cache_size_mb=cache_size_mb,
            block_cache_block_size_mb=self.block_cache_block_size_mb,
            block_cache_disk_timeout_sec=self.block_cache_disk_timeout_sec,
            file_cache_timeout_sec=self.file_cache_timeout_sec,
            file_cache_max_size_mb=file_cache_max_size_mb,
            cache_dir=command_cache_dir,
            allow_other=self.allow_other,
            log_type=self.log_type,
            log_level=self.log_level,
            entry_cache_timeout_sec=self.entry_cache_timeout_sec,
            negative_entry_cache_timeout_sec=self.negative_entry_cache_timeout_sec,
            attr_cache_timeout_sec=self.attr_cache_timeout_sec,
            identity_client_id=fuse_config.identity_client_id,
            account_key=fuse_config.account_key,
        )
        config_payload = blobfuse_config.to_text().encode("utf-8")
        await _write_sensitive_config_file(session, config_path, config_payload)

        cmd: list[str] = ["blobfuse2", "mount"]
        if fuse_config.read_only:
            cmd.append("--read-only")
        cmd.extend(["--config-file", sandbox_path_str(command_config_path)])
        cmd.append(sandbox_path_str(mount_path))

        result = await session.exec(*cmd, shell=False)
        if not result.ok():
            raise MountCommandError(
                command=" ".join(cmd),
                stderr=result.stderr.decode("utf-8", errors="replace"),
                context={"account": account, "container": container},
            )

    async def unapply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        _ = _require_mount_config(config, FuseMountConfig)
        # Best-effort unmount; ignore failures for already-unmounted mounts.
        await session.exec(
            "sh",
            "-lc",
            f"fusermount3 -u {shlex.quote(sandbox_path_str(path))} || "
            f"umount {shlex.quote(sandbox_path_str(path))}",
            shell=False,
        )


class MountpointMountPattern(MountPatternBase):
    type: Literal["mountpoint"] = "mountpoint"

    @dataclass(frozen=True)
    class MountpointOptions:
        prefix: str | None = None
        region: str | None = None
        endpoint_url: str | None = None

    options: MountpointOptions = Field(default_factory=MountpointOptions)

    async def apply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        mountpoint_config = _require_mount_config(config, MountpointMountConfig)
        bucket = mountpoint_config.bucket

        tool_check = await session.exec("command -v mount-s3 >/dev/null 2>&1")
        if not tool_check.ok():
            raise MountToolMissingError(
                tool="mount-s3",
                context={"bucket": bucket},
            )

        await session.mkdir(path, parents=True)

        cmd: list[str] = ["mount-s3"]
        if mountpoint_config.read_only:
            cmd.append("--read-only")
        elif mountpoint_config.mount_type in {"s3_mount", "gcs_mount"}:
            cmd.extend(["--allow-overwrite", "--allow-delete"])

        if mountpoint_config.region:
            cmd.extend(["--region", mountpoint_config.region])
        if mountpoint_config.endpoint_url:
            cmd.extend(["--endpoint-url", mountpoint_config.endpoint_url])
        if mountpoint_config.mount_type == "gcs_mount":
            # GCS XML API rejects the default upload checksum flow used by mount-s3.
            cmd.extend(["--upload-checksums", "off"])
        if mountpoint_config.prefix:
            cmd.extend(["--prefix", mountpoint_config.prefix])
        cmd.extend([bucket, sandbox_path_str(path)])

        env_parts: list[str] = []
        access_key_id = mountpoint_config.access_key_id
        secret_access_key = mountpoint_config.secret_access_key
        session_token = mountpoint_config.session_token
        if access_key_id and secret_access_key:
            env_parts.append(f"AWS_ACCESS_KEY_ID={shlex.quote(access_key_id)}")
            env_parts.append(f"AWS_SECRET_ACCESS_KEY={shlex.quote(secret_access_key)}")
            if session_token:
                env_parts.append(f"AWS_SESSION_TOKEN={shlex.quote(session_token)}")

        joined_cmd = " ".join(shlex.quote(part) for part in cmd)
        if env_parts:
            joined_cmd = f"{' '.join(env_parts)} {joined_cmd}"

        result = await session.exec("sh", "-lc", joined_cmd, shell=False)
        if not result.ok():
            raise MountCommandError(
                command=joined_cmd,
                stderr=result.stderr.decode("utf-8", errors="replace"),
                context={"bucket": bucket},
            )

    async def unapply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        _ = _require_mount_config(config, MountpointMountConfig)
        await session.exec(
            "sh",
            "-lc",
            f"fusermount3 -u {shlex.quote(sandbox_path_str(path))} || "
            f"umount {shlex.quote(sandbox_path_str(path))}",
            shell=False,
        )


class S3FilesMountPattern(MountPatternBase):
    type: Literal["s3files"] = "s3files"

    @dataclass(frozen=True)
    class S3FilesOptions:
        mount_target_ip: str | None = None
        access_point: str | None = None
        region: str | None = None
        extra_options: dict[str, str | None] = field(default_factory=dict)

    options: S3FilesOptions = Field(default_factory=S3FilesOptions)

    async def apply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        s3files_config = _require_mount_config(config, S3FilesMountConfig)

        tool_check = await session.exec("command -v mount.s3files >/dev/null 2>&1")
        if not tool_check.ok():
            raise MountToolMissingError(
                tool="mount.s3files",
                context={"file_system_id": s3files_config.file_system_id},
            )

        await session.mkdir(path, parents=True)

        device = s3files_config.file_system_id
        if s3files_config.subpath:
            device = f"{device}:{s3files_config.subpath}"

        options: dict[str, str | None] = dict(s3files_config.extra_options)
        if s3files_config.read_only:
            options["ro"] = None
        if s3files_config.mount_target_ip:
            options["mounttargetip"] = s3files_config.mount_target_ip
        if s3files_config.access_point:
            options["accesspoint"] = s3files_config.access_point
        if s3files_config.region:
            options["region"] = s3files_config.region

        cmd: list[str] = ["mount", "-t", "s3files"]
        if options:
            rendered_options = ",".join(
                key if value is None else f"{key}={value}" for key, value in options.items()
            )
            cmd.extend(["-o", rendered_options])
        cmd.extend([device, sandbox_path_str(path)])

        result = await session.exec(*cmd, shell=False)
        if not result.ok():
            raise MountCommandError(
                command=" ".join(shlex.quote(part) for part in cmd),
                stderr=result.stderr.decode("utf-8", errors="replace"),
                context={"file_system_id": s3files_config.file_system_id},
            )

    async def unapply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        _ = _require_mount_config(config, S3FilesMountConfig)
        await session.exec(
            "sh",
            "-lc",
            f"umount {shlex.quote(sandbox_path_str(path))} || true",
            shell=False,
        )


def _supplement_rclone_config_text(
    *,
    config_text: str,
    remote_name: str,
    required_lines: list[str],
    mount_type: str | None,
) -> str:
    section_pattern = re.compile(rf"^\s*\[{re.escape(remote_name)}\]\s*$", re.MULTILINE)
    match = section_pattern.search(config_text)
    if not match:
        raise MountConfigError(
            message="rclone config missing required remote section",
            context={"type": mount_type or "mount", "remote_name": remote_name},
        )

    section_start = match.start()
    section_end = match.end()
    next_section = re.search(r"^\s*\[.+\]\s*$", config_text[section_end:], re.MULTILINE)
    if next_section:
        section_body_end = section_end + next_section.start()
    else:
        section_body_end = len(config_text)

    before = config_text[:section_start]
    section_body = config_text[section_start:section_body_end].rstrip("\n")
    after = config_text[section_body_end:]

    supplement = "\n".join(required_lines[1:])  # header already present
    merged_section = f"{section_body}\n{supplement}\n"
    return f"{before}{merged_section}{after}"


class RcloneMountPattern(MountPatternBase):
    type: Literal["rclone"] = "rclone"
    mode: Literal["fuse", "nfs"] = Field(default="fuse")
    remote_name: str | None = None
    extra_args: list[str] = Field(default_factory=list)
    nfs_addr: str | None = None
    nfs_mount_options: list[str] | None = None
    config_file_path: Path | None = None

    def resolve_remote_name(
        self,
        *,
        session_id: str,
        remote_kind: str,
        mount_type: str | None = None,
    ) -> str:
        if self.remote_name:
            return self.remote_name
        if not remote_kind:
            raise MountConfigError(
                message="rclone mount requires remote_kind",
                context={"type": mount_type or "mount"},
            )
        # Derive a deterministic per-session remote name when the caller did not pin one, so
        # multiple mounts can coexist without sharing mutable rclone config sections.
        return f"sandbox_{remote_kind}_{session_id}"

    def _resolve_config_path(
        self,
        session: BaseSandboxSession,
        config_path: Path,
    ) -> Path:
        manifest_root = posix_path_as_path(
            coerce_posix_path(getattr(session.state.manifest, "root", "/"))
        )
        if config_path.is_absolute():
            return config_path
        # Relative config paths are resolved inside the sandbox workspace, not relative to the
        # host process that is orchestrating the session.
        return manifest_root / config_path

    async def read_config_text(
        self,
        session: BaseSandboxSession,
        remote_name: str,
        *,
        mount_type: str | None,
    ) -> str:
        if self.config_file_path is None:
            raise MountConfigError(
                message="rclone config_file_path is not set",
                context={"type": mount_type or "mount"},
            )
        config_path = self._resolve_config_path(session, self.config_file_path)
        try:
            handle = await session.read(config_path)
        except WorkspaceReadNotFoundError:
            raise
        except FileNotFoundError as e:
            raise WorkspaceReadNotFoundError(path=config_path, cause=e) from e
        except Exception as e:
            raise MountConfigError(
                message="failed to read rclone config file",
                context={"type": mount_type or "mount", "path": sandbox_path_str(config_path)},
            ) from e

        try:
            raw_config = handle.read()
        finally:
            handle.close()
        if isinstance(raw_config, bytes):
            config_text = raw_config.decode("utf-8", errors="replace")
        elif isinstance(raw_config, str):
            config_text = raw_config
        else:
            config_text = str(raw_config)

        if not config_text.strip():
            raise MountConfigError(
                message="rclone config file is empty",
                context={"type": mount_type or "mount", "path": sandbox_path_str(config_path)},
            )

        section_pattern = rf"^\s*\[{re.escape(remote_name)}\]\s*$"
        if not re.search(section_pattern, config_text, re.MULTILINE):
            raise MountConfigError(
                message="rclone config missing required remote section",
                context={
                    "type": mount_type or "mount",
                    "path": sandbox_path_str(config_path),
                    "remote_name": remote_name,
                },
            )

        return config_text

    async def _start_rclone_server(
        self,
        session: BaseSandboxSession,
        *,
        config: RcloneMountConfig,
        config_path: Path,
        nfs_addr: str,
    ) -> None:
        nfs_check = await session.exec(
            "sh",
            "-lc",
            "/usr/local/bin/rclone serve nfs --help >/dev/null 2>&1"
            " || rclone serve nfs --help >/dev/null 2>&1",
            shell=False,
        )
        if not nfs_check.ok():
            raise MountToolMissingError(
                tool="rclone serve nfs",
                context={"type": config.mount_type},
            )
        cmd: list[str] = ["rclone", "serve", "nfs", f"{config.remote_name}:{config.remote_path}"]
        cmd.extend(["--addr", nfs_addr])
        cmd.extend(["--config", sandbox_path_str(config_path)])
        if config.read_only:
            cmd.append("--read-only")
        if self.extra_args:
            cmd.extend(self.extra_args)
        joined_cmd = " ".join(shlex.quote(part) for part in cmd)
        # Run in background so we can wait for the server to start.
        server_cmd = f"{joined_cmd} &"
        result = await session.exec("sh", "-lc", server_cmd, shell=False)
        if not result.ok():
            raise MountCommandError(
                command=" ".join(cmd),
                stderr=result.stderr.decode("utf-8", errors="replace"),
                context={"type": config.mount_type},
            )

    async def _start_rclone_client(
        self,
        session: BaseSandboxSession,
        *,
        path: Path,
        config: RcloneMountConfig,
        config_path: Path,
        nfs_addr: str | None = None,
    ) -> None:
        if self.mode == "fuse":
            cmd: list[str] = [
                "rclone",
                "mount",
                f"{config.remote_name}:{config.remote_path}",
                sandbox_path_str(path),
            ]
            if config.read_only:
                cmd.append("--read-only")
            cmd.extend(["--config", sandbox_path_str(config_path), "--daemon"])
            if self.extra_args:
                cmd.extend(self.extra_args)
            result = await session.exec(*cmd, shell=False)
            if not result.ok():
                raise MountCommandError(
                    command=" ".join(cmd),
                    stderr=result.stderr.decode("utf-8", errors="replace"),
                    context={"type": config.mount_type},
                )
            return

        if nfs_addr is None:
            raise MountConfigError(
                message="nfs_addr required for rclone nfs client",
                context={"type": config.mount_type},
            )

        nfs_supported = await session.exec(
            "sh", "-lc", "grep -w nfs /proc/filesystems", shell=False
        )
        if not nfs_supported.ok():
            warnings.warn(
                "NFS client support not detected; attempting mount anyway. "
                "If it fails, use rclone fuse mode or run on a kernel with NFS support.",
                stacklevel=2,
            )

        # Default to localhost if no NFS address is provided
        host = "127.0.0.1"
        port = "2049"

        if ":" in nfs_addr:
            host, port = nfs_addr.rsplit(":", 1)
        else:
            host = nfs_addr
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"

        mount_options = self.nfs_mount_options or [
            "vers=4.1",
            "tcp",
            f"port={port}",
            "soft",
            "timeo=50",
            "retrans=1",
        ]
        option_arg = ",".join(mount_options)
        timeout_check = await session.exec(
            "sh", "-lc", "command -v timeout >/dev/null 2>&1", shell=False
        )
        timeout_prefix = "timeout 10s " if timeout_check.ok() else ""
        mount_cmd_string = " ".join(
            [
                "for i in 1 2 3; do",
                f"{timeout_prefix}mount",
                "-v",
                "-t",
                "nfs",
                "-o",
                shlex.quote(option_arg),
                f"{shlex.quote(host)}:/",
                shlex.quote(sandbox_path_str(path)),
                "&& exit 0; sleep 1; done; exit 1",
            ]
        )
        mount_cmd = (
            "sh",
            "-lc",
            mount_cmd_string,
        )
        mount_result = await session.exec(*mount_cmd, shell=False)
        if not mount_result.ok():
            raise MountCommandError(
                command=" ".join(mount_cmd),
                stderr=mount_result.stderr.decode("utf-8", errors="replace"),
                context={"type": config.mount_type},
            )

    async def apply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        rclone_config = _require_mount_config(config, RcloneMountConfig)
        tool_check = await session.exec(
            "sh",
            "-lc",
            "command -v rclone >/dev/null 2>&1 || test -x /usr/local/bin/rclone",
            shell=False,
        )
        if not tool_check.ok():
            raise MountToolMissingError(
                tool="rclone",
                context={"type": rclone_config.mount_type},
            )

        if rclone_config.config_text is None:
            raise MountConfigError(
                message="rclone mount requires config_text",
                context={"type": rclone_config.mount_type},
            )

        session_id = getattr(session.state, "session_id", None)
        if session_id is None:
            raise MountConfigError(
                message="mount session is missing session_id",
                context={"type": rclone_config.mount_type},
            )
        session_id_str = session_id.hex
        # Keep generated rclone config under the workspace root so `session.mkdir()` /
        # `session.write()` can handle it without special-casing absolute paths.
        config_dir = posix_path_as_path(
            coerce_posix_path(f".sandbox-rclone-config/{session_id_str}")
        )
        config_path = config_dir / f"{rclone_config.remote_name}.conf"
        await session.mkdir(path, parents=True)
        await session.mkdir(config_dir, parents=True)
        session.register_persist_workspace_skip_path(config_dir)
        # Always write an isolated config file for the live mount operation so provider-specific
        # augmentation does not mutate a shared source config in the workspace.
        await _write_sensitive_config_file(
            session,
            config_path,
            rclone_config.config_text.encode("utf-8"),
        )
        command_config_path = session.normalize_path(config_path)

        if self.mode == "nfs":
            nfs_addr = self.nfs_addr or "127.0.0.1:2049"
            await self._start_rclone_server(
                session,
                config=rclone_config,
                config_path=command_config_path,
                nfs_addr=nfs_addr,
            )
            await self._start_rclone_client(
                session,
                path=path,
                config=rclone_config,
                config_path=command_config_path,
                nfs_addr=nfs_addr,
            )
        else:
            # fuse mode
            await self._start_rclone_client(
                session,
                path=path,
                config=rclone_config,
                config_path=command_config_path,
            )

    async def unapply(
        self,
        session: BaseSandboxSession,
        path: Path,
        config: MountPatternConfig,
    ) -> None:
        rclone_config = _require_mount_config(config, RcloneMountConfig)
        if self.mode == "fuse":
            await session.exec(
                "sh",
                "-lc",
                f"fusermount3 -u {shlex.quote(sandbox_path_str(path))} || "
                f"umount {shlex.quote(sandbox_path_str(path))}",
                shell=False,
            )
        if self.mode == "nfs":
            await session.exec(
                "sh",
                "-lc",
                f"umount {shlex.quote(sandbox_path_str(path))} >/dev/null 2>&1 || true",
                shell=False,
            )

        await session.exec(
            "sh",
            "-lc",
            (
                "pkill -f -- "
                f"'rclone (mount|serve nfs) {rclone_config.remote_name}:' >/dev/null 2>&1 || true"
            ),
            shell=False,
        )


MountPattern = Annotated[
    FuseMountPattern | MountpointMountPattern | RcloneMountPattern | S3FilesMountPattern,
    Field(discriminator="type"),
]
