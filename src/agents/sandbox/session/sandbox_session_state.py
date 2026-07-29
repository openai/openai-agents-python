from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, ClassVar, Literal, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializeAsAny,
    field_validator,
    model_serializer,
)

from ..manifest import Manifest
from ..snapshot import SnapshotBase

SessionStateClass = type["SandboxSessionState"]
REDACTED_HOST_PATH_GRANT_PATHS_KEY = "__openai_agents_redacted_host_path_grant_paths"


class SandboxSessionState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    type: str
    session_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    snapshot: SerializeAsAny[SnapshotBase]
    manifest: Manifest
    exposed_ports: tuple[int, ...] = Field(default_factory=tuple)
    snapshot_fingerprint: str | None = None
    snapshot_fingerprint_version: str | None = None
    workspace_root_ready: bool = False

    _subclass_registry: ClassVar[dict[str, SessionStateClass]] = {}
    _path_grants_require_rebind: tuple[str, ...] = PrivateAttr(default=())

    @property
    def path_grants_require_rebind(self) -> tuple[str, ...]:
        return self._path_grants_require_rebind

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Auto-register every subclass by its ``type`` field default."""
        super().__pydantic_init_subclass__(**kwargs)

        type_field = cls.model_fields.get("type")
        if type_field is None:
            return

        annotation = type_field.annotation
        if get_origin(annotation) is not Literal:
            return

        args = get_args(annotation)
        if not args:
            return

        type_default = type_field.default
        if not isinstance(type_default, str) or type_default == "":
            return

        SandboxSessionState._subclass_registry[type_default] = cls

    @classmethod
    def parse(cls, payload: object) -> SandboxSessionState:
        """Deserialize *payload* into the correct registered subclass.

        Accepts a ``SandboxSessionState`` instance (returned as-is if already a
        subclass, or upgraded via ``model_dump`` -> registry lookup if it is a
        bare base instance) or a plain ``dict``.
        """
        if isinstance(payload, SandboxSessionState):
            if type(payload) is not SandboxSessionState:
                return payload
            payload = payload.model_dump()

        if isinstance(payload, dict):
            state_type = payload.get("type")
            if not isinstance(state_type, str):
                raise ValueError("sandbox session state payload must include a string `type`")

            subclass = SandboxSessionState._subclass_registry.get(state_type)
            if subclass is None:
                raise ValueError(f"unknown sandbox session state type `{state_type}`")

            return cls._mark_persisted_path_grants(
                subclass.model_validate(payload),
                payload=payload,
            )

        raise TypeError("session state payload must be a SandboxSessionState or dict")

    @classmethod
    def _mark_persisted_path_grants(
        cls,
        state: SandboxSessionState,
        *,
        payload: dict[str, object],
    ) -> SandboxSessionState:
        redacted_value = payload.get(REDACTED_HOST_PATH_GRANT_PATHS_KEY)
        marker_paths = (
            tuple(path for path in redacted_value if isinstance(path, str))
            if isinstance(redacted_value, list | tuple)
            else ()
        )
        serialized_host_path_grant_paths = tuple(
            grant.path for grant in state.manifest.extra_path_grants if grant.host_path is not None
        )
        persistent_grants = tuple(
            grant for grant in state.manifest.extra_path_grants if grant.host_path is None
        )
        sanitized_manifest = state.manifest.model_copy(
            update={"extra_path_grants": persistent_grants},
        )
        marked = state.model_copy(update={"manifest": sanitized_manifest})
        marked._path_grants_require_rebind = tuple(
            dict.fromkeys(
                (
                    *state.path_grants_require_rebind,
                    *marker_paths,
                    *serialized_host_path_grant_paths,
                )
            )
        )
        return marked

    def rebind_persisted_path_grants(
        self,
        trusted_manifest: Manifest | None,
    ) -> SandboxSessionState:
        """Replace persisted path grants with grants from current trusted configuration."""

        if not self.path_grants_require_rebind:
            return self
        if trusted_manifest is None:
            raise ValueError(
                "Sandbox session state contains path grants that require a current trusted "
                "manifest before resume"
            )

        trusted_host_path_grant_paths = {
            grant.path
            for grant in trusted_manifest.extra_path_grants
            if grant.host_path is not None
        }
        missing_host_paths = [
            path
            for path in self.path_grants_require_rebind
            if path not in trusted_host_path_grant_paths
        ]
        if missing_host_paths:
            raise ValueError(
                "Sandbox session state requires current trusted host_path values for these "
                f"path grants: {', '.join(missing_host_paths)}"
            )

        rebound_manifest = self.manifest.model_copy(
            update={
                "extra_path_grants": tuple(
                    grant.model_copy() for grant in trusted_manifest.extra_path_grants
                )
            },
        )
        rebound = self.model_copy(update={"manifest": rebound_manifest})
        rebound._path_grants_require_rebind = ()
        return rebound

    def assert_path_grants_rebound(self) -> None:
        if not self.path_grants_require_rebind:
            return
        raise ValueError(
            "Sandbox session state path grants must be rebound from a current trusted manifest "
            "before resume; resume through Runner with SandboxRunConfig.manifest"
        )

    @model_serializer(mode="wrap")
    def _serialize_always_include_defaults(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.type:
            data["type"] = self.type
        if self.session_id:
            data["session_id"] = self.session_id
        return data

    @field_validator("snapshot", mode="before")
    @classmethod
    def _coerce_snapshot(cls, value: object) -> SnapshotBase:
        return SnapshotBase.parse(value)

    @field_validator("exposed_ports", mode="before")
    @classmethod
    def _coerce_exposed_ports(cls, value: object) -> tuple[int, ...]:
        if value is None:
            return ()
        if isinstance(value, int):
            ports: Iterable[object] = (value,)
        elif isinstance(value, Iterable) and not isinstance(value, str | bytes | bytearray):
            ports = value
        else:
            raise TypeError("exposed_ports must be an iterable of TCP port integers")

        normalized: list[int] = []
        seen: set[int] = set()
        for port in ports:
            if not isinstance(port, int):
                raise TypeError("exposed_ports must contain integers")
            if port < 1 or port > 65535:
                raise ValueError("exposed_ports entries must be between 1 and 65535")
            if port in seen:
                continue
            seen.add(port)
            normalized.append(port)
        return tuple(normalized)
