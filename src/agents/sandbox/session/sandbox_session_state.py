from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, ClassVar, Literal, cast, get_args, get_origin

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializeAsAny,
    field_validator,
    model_serializer,
    model_validator,
)
from typing_extensions import Self

from .._mount_security import (
    redact_mount_error_data_sync,
)
from ..manifest import Manifest
from ..snapshot import SnapshotBase

SessionStateClass = type["SandboxSessionState"]
REDACTED_HOST_PATH_GRANT_PATHS_KEY = "__openai_agents_redacted_host_path_grant_paths"


class SandboxSessionState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, hide_input_in_errors=True)
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
    _mount_authority_redacted: bool = PrivateAttr(default=False)
    _mount_authority_rebound: bool = PrivateAttr(default=False)

    @property
    def path_grants_require_rebind(self) -> tuple[str, ...]:
        return self._path_grants_require_rebind

    @property
    def mount_authority_redacted(self) -> bool:
        return self._mount_authority_redacted

    @property
    def mount_authority_rebound(self) -> bool:
        """Whether persisted mount topology was rebound from current trusted configuration."""

        return self._mount_authority_rebound

    def _sanitize_persisted_provider_identity(
        self,
        data: dict[str, Any],
        *,
        mount_authority_redacted: bool,
    ) -> None:
        """Remove provider identity when this state cannot safely reconnect it."""

        _ = (data, mount_authority_redacted)

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
            from ...exceptions import (
                _raise_data_redacted_error,
                _replace_data_redacted_process_control_error,
            )
            from .._mount_security import (
                _redact_mount_state_validation_error,
                sanitize_raw_session_state_mount_authority,
            )

            safe_error: BaseException | None = None
            sanitized: object = None
            state_type: object = None
            subclass: SessionStateClass | None = None
            try:
                sanitized, _redacted = sanitize_raw_session_state_mount_authority(payload)
                if not isinstance(sanitized, dict):
                    raise ValueError("sandbox session state payload has an invalid shape")
                payload = sanitized
                state_type = payload.get("type")
                if not isinstance(state_type, str):
                    raise ValueError("sandbox session state payload must include a string `type`")

                subclass = SandboxSessionState._subclass_registry.get(state_type)
                if subclass is None:
                    raise ValueError("unknown sandbox session state type")

                return cls._mark_persisted_path_grants(
                    subclass.model_validate(payload),
                    payload=payload,
                )
            except BaseException as error:
                payload.clear()
                if isinstance(sanitized, dict):
                    sanitized.clear()
                safe_error = _replace_data_redacted_process_control_error(error)
                if safe_error is None:
                    safe_error = _redact_mount_state_validation_error(
                        error,
                        message="sandbox session state payload is invalid",
                    )

            payload = cast(Any, None)
            sanitized = None
            state_type = None
            subclass = None
            assert safe_error is not None
            _raise_data_redacted_error(safe_error)

        payload = cast(Any, None)
        raise TypeError("session state payload must be a SandboxSessionState or dict")

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Literal["allow", "ignore", "forbid"] | None = None,
        context: Any | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Validate JSON without retaining malformed input in a public error."""

        from ...exceptions import (
            _raise_data_redacted_error,
            _replace_data_redacted_process_control_error,
        )
        from .._mount_security import _redact_mount_state_validation_error

        decoded: object = None
        safe_error: BaseException | None = None
        try:
            decoded = json.loads(json_data)
        except BaseException as error:
            safe_error = _replace_data_redacted_process_control_error(error)
            if safe_error is None:
                safe_error = _redact_mount_state_validation_error(
                    error,
                    message="sandbox session state JSON is invalid",
                )

        if safe_error is not None:
            if isinstance(decoded, dict | list):
                decoded.clear()
            decoded = None
            json_data = cast(Any, None)
            _raise_data_redacted_error(safe_error)

        decoded = None
        try:
            return super().model_validate_json(
                json_data,
                strict=strict,
                extra=extra,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except BaseException as error:
            safe_error = _replace_data_redacted_process_control_error(error)
            json_data = cast(Any, None)
            if safe_error is not None:
                _raise_data_redacted_error(safe_error)
            if isinstance(error, Exception):
                raise
            safe_error = _redact_mount_state_validation_error(
                error,
                message="sandbox session state JSON is invalid",
            )
            _raise_data_redacted_error(safe_error)

    @classmethod
    def _mark_persisted_path_grants(
        cls,
        state: SandboxSessionState,
        *,
        payload: dict[str, object],
    ) -> SandboxSessionState:
        from .._mount_security import REDACTED_MOUNT_AUTHORITY_KEY

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
        marked._mount_authority_redacted = bool(
            state.mount_authority_redacted or payload.get(REDACTED_MOUNT_AUTHORITY_KEY) is True
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

    @redact_mount_error_data_sync
    def rebind_persisted_mount_authority(
        self,
        trusted_manifest: Manifest | None,
        *,
        provider_backend_id: str,
    ) -> SandboxSessionState:
        """Restore redacted mount authority from an exact current trusted manifest."""

        if not self.mount_authority_redacted:
            return self
        if trusted_manifest is None:
            raise ValueError(
                "Sandbox session state contains redacted cloud mount credentials and requires "
                "a current trusted manifest before resume"
            )

        from .._mount_security import rebind_manifest_mount_authority

        rebound_manifest = rebind_manifest_mount_authority(
            self.manifest,
            trusted_manifest,
            provider_backend_id=provider_backend_id,
        )
        rebound = self.model_copy(update={"manifest": rebound_manifest})
        rebound._mount_authority_redacted = False
        rebound._mount_authority_rebound = True
        return rebound

    @redact_mount_error_data_sync
    def assert_path_grants_rebound(self) -> None:
        from .._mount_security import validate_manifest_mount_credential_boundaries

        validate_manifest_mount_credential_boundaries(
            self.manifest,
            provider_backend_id=self.type,
        )

        if self.mount_authority_redacted:
            raise ValueError(
                "Sandbox session state with cloud mount credentials cannot be resumed; "
                "resume through Runner with the current trusted manifest"
            )
        if not self.path_grants_require_rebind:
            return
        raise ValueError(
            "Sandbox session state path grants must be rebound from a current trusted manifest "
            "before resume; resume through Runner with SandboxRunConfig.manifest"
        )

    @model_serializer(mode="wrap")
    def _serialize_always_include_defaults(self, handler: Any) -> dict[str, Any]:
        from ...exceptions import _raise_data_redacted_error
        from .._mount_security import (
            REDACTED_MOUNT_AUTHORITY_KEY,
            _manifest_has_configured_mount_authority,
            _manifest_mount_provenance_error,
            _mark_mount_validation_error,
            _redact_mount_serialization_error,
            sanitize_raw_manifest_mount_authority,
        )

        data: dict[str, Any] | None = None
        safe_error: BaseException | None = None
        provenance_error = _manifest_mount_provenance_error(self.manifest)
        if provenance_error is not None:
            _mark_mount_validation_error(provenance_error)
            safe_error = provenance_error
        else:
            try:
                data = handler(self)
                sanitized_manifest, mount_authority_redacted = (
                    sanitize_raw_manifest_mount_authority(
                        cast(dict[str, Any], data).get("manifest")
                    )
                )
            except BaseException as error:
                if not _manifest_has_configured_mount_authority(self.manifest):
                    raise
                safe_error = _redact_mount_serialization_error(error)

        if safe_error is not None:
            data = None
            self = cast(Any, None)
            _raise_data_redacted_error(safe_error)

        assert data is not None
        if "manifest" in data:
            data["manifest"] = sanitized_manifest
        requires_mount_authority_rebind = mount_authority_redacted or self.mount_authority_redacted
        if requires_mount_authority_rebind:
            data[REDACTED_MOUNT_AUTHORITY_KEY] = True
        if self.type:
            data["type"] = self.type
        if self.session_id:
            data["session_id"] = self.session_id
        self._sanitize_persisted_provider_identity(
            data,
            mount_authority_redacted=requires_mount_authority_rebind,
        )
        return data

    @model_validator(mode="wrap")
    @classmethod
    def _restore_mount_authority_marker(cls, value: Any, handler: Any) -> SandboxSessionState:
        from ...exceptions import (
            _raise_data_redacted_error,
            _replace_data_redacted_process_control_error,
        )
        from .._mount_security import (
            REDACTED_MOUNT_AUTHORITY_KEY,
            _manifest_has_configured_mount_authority,
            _redact_mount_state_validation_error,
            sanitize_raw_session_state_mount_authority,
        )

        marker = False
        state: SandboxSessionState | None = None
        sanitized: object = None
        safe_error: BaseException | None = None
        redact_failure = True
        try:
            if isinstance(value, Mapping):
                marker = value.get(REDACTED_MOUNT_AUTHORITY_KEY) is True
            if (
                isinstance(value, Mapping)
                and "manifest" in value
                and not isinstance(value.get("manifest"), Manifest)
            ):
                sanitized, redacted = sanitize_raw_session_state_mount_authority(value)
                marker = marker or redacted
                redact_failure = marker
                state = handler(sanitized)
            else:
                manifest = value.get("manifest") if isinstance(value, Mapping) else None
                redact_failure = marker or (
                    isinstance(manifest, Manifest)
                    and _manifest_has_configured_mount_authority(manifest)
                )
                state = handler(value)
        except BaseException as error:
            safe_error = _replace_data_redacted_process_control_error(error)
            if safe_error is None and not redact_failure:
                raise
            if isinstance(value, dict):
                value.clear()
            if isinstance(sanitized, dict):
                sanitized.clear()
            if safe_error is None:
                safe_error = _redact_mount_state_validation_error(
                    error,
                    message="sandbox session state payload is invalid",
                )

        if safe_error is not None:
            value = cast(Any, None)
            sanitized = None
            _raise_data_redacted_error(safe_error)

        assert state is not None
        if marker:
            state._mount_authority_redacted = True
        return state

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
