from __future__ import annotations

import abc
import builtins
import inspect
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import BaseModel, Field, SerializeAsAny, field_validator

from ...errors import InvalidManifestPathError, MountConfigError
from ...materialization import MaterializedFile
from ...types import FileMode, Permissions
from ...workspace_paths import coerce_posix_path, posix_path_as_path, windows_absolute_path
from ..base import BaseEntry
from .patterns import MountPattern, MountPatternBase, MountPatternConfig

if TYPE_CHECKING:
    from ...session.base_sandbox_session import BaseSandboxSession


class InContainerMountAdapter:
    """Default adapter for mounts materialized by commands inside the sandbox.

    Provider-backed mounts use this directly to translate model fields into a
    `MountPatternConfig`, then run the selected `MountPattern`.
    """

    def __init__(self, mount: Mount) -> None:
        self._mount = mount

    def validate(self, strategy: InContainerMountStrategy) -> None:
        if not isinstance(strategy.pattern, self._mount.supported_in_container_patterns()):
            raise MountConfigError(
                message="invalid mount_pattern type",
                context={"type": self._mount.type},
            )

    async def _build_config(
        self,
        strategy: InContainerMountStrategy,
        session: BaseSandboxSession,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig:
        config = await self._mount.build_in_container_mount_config(
            session,
            strategy.pattern,
            include_config_text=include_config_text,
        )
        if config is None:
            raise MountConfigError(
                message="configured in-container mount did not return pattern config",
                context={"type": self._mount.type},
            )
        return config

    async def activate(
        self,
        strategy: InContainerMountStrategy,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        _ = base_dir
        mount_path = self._mount._resolve_mount_path(session, dest)
        config = await self._build_config(strategy, session, include_config_text=True)
        await strategy.pattern.apply(session, mount_path, config)
        return []

    async def deactivate(
        self,
        strategy: InContainerMountStrategy,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        _ = base_dir
        mount_path = self._mount._resolve_mount_path(session, dest)
        config = await self._build_config(strategy, session, include_config_text=False)
        await strategy.pattern.unapply(session, mount_path, config)

    async def teardown_for_snapshot(
        self,
        strategy: InContainerMountStrategy,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        config = await self._build_config(strategy, session, include_config_text=False)
        await strategy.pattern.unapply(session, path, config)

    async def restore_after_snapshot(
        self,
        strategy: InContainerMountStrategy,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        config = await self._build_config(strategy, session, include_config_text=True)
        await strategy.pattern.apply(session, path, config)


class DockerVolumeMountAdapter:
    """Default adapter for mounts attached by the host container runtime."""

    def __init__(self, mount: Mount) -> None:
        self._mount = mount

    def validate(self, strategy: DockerVolumeMountStrategy) -> None:
        if strategy.driver not in self._mount.supported_docker_volume_drivers():
            raise MountConfigError(
                message="invalid Docker volume driver",
                context={"type": self._mount.type, "driver": strategy.driver},
            )

    def build_docker_volume_driver_config(
        self,
        strategy: DockerVolumeMountStrategy,
    ) -> tuple[str, dict[str, str], bool]:
        return self._mount.build_docker_volume_driver_config(strategy)


class MountStrategyBase(BaseModel, abc.ABC):
    type: str
    _subclass_registry: ClassVar[dict[str, builtins.type[MountStrategyBase]]] = {}

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)

        type_field = cls.model_fields.get("type")
        type_default = type_field.default if type_field is not None else None
        if not isinstance(type_default, str) or type_default == "":
            if inspect.isabstract(cls):
                return
            raise TypeError(f"{cls.__name__} must define a non-empty string default for `type`")

        existing = MountStrategyBase._subclass_registry.get(type_default)
        if existing is not None and existing is not cls:
            if existing.__module__ == cls.__module__ and existing.__qualname__ == cls.__qualname__:
                MountStrategyBase._subclass_registry[type_default] = cls
                return
            raise TypeError(
                f"mount strategy type `{type_default}` is already registered by {existing.__name__}"
            )
        MountStrategyBase._subclass_registry[type_default] = cls

    @classmethod
    def parse(cls, payload: object) -> MountStrategyBase:
        if isinstance(payload, MountStrategyBase):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError("mount strategy payload must be a MountStrategyBase or object payload")

        strategy_type = payload.get("type")
        if not isinstance(strategy_type, str):
            raise ValueError("mount strategy payload must include a string `type` field")

        strategy_cls = MountStrategyBase._subclass_registry.get(strategy_type)
        if strategy_cls is None:
            known = ", ".join(sorted(MountStrategyBase._subclass_registry)) or "<none>"
            raise ValueError(
                f"Unknown mount strategy type `{strategy_type}`. Registered types: {known}"
            )
        return strategy_cls.model_validate(dict(payload))

    @abc.abstractmethod
    def validate_mount(self, mount: Mount) -> None:
        raise NotImplementedError

    def supports_native_snapshot_detach(self, mount: Mount) -> bool:
        """Return whether native snapshot flows can safely detach this mount in-place."""
        _ = mount
        return True

    @abc.abstractmethod
    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        raise NotImplementedError

    @abc.abstractmethod
    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        raise NotImplementedError


class InContainerMountStrategy(MountStrategyBase):
    type: Literal["in_container"] = "in_container"
    pattern: MountPattern

    def validate_mount(self, mount: Mount) -> None:
        mount.in_container_adapter().validate(self)

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        return await mount.in_container_adapter().activate(self, session, dest, base_dir)

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        await mount.in_container_adapter().deactivate(self, session, dest, base_dir)

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        await mount.in_container_adapter().teardown_for_snapshot(self, session, path)

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        await mount.in_container_adapter().restore_after_snapshot(self, session, path)

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        _ = mount
        return None


class DockerVolumeMountStrategy(MountStrategyBase):
    type: Literal["docker_volume"] = "docker_volume"
    driver: str
    driver_options: dict[str, str] = Field(default_factory=dict)

    def validate_mount(self, mount: Mount) -> None:
        mount.docker_volume_adapter().validate(self)

    async def activate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        if not session.supports_docker_volume_mounts():
            raise MountConfigError(
                message="docker-volume mounts are not supported by this sandbox backend",
                context={"mount_type": mount.type, "session_type": type(session).__name__},
            )
        _ = (mount, session, dest, base_dir)
        return []

    async def deactivate(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        if not session.supports_docker_volume_mounts():
            raise MountConfigError(
                message="docker-volume mounts are not supported by this sandbox backend",
                context={"mount_type": mount.type, "session_type": type(session).__name__},
            )
        _ = (mount, session, dest, base_dir)
        return None

    async def teardown_for_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _ = (mount, session, path)
        return None

    async def restore_after_snapshot(
        self,
        mount: Mount,
        session: BaseSandboxSession,
        path: Path,
    ) -> None:
        _ = (mount, session, path)
        return None

    def build_docker_volume_driver_config(
        self,
        mount: Mount,
    ) -> tuple[str, dict[str, str], bool] | None:
        return mount.docker_volume_adapter().build_docker_volume_driver_config(self)


MountStrategy = SerializeAsAny[MountStrategyBase]


class Mount(BaseEntry):
    """A manifest entry that exposes external storage inside the sandbox workspace.

    `Mount` holds strategy-independent mount metadata and delegates lifecycle behavior to
    `mount_strategy`. Provider subclasses describe what to mount; the strategy describes how the
    backend should make it available.
    """

    is_dir: bool = True
    _abstract_entry_base: ClassVar[bool] = True
    mount_path: Path | None = None
    # Mounts are runtime-attached external filesystems, not durable workspace state, so
    # snapshots must always treat them as ephemeral.
    ephemeral: bool = True
    read_only: bool = Field(default=True)
    mount_strategy: MountStrategy

    @field_validator("mount_strategy", mode="before")
    @classmethod
    def _parse_mount_strategy(cls, value: object) -> MountStrategyBase:
        return MountStrategyBase.parse(value)

    def model_post_init(self, context: object, /) -> None:
        """Normalize mount metadata and validate that the active strategy fits this mount type."""

        _ = context

        default_permissions = Permissions(
            owner=FileMode.ALL,
            group=FileMode.READ | FileMode.EXEC,
            other=FileMode.READ | FileMode.EXEC,
        )
        if (
            self.permissions.owner != default_permissions.owner
            or self.permissions.group != default_permissions.group
            or self.permissions.other != default_permissions.other
        ):
            warnings.warn(
                "Mount permissions are not enforced. "
                "Please configure access in the cloud provider instead; "
                "mount-level permissions can be unreliable.",
                stacklevel=2,
            )
            self.permissions.owner = default_permissions.owner
            self.permissions.group = default_permissions.group
            self.permissions.other = default_permissions.other
        self.permissions.directory = True
        if (
            not self.supported_in_container_patterns()
            and not self.supported_docker_volume_drivers()
        ):
            raise MountConfigError(
                message="mount type must support at least one mount strategy",
                context={"mount_type": self.type},
            )
        self.mount_strategy.validate_mount(self)

    def in_container_adapter(self) -> InContainerMountAdapter:
        """Return the strategy adapter for in-container mount lifecycle.

        Mount subclasses that do not support in-container mounts inherit this default unsupported
        implementation.
        """

        raise MountConfigError(
            message="in-container mounts are not supported for this mount type",
            context={"mount_type": self.type},
        )

    def docker_volume_adapter(self) -> DockerVolumeMountAdapter:
        """Return the strategy adapter for Docker volume lifecycle."""

        return DockerVolumeMountAdapter(self)

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        """Activate this mount for a manifest application pass.

        In-container strategies run a live mount command here. Docker-volume strategies are
        intentionally no-ops because the backend attaches them before the session starts.
        """

        return await self.mount_strategy.activate(self, session, dest, base_dir)

    async def unmount(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> None:
        """Deactivate this mount for manifest teardown."""

        await self.mount_strategy.deactivate(self, session, dest, base_dir)

    async def build_in_container_mount_config(
        self,
        session: BaseSandboxSession,
        pattern: MountPattern,
        *,
        include_config_text: bool,
    ) -> MountPatternConfig | None:
        """Return pattern runtime config for provider-backed in-container mounts."""

        _ = (session, pattern, include_config_text)
        return None

    def supported_in_container_patterns(self) -> tuple[builtins.type[MountPatternBase], ...]:
        """Return the `MountPattern` classes accepted by `InContainerMountStrategy`."""

        return ()

    def supported_docker_volume_drivers(self) -> frozenset[str]:
        """Return Docker volume driver names accepted by `DockerVolumeMountStrategy`."""

        return frozenset()

    def build_docker_volume_driver_config(
        self,
        strategy: DockerVolumeMountStrategy,
    ) -> tuple[str, dict[str, str], bool]:
        """Build the Docker volume driver tuple for Docker-volume mounts.

        Mount subclasses that do not support Docker volumes inherit this default unsupported
        implementation.
        """

        _ = strategy
        raise MountConfigError(
            message="docker-volume mounts are not supported for this mount type",
            context={"mount_type": self.type},
        )

    def _resolve_mount_path(
        self,
        session: BaseSandboxSession,
        dest: Path,
    ) -> Path:
        """Resolve the concrete path where this mount should appear in the active workspace."""

        manifest_root = posix_path_as_path(
            coerce_posix_path(getattr(session.state.manifest, "root", "/"))
        )
        return self._resolve_mount_path_for_root(manifest_root, dest)

    def _resolve_mount_path_for_root(
        self,
        manifest_root: Path,
        dest: Path,
    ) -> Path:
        """Resolve a mount path against an explicit manifest root.

        This helper is used both by live sessions and by container-creation code that only has the
        manifest root, not a started session.
        """

        if self.mount_path is not None:
            if (windows_path := windows_absolute_path(self.mount_path)) is not None:
                raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
            mount_posix = coerce_posix_path(self.mount_path)
            mount_path = posix_path_as_path(mount_posix)
            if mount_posix.is_absolute():
                return mount_path
            # Relative explicit mount paths are interpreted inside the active workspace root so a
            # manifest can stay portable across backends with different concrete root prefixes.
            return manifest_root / mount_path

        if dest.is_absolute():
            try:
                rel_dest = dest.relative_to(manifest_root)
            except ValueError:
                return dest
            # `dest` may already be normalized to an absolute workspace path; re-anchor it to the
            # current manifest root instead of nesting the root twice.
            return manifest_root / rel_dest
        return manifest_root / dest
