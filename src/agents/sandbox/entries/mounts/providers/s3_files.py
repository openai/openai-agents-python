from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from ....errors import MountConfigError
from ..patterns import (
    MountPattern,
    MountPatternConfig,
    S3FilesMountConfig,
    S3FilesMountPattern,
)
from .base import _ConfiguredMount

if TYPE_CHECKING:
    from ....session.base_sandbox_session import BaseSandboxSession


class S3FilesMount(_ConfiguredMount):
    """Mount an existing Amazon S3 Files file system inside the sandbox.

    S3 Files exposes objects in an S3 bucket through an S3 file system that is
    mounted with the Linux `s3files` file-system type. AWS documents the mount
    helper at https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files-mounting.html.

    This mount does not create the S3 Files file system, mount target, VPC, or
    bucket configuration. It expects those resources to already exist and the
    sandbox container to run where the S3 Files mount target is reachable. In
    practice, run the container on infrastructure that has network access to a
    mount target in the S3 Files file system's VPC/AZ, and pass the file-system
    region when it cannot be discovered from the container's AWS environment.
    At mount time, the selected `S3FilesMountPattern` runs `mount -t s3files`
    inside the sandbox using `file_system_id` as the device, optional `subpath`
    as the file-system subdirectory, and any supplied mount-helper options such
    as `mount_target_ip`, `access_point`, `region`, or `extra_options`.
    """

    type: Literal["s3_files_mount"] = "s3_files_mount"
    file_system_id: str
    subpath: str | None = None
    mount_target_ip: str | None = None
    access_point: str | None = None
    region: str | None = None
    extra_options: dict[str, str | None] = Field(default_factory=dict)

    def supported_in_container_patterns(self) -> tuple[builtins.type[MountPattern], ...]:
        return (S3FilesMountPattern,)

    async def build_in_container_mount_config(
        self,
        session: BaseSandboxSession,
        pattern: MountPattern,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig:
        _ = (session, include_config_text)
        if isinstance(pattern, S3FilesMountPattern):
            options = pattern.options
            return S3FilesMountConfig(
                file_system_id=self.file_system_id,
                subpath=self.subpath,
                mount_target_ip=self.mount_target_ip or options.mount_target_ip,
                access_point=self.access_point or options.access_point,
                region=self.region or options.region,
                extra_options=options.extra_options | self.extra_options,
                mount_type=self.type,
                read_only=self.read_only,
            )
        raise MountConfigError(
            message="invalid mount_pattern type",
            context={"type": self.type},
        )
