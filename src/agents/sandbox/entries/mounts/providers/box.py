from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Literal

from ....errors import MountConfigError
from ..base import DockerVolumeMountStrategy
from ..patterns import MountPattern, MountPatternConfig, RcloneMountPattern
from .base import _ConfiguredMount

if TYPE_CHECKING:
    from ....session.base_sandbox_session import BaseSandboxSession


class BoxMount(_ConfiguredMount):
    """Mount a Box folder using rclone.

    See Box's JWT setup guide (https://developer.box.com/guides/authentication/jwt/jwt-setup/)
    and rclone's Box guide (https://rclone.org/box/). Non-interactive mounts require
    a minted `token` or `access_token`.
    """

    type: Literal["box_mount"] = "box_mount"
    path: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    access_token: str | None = None
    token: str | None = None
    box_config_file: str | None = None
    config_credentials: str | None = None
    box_sub_type: Literal["user", "enterprise"] = "user"
    root_folder_id: str | None = None
    impersonate: str | None = None
    owned_by: str | None = None

    def supported_in_container_patterns(self) -> tuple[builtins.type[MountPattern], ...]:
        return (RcloneMountPattern,)

    def supported_docker_volume_drivers(self) -> frozenset[str]:
        return frozenset({"rclone"})

    def build_docker_volume_driver_config(
        self,
        strategy: DockerVolumeMountStrategy,
    ) -> tuple[str, dict[str, str], bool]:
        options: dict[str, str] = {"type": "box", "path": self._remote_path()}
        if self.client_id is not None:
            options["box-client-id"] = self.client_id
        if self.client_secret is not None:
            options["box-client-secret"] = self.client_secret
        if self.access_token is not None:
            options["box-access-token"] = self.access_token
        if self.token is not None:
            options["box-token"] = self.token
        if self.box_config_file is not None:
            options["box-box-config-file"] = self.box_config_file
        if self.config_credentials is not None:
            options["box-config-credentials"] = self.config_credentials
        if self.box_sub_type != "user":
            options["box-box-sub-type"] = self.box_sub_type
        if self.root_folder_id is not None:
            options["box-root-folder-id"] = self.root_folder_id
        if self.impersonate is not None:
            options["box-impersonate"] = self.impersonate
        if self.owned_by is not None:
            options["box-owned-by"] = self.owned_by
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
                remote_kind="box",
                remote_path=self._remote_path(),
                required_lines=self._rclone_required_lines(
                    pattern.resolve_remote_name(
                        session_id=self._require_session_id_hex(session, self.type),
                        remote_kind="box",
                        mount_type=self.type,
                    )
                ),
                include_config_text=include_config_text,
            )
        raise MountConfigError(
            message="invalid mount_pattern type",
            context={"type": self.type},
        )

    def _remote_path(self) -> str:
        if self.path is None:
            return ""
        return self.path.lstrip("/")

    def _rclone_required_lines(self, remote_name: str) -> list[str]:
        lines = [
            f"[{remote_name}]",
            "type = box",
        ]
        if self.client_id is not None:
            lines.append(f"client_id = {self.client_id}")
        if self.client_secret is not None:
            lines.append(f"client_secret = {self.client_secret}")
        if self.access_token is not None:
            lines.append(f"access_token = {self.access_token}")
        if self.token is not None:
            lines.append(f"token = {self.token}")
        if self.box_config_file is not None:
            lines.append(f"box_config_file = {self.box_config_file}")
        if self.config_credentials is not None:
            lines.append(f"config_credentials = {self.config_credentials}")
        if self.box_sub_type != "user":
            lines.append(f"box_sub_type = {self.box_sub_type}")
        if self.root_folder_id is not None:
            lines.append(f"root_folder_id = {self.root_folder_id}")
        if self.impersonate is not None:
            lines.append(f"impersonate = {self.impersonate}")
        if self.owned_by is not None:
            lines.append(f"owned_by = {self.owned_by}")
        return lines
