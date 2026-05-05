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


class GCSMount(_ConfiguredMount):
    type: Literal["gcs_mount"] = "gcs_mount"
    bucket: str
    access_id: str | None = None
    secret_access_key: str | None = None
    prefix: str | None = None
    region: str | None = None
    endpoint_url: str | None = None
    service_account_file: str | None = None
    service_account_credentials: str | None = None
    access_token: str | None = None

    def supported_in_container_patterns(self) -> tuple[builtins.type[MountPattern], ...]:
        return (RcloneMountPattern, MountpointMountPattern)

    def supported_docker_volume_drivers(self) -> frozenset[str]:
        return frozenset({"mountpoint", "rclone"})

    def _use_s3_compatible_rclone(self) -> bool:
        """Return true when this mount has GCS HMAC credentials for rclone's S3 backend."""

        return self.access_id is not None and self.secret_access_key is not None

    def _rclone_remote_kind(self) -> str:
        if self._use_s3_compatible_rclone():
            # Keep HMAC-auth GCS mounts in a distinct generated remote-name namespace from real S3
            # mounts. The config backend is still rclone's S3 backend, but the remote section/file
            # name must not collide with `S3Mount` in the same session.
            return "gcs_s3"
        return "gcs"

    def build_docker_volume_driver_config(
        self,
        strategy: DockerVolumeMountStrategy,
    ) -> tuple[str, dict[str, str], bool]:
        if strategy.driver == "rclone":
            if self._use_s3_compatible_rclone():
                assert self.access_id is not None
                assert self.secret_access_key is not None
                hmac_options: dict[str, str] = {
                    "type": "s3",
                    "path": self._join_remote_path(self.bucket, self.prefix),
                    "s3-provider": "GCS",
                    "s3-access-key-id": self.access_id,
                    "s3-secret-access-key": self.secret_access_key,
                    "s3-endpoint": self.endpoint_url or "https://storage.googleapis.com",
                }
                if self.region is not None:
                    hmac_options["s3-region"] = self.region
                return strategy.driver, hmac_options | strategy.driver_options, self.read_only

            native_options: dict[str, str] = {
                "type": "google cloud storage",
                "path": self._join_remote_path(self.bucket, self.prefix),
            }
            if self.service_account_file is not None:
                native_options["gcs-service-account-file"] = self.service_account_file
            if self.service_account_credentials is not None:
                native_options["gcs-service-account-credentials"] = self.service_account_credentials
            if self.access_token is not None:
                native_options["gcs-access-token"] = self.access_token
            return strategy.driver, native_options | strategy.driver_options, self.read_only

        mountpoint_options: dict[str, str] = {
            "bucket": self.bucket,
            "endpoint_url": self.endpoint_url or "https://storage.googleapis.com",
        }
        if self.access_id is not None:
            mountpoint_options["access_key_id"] = self.access_id
        if self.secret_access_key is not None:
            mountpoint_options["secret_access_key"] = self.secret_access_key
        if self.region is not None:
            mountpoint_options["region"] = self.region
        if self.prefix is not None:
            mountpoint_options["prefix"] = self.prefix
        return strategy.driver, mountpoint_options | strategy.driver_options, self.read_only

    async def build_in_container_mount_config(
        self,
        session: BaseSandboxSession,
        pattern: MountPattern,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig:
        if isinstance(pattern, RcloneMountPattern):
            if self._use_s3_compatible_rclone():
                remote_kind = self._rclone_remote_kind()
                return await self._build_rclone_config(
                    session=session,
                    pattern=pattern,
                    remote_kind=remote_kind,
                    remote_path=self._join_remote_path(self.bucket, self.prefix),
                    required_lines=self._s3_compatible_rclone_required_lines(
                        pattern.resolve_remote_name(
                            session_id=self._require_session_id_hex(session, self.type),
                            remote_kind=remote_kind,
                            mount_type=self.type,
                        )
                    ),
                    include_config_text=include_config_text,
                )

            remote_kind = self._rclone_remote_kind()
            return await self._build_rclone_config(
                session=session,
                pattern=pattern,
                remote_kind=remote_kind,
                remote_path=self._join_remote_path(self.bucket, self.prefix),
                required_lines=self._rclone_required_lines(
                    pattern.resolve_remote_name(
                        session_id=self._require_session_id_hex(session, self.type),
                        remote_kind=remote_kind,
                        mount_type=self.type,
                    )
                ),
                include_config_text=include_config_text,
            )
        if isinstance(pattern, MountpointMountPattern):
            options = pattern.options
            return MountpointMountConfig(
                bucket=self.bucket,
                access_key_id=self.access_id,
                secret_access_key=self.secret_access_key,
                session_token=None,
                prefix=self.prefix or options.prefix,
                region=self.region or options.region,
                endpoint_url=(
                    self.endpoint_url or options.endpoint_url or "https://storage.googleapis.com"
                ),
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
            "type = google cloud storage",
        ]
        if self.service_account_file:
            lines.append(f"service_account_file = {self.service_account_file}")
        if self.service_account_credentials:
            lines.append(f"service_account_credentials = {self.service_account_credentials}")
        if self.access_token:
            lines.append(f"access_token = {self.access_token}")
        if (
            self.service_account_file is None
            and self.service_account_credentials is None
            and self.access_token is None
        ):
            lines.append("env_auth = true")
        else:
            lines.append("env_auth = false")
        return lines

    def _s3_compatible_rclone_required_lines(self, remote_name: str) -> list[str]:
        lines = [
            f"[{remote_name}]",
            "type = s3",
            "provider = GCS",
            "env_auth = false",
            f"access_key_id = {self.access_id}",
            f"secret_access_key = {self.secret_access_key}",
            f"endpoint = {self.endpoint_url or 'https://storage.googleapis.com'}",
        ]
        if self.region:
            lines.append(f"region = {self.region}")
        return lines
