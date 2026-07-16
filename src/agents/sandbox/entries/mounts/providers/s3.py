from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Literal

from ....errors import MountConfigError
from ..base import DockerVolumeMountStrategy
from ..patterns import (
    MountPattern,
    MountPatternConfig,
    MountpointMountConfig,
    MountpointMountPattern,
    RcloneMountPattern,
)
from .base import _ConfiguredMount

if TYPE_CHECKING:
    from ....session.base_sandbox_session import BaseSandboxSession


class S3Mount(_ConfiguredMount):
    type: Literal["s3_mount"] = "s3_mount"
    bucket: str
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    prefix: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    s3_provider: str = "AWS"

    def supported_in_container_patterns(self) -> tuple[builtins.type[MountPattern], ...]:
        return (RcloneMountPattern, MountpointMountPattern)

    def supported_docker_volume_drivers(self) -> frozenset[str]:
        return frozenset({"mountpoint", "rclone"})

    def build_docker_volume_driver_config(
        self,
        strategy: DockerVolumeMountStrategy,
    ) -> tuple[str, dict[str, str], bool]:
        if strategy.driver == "rclone":
            options: dict[str, str] = {
                "type": "s3",
                "s3-provider": self.s3_provider,
                "path": self._join_remote_path(self.bucket, self.prefix),
            }
            if self.access_key_id is not None:
                options["s3-access-key-id"] = self.access_key_id
            if self.secret_access_key is not None:
                options["s3-secret-access-key"] = self.secret_access_key
            if self.session_token is not None:
                options["s3-session-token"] = self.session_token
            if self.endpoint_url is not None:
                options["s3-endpoint"] = self.endpoint_url
            if self.region is not None:
                options["s3-region"] = self.region
            return strategy.driver, options | strategy.driver_options, self.read_only

        options = {"bucket": self.bucket}
        if self.access_key_id is not None:
            options["access_key_id"] = self.access_key_id
        if self.secret_access_key is not None:
            options["secret_access_key"] = self.secret_access_key
        if self.session_token is not None:
            options["session_token"] = self.session_token
        if self.endpoint_url is not None:
            options["endpoint_url"] = self.endpoint_url
        if self.region is not None:
            options["region"] = self.region
        if self.prefix is not None:
            options["prefix"] = self.prefix
        return strategy.driver, options | strategy.driver_options, self.read_only

    async def build_in_container_mount_config(
        self,
        session: BaseSandboxSession,
        pattern: MountPattern,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig:
        if isinstance(pattern, RcloneMountPattern):
            return await self._build_rclone_config(
                session=session,
                pattern=pattern,
                remote_kind="s3",
                remote_path=self._join_remote_path(self.bucket, self.prefix),
                required_lines=self._rclone_required_lines(
                    pattern.resolve_remote_name(
                        session_id=self._require_session_id_hex(session, self.type),
                        remote_kind="s3",
                        mount_type=self.type,
                    )
                ),
                include_config_text=include_config_text,
            )
        if isinstance(pattern, MountpointMountPattern):
            options = pattern.options
            return MountpointMountConfig(
                bucket=self.bucket,
                access_key_id=self.access_key_id,
                secret_access_key=self.secret_access_key,
                session_token=self.session_token,
                prefix=self.prefix or options.prefix,
                region=self.region or options.region,
                endpoint_url=self.endpoint_url or options.endpoint_url,
                mount_type=self.type,
                read_only=self.read_only,
            )
        raise MountConfigError(
            message="invalid mount_pattern type",
            context={"type": self.type},
        )

    def _rclone_required_lines(self, remote_name: str) -> list[str]:
        lines = [
            f"[{remote_name}]",
            "type = s3",
            f"provider = {self.s3_provider}",
        ]
        if self.endpoint_url is not None:
            lines.append(f"endpoint = {self.endpoint_url}")
        if self.region is not None:
            lines.append(f"region = {self.region}")
        if self.access_key_id and self.secret_access_key:
            lines.append("env_auth = false")
            lines.append(f"access_key_id = {self.access_key_id}")
            lines.append(f"secret_access_key = {self.secret_access_key}")
            if self.session_token:
                lines.append(f"session_token = {self.session_token}")
        else:
            lines.append("env_auth = true")
        return lines
