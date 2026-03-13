from __future__ import annotations

import abc
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ...errors import MountConfigError
from .base import Mount
from .patterns import (
    FuseMountConfig,
    FuseMountPattern,
    MountPattern,
    MountPatternConfig,
    MountpointMountConfig,
    MountpointMountPattern,
    RcloneMountConfig,
    RcloneMountPattern,
    _supplement_rclone_config_text,
)

if TYPE_CHECKING:
    from ...session.base_sandbox_session import BaseSandboxSession


class _ConfiguredMount(Mount, abc.ABC):
    mount_pattern: MountPattern | None = None

    def _require_mount_pattern(self) -> MountPattern:
        if self.mount_pattern is None:
            raise MountConfigError(
                message=f"{self.type} requires mount_pattern",
                context={"type": self.type},
            )
        return self.mount_pattern

    @staticmethod
    def _require_session_id_hex(session: BaseSandboxSession, mount_type: str) -> str:
        session_id = getattr(session.state, "session_id", None)
        if not isinstance(session_id, uuid.UUID):
            raise MountConfigError(
                message="mount session is missing session_id",
                context={"type": mount_type},
            )
        return session_id.hex

    async def _build_rclone_config(
        self,
        *,
        session: BaseSandboxSession,
        pattern: RcloneMountPattern,
        remote_kind: str,
        remote_path: str,
        required_lines: list[str],
        include_config_text: bool,
    ) -> RcloneMountConfig:
        remote_name = pattern.resolve_remote_name(
            session_id=self._require_session_id_hex(session, self.type),
            remote_kind=remote_kind,
            mount_type=self.type,
        )
        config_text: str | None = None
        if include_config_text:
            if pattern.config_file_path is not None:
                config_text = await pattern.read_config_text(
                    session,
                    remote_name,
                    mount_type=self.type,
                )
                config_text = _supplement_rclone_config_text(
                    config_text=config_text,
                    remote_name=remote_name,
                    required_lines=required_lines,
                    mount_type=self.type,
                )
            else:
                config_text = "\n".join(required_lines) + "\n"
        return RcloneMountConfig(
            remote_name=remote_name,
            remote_path=remote_path,
            remote_kind=remote_kind,
            mount_type=self.type,
            config_text=config_text,
        )

    @abc.abstractmethod
    async def _build_mount_config(
        self,
        session: BaseSandboxSession,
        pattern: MountPattern,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig:
        raise NotImplementedError

    async def _mount(self, session: BaseSandboxSession, path: Path) -> None:
        pattern = self._require_mount_pattern()
        config = await self._build_mount_config(session, pattern, include_config_text=True)
        await pattern.apply(session, path, config)

    async def _unmount(self, session: BaseSandboxSession, path: Path) -> None:
        pattern = self._require_mount_pattern()
        config = await self._build_mount_config(session, pattern, include_config_text=False)
        await pattern.unapply(session, path, config)


class AzureBlobMount(_ConfiguredMount):
    type: Literal["azure_blob_mount"] = "azure_blob_mount"
    account: str  # AZURE_STORAGE_ACCOUNT
    container: str  # AZURE_STORAGE_CONTAINER
    endpoint: str | None = None
    identity_client_id: str | None = None  # AZURE_CLIENT_ID
    account_key: str | None = None  # AZURE_STORAGE_ACCOUNT_KEY

    def model_post_init(self, context: object, /) -> None:
        super().model_post_init(context)
        pattern = self._require_mount_pattern()
        if not isinstance(pattern, (RcloneMountPattern, FuseMountPattern)):
            raise MountConfigError(
                message="invalid mount_pattern type",
                context={"type": self.type},
            )

    async def _build_mount_config(
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
                remote_kind="azureblob",
                remote_path=self.container,
                required_lines=self._rclone_required_lines(
                    pattern.resolve_remote_name(
                        session_id=self._require_session_id_hex(session, self.type),
                        remote_kind="azureblob",
                        mount_type=self.type,
                    )
                ),
                include_config_text=include_config_text,
            )
        if isinstance(pattern, FuseMountPattern):
            return FuseMountConfig(
                account=self.account,
                container=self.container,
                endpoint=self.endpoint,
                identity_client_id=self.identity_client_id,
                account_key=self.account_key,
                mount_type=self.type,
            )
        raise MountConfigError(
            message="invalid mount_pattern type",
            context={"type": self.type},
        )

    def _rclone_required_lines(self, remote_name: str) -> list[str]:
        lines = [
            f"[{remote_name}]",
            "type = azureblob",
            f"account = {self.account}",
        ]
        if self.endpoint:
            lines.append(f"endpoint = {self.endpoint}")
        if self.account_key:
            lines.append(f"key = {self.account_key}")
        else:
            lines.append("use_msi = true")
            if self.identity_client_id:
                lines.append(f"msi_client_id = {self.identity_client_id}")
        return lines


class S3Mount(_ConfiguredMount):
    type: Literal["s3_mount"] = "s3_mount"
    bucket: str
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None

    def model_post_init(self, context: object, /) -> None:
        super().model_post_init(context)
        pattern = self._require_mount_pattern()
        if not isinstance(pattern, (RcloneMountPattern, MountpointMountPattern)):
            raise MountConfigError(
                message="invalid mount_pattern type",
                context={"type": self.type},
            )

    async def _build_mount_config(
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
                remote_path=self.bucket,
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
                prefix=options.prefix,
                region=options.region,
                endpoint_url=options.endpoint_url,
                mount_type=self.type,
            )
        raise MountConfigError(
            message="invalid mount_pattern type",
            context={"type": self.type},
        )

    def _rclone_required_lines(self, remote_name: str) -> list[str]:
        lines = [
            f"[{remote_name}]",
            "type = s3",
            "provider = AWS",
        ]
        if self.access_key_id and self.secret_access_key:
            lines.append("env_auth = false")
            lines.append(f"access_key_id = {self.access_key_id}")
            lines.append(f"secret_access_key = {self.secret_access_key}")
            if self.session_token:
                lines.append(f"session_token = {self.session_token}")
        else:
            lines.append("env_auth = true")
        return lines


class GCSMount(_ConfiguredMount):
    type: Literal["gcs_mount"] = "gcs_mount"
    bucket: str
    access_id: str | None = None
    secret_access_key: str | None = None

    def model_post_init(self, context: object, /) -> None:
        super().model_post_init(context)
        if self.mount_pattern is None:
            # GCS defaults to the S3-compatible mountpoint path so examples can omit the pattern
            # unless they specifically need rclone behavior.
            self.mount_pattern = MountpointMountPattern()
        pattern = self._require_mount_pattern()
        if not isinstance(pattern, (RcloneMountPattern, MountpointMountPattern)):
            raise MountConfigError(
                message="invalid mount_pattern type",
                context={"type": self.type},
            )

    async def _build_mount_config(
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
                remote_kind="gcs",
                remote_path=self.bucket,
                required_lines=self._rclone_required_lines(
                    pattern.resolve_remote_name(
                        session_id=self._require_session_id_hex(session, self.type),
                        remote_kind="gcs",
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
                prefix=options.prefix,
                region=options.region,
                endpoint_url=options.endpoint_url or "https://storage.googleapis.com",
                mount_type=self.type,
            )
        raise MountConfigError(
            message="invalid mount_pattern type",
            context={"type": self.type},
        )

    def _rclone_required_lines(self, remote_name: str) -> list[str]:
        lines = [
            f"[{remote_name}]",
            "type = s3",
            "provider = GCS",
            "endpoint = https://storage.googleapis.com",
        ]
        if self.access_id and self.secret_access_key:
            lines.append("env_auth = false")
            lines.append(f"access_key_id = {self.access_id}")
            lines.append(f"secret_access_key = {self.secret_access_key}")
        else:
            lines.append("env_auth = true")
        return lines
