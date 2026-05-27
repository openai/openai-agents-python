from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Literal

from ....errors import MountConfigError
from ..base import DockerVolumeMountStrategy
from ..patterns import MountPattern, MountPatternConfig, RcloneMountPattern
from .base import _ConfiguredMount

if TYPE_CHECKING:
    from ....session.base_sandbox_session import BaseSandboxSession


class R2Mount(_ConfiguredMount):
    type: Literal["r2_mount"] = "r2_mount"
    bucket: str
    account_id: str
    access_key_id: str | None = None
    secret_access_key: str | None = None
    custom_domain: str | None = None

    def _validate_credential_pair(self) -> None:
        if (self.access_key_id is None) != (self.secret_access_key is None):
            raise MountConfigError(
                message="r2 credentials must include both access_key_id and secret_access_key",
                context={"type": self.type},
            )

    def supported_in_container_patterns(self) -> tuple[builtins.type[MountPattern], ...]:
        return (RcloneMountPattern,)

    def supported_docker_volume_drivers(self) -> frozenset[str]:
        return frozenset({"rclone"})

    def build_docker_volume_driver_config(
        self,
        strategy: DockerVolumeMountStrategy,
    ) -> tuple[str, dict[str, str], bool]:
        self._validate_credential_pair()
        options: dict[str, str] = {
            "type": "s3",
            "path": self.bucket,
            "s3-provider": "Cloudflare",
            "s3-endpoint": (
                self.custom_domain or f"https://{self.account_id}.r2.cloudflarestorage.com"
            ),
        }
        if self.access_key_id is not None:
            options["s3-access-key-id"] = self.access_key_id
        if self.secret_access_key is not None:
            options["s3-secret-access-key"] = self.secret_access_key
        return strategy.driver, options | strategy.driver_options, self.read_only

    async def build_in_container_mount_config(
        self,
        session: BaseSandboxSession,
        pattern: MountPattern,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig:
        self._validate_credential_pair()
        if isinstance(pattern, RcloneMountPattern):
            return await self._build_rclone_config(
                session=session,
                pattern=pattern,
                remote_kind="r2",
                remote_path=self.bucket,
                required_lines=self._rclone_required_lines(
                    pattern.resolve_remote_name(
                        session_id=self._require_session_id_hex(session, self.type),
                        remote_kind="r2",
                        mount_type=self.type,
                    )
                ),
                include_config_text=include_config_text,
            )
        raise MountConfigError(
            message="invalid mount_pattern type",
            context={"type": self.type},
        )

    def _rclone_required_lines(self, remote_name: str) -> list[str]:
        lines = [
            f"[{remote_name}]",
            "type = s3",
            "provider = Cloudflare",
            (
                "endpoint = "
                f"{self.custom_domain or f'https://{self.account_id}.r2.cloudflarestorage.com'}"
            ),
            "acl = private",
        ]
        if self.access_key_id and self.secret_access_key:
            lines.append("env_auth = false")
            lines.append(f"access_key_id = {self.access_key_id}")
            lines.append(f"secret_access_key = {self.secret_access_key}")
        else:
            lines.append("env_auth = true")
        return lines
