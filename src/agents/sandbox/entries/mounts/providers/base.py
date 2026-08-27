from __future__ import annotations

import abc
import uuid
from typing import TYPE_CHECKING

from ....errors import MountConfigError
from ..base import (
    DockerVolumeMountAdapter,
    InContainerMountAdapter,
    InContainerMountStrategy,
    Mount,
)
from ..patterns import (
    MountPattern,
    MountPatternConfig,
    RcloneMountConfig,
    RcloneMountPattern,
    _supplement_rclone_config_text,
)

if TYPE_CHECKING:
    from ....session.base_sandbox_session import BaseSandboxSession


class _ConfiguredMount(Mount, abc.ABC):
    """Base class for provider-backed mounts that can derive both strategy shapes from one model.

    Subclasses keep provider-specific translation logic here:
    - in-container: build a `MountPatternConfig` for the selected `MountPattern`.
    - docker-volume: build Docker volume driver options for the selected driver.
    Strategy objects own when those hooks are called.
    """

    def _require_mount_pattern(self) -> MountPattern:
        """Return the active in-container pattern.

        Fail if this mount is not using the in-container strategy.
        """

        if not isinstance(self.mount_strategy, InContainerMountStrategy):
            raise MountConfigError(
                message=f"{self.type} requires in-container mount strategy",
                context={"type": self.type},
            )
        return self.mount_strategy.pattern

    def in_container_adapter(self) -> InContainerMountAdapter:
        """Use pattern-driven in-container behavior for built-in provider mounts."""

        return InContainerMountAdapter(self)

    def docker_volume_adapter(self) -> DockerVolumeMountAdapter:
        """Use Docker volume-driver behavior for built-in provider mounts."""

        return DockerVolumeMountAdapter(self)

    @staticmethod
    def _require_session_id_hex(session: BaseSandboxSession, mount_type: str) -> str:
        """Return the current session id as hex for per-session temp config names."""

        session_id = getattr(session.state, "session_id", None)
        if not isinstance(session_id, uuid.UUID):
            raise MountConfigError(
                message="mount session is missing session_id",
                context={"type": mount_type},
            )
        return session_id.hex

    @staticmethod
    def _join_remote_path(root: str, prefix: str | None) -> str:
        """Join a bucket/container root with an optional object prefix for driver paths."""

        if prefix is None:
            return root
        return f"{root}/{prefix.lstrip('/')}"

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
        """Build isolated rclone runtime config for a single live mount operation.

        When `include_config_text` is false, callers only need the remote identity for teardown,
        so we skip reading or synthesizing config text.
        """

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
            read_only=self.read_only,
        )

    @abc.abstractmethod
    async def build_in_container_mount_config(
        self,
        session: BaseSandboxSession,
        pattern: MountPattern,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig:
        """Translate provider fields into the runtime config expected by `pattern.apply()`."""

        raise NotImplementedError
