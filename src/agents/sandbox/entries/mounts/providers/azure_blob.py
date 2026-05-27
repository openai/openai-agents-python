from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Literal

from ....errors import MountConfigError
from ..base import DockerVolumeMountStrategy
from ..patterns import (
    FuseMountConfig,
    FuseMountPattern,
    MountPattern,
    MountPatternConfig,
    RcloneMountPattern,
)
from .base import _ConfiguredMount

if TYPE_CHECKING:
    from ....session.base_sandbox_session import BaseSandboxSession


class AzureBlobMount(_ConfiguredMount):
    type: Literal["azure_blob_mount"] = "azure_blob_mount"
    account: str  # AZURE_STORAGE_ACCOUNT
    container: str  # AZURE_STORAGE_CONTAINER
    endpoint: str | None = None
    identity_client_id: str | None = None  # AZURE_CLIENT_ID
    account_key: str | None = None  # AZURE_STORAGE_ACCOUNT_KEY

    def supported_in_container_patterns(self) -> tuple[builtins.type[MountPattern], ...]:
        return (RcloneMountPattern, FuseMountPattern)

    def supported_docker_volume_drivers(self) -> frozenset[str]:
        return frozenset({"rclone"})

    def build_docker_volume_driver_config(
        self,
        strategy: DockerVolumeMountStrategy,
    ) -> tuple[str, dict[str, str], bool]:
        options = {
            "type": "azureblob",
            "path": self.container,
            "azureblob-account": self.account,
        }
        if self.endpoint is not None:
            options["azureblob-endpoint"] = self.endpoint
        if self.identity_client_id is not None:
            options["azureblob-msi-client-id"] = self.identity_client_id
        if self.account_key is not None:
            options["azureblob-key"] = self.account_key
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
                read_only=self.read_only,
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
