import abc
import inspect
import os
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    SerializeAsAny,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticSerializationError
from typing_extensions import assert_never

from .._config_coercion import coerce_pydantic_config
from ..util._asyncio_tasks import gather_with_cancel
from ._mount_security import (
    _mark_process_environment_error_safe,
    redact_mount_validation_error_data_sync,
)
from .entries import BaseEntry, Dir, Mount, resolve_workspace_path
from .errors import InvalidManifestPathError
from .manifest_render import render_manifest_description
from .types import Group, User
from .workspace_paths import (
    SandboxPathGrant,
    coerce_posix_path,
    posix_path_as_path,
    windows_absolute_path,
)

DEFAULT_REMOTE_MOUNT_COMMAND_ALLOWLIST = [
    "ls",
    "find",
    "stat",
    "cat",
    "less",
    "head",
    "tail",
    "du",
    "grep",
    "rg",
    "wc",
    "sort",
    "cut",
    "cp",
    "tee",
    "echo",
    "mkdir",
    "rm",
]

_MOUNT_CREDENTIAL_EXPOSURE_POLICY_KEYS = frozenset(
    {
        "in_container_mount_credential_exposure_allowed_paths",
        "_in_container_mount_credential_exposure_allowed_paths",
        "inContainerMountCredentialExposureAllowedPaths",
        "_inContainerMountCredentialExposureAllowedPaths",
        "in_container_mount_credential_exposure_acknowledged_paths",
        "_in_container_mount_credential_exposure_acknowledged_paths",
        "inContainerMountCredentialExposureAcknowledgedPaths",
        "_inContainerMountCredentialExposureAcknowledgedPaths",
        "in_container_mount_broad_credential_exposure_acknowledged_paths",
        "_in_container_mount_broad_credential_exposure_acknowledged_paths",
        "inContainerMountBroadCredentialExposureAcknowledgedPaths",
        "_inContainerMountBroadCredentialExposureAcknowledgedPaths",
        "mount_credential_exposure_policy",
        "_mount_credential_exposure_policy",
    }
)

_PROCESS_ENVIRONMENT_ACCESS_KEYS = frozenset(
    {
        "process_environment_access",
        "_process_environment_access",
        "processEnvironmentAccess",
        "_processEnvironmentAccess",
        "process_environment_allowed_names",
        "_process_environment_allowed_names",
        "processEnvironmentAllowedNames",
        "_processEnvironmentAllowedNames",
    }
)


@dataclass(frozen=True)
class _MountCredentialExposurePolicy:
    mount_scoped: frozenset[str] = frozenset()
    broad: frozenset[str] = frozenset()


EnvValueClass = type["EnvValue"]


# TODO (sdcoffey) env val from secret store
class EnvValue(BaseModel, abc.ABC):
    type: str = ""
    _subclass_registry: ClassVar[dict[str, EnvValueClass]] = {}

    @abc.abstractmethod
    async def resolve(self) -> str: ...

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: object) -> None:
        super().__pydantic_init_subclass__(**kwargs)

        annotations = inspect.get_annotations(cls)
        if "type" not in annotations:
            return

        type_field = cls.model_fields.get("type")
        type_default = type_field.default if type_field is not None else None
        if not isinstance(type_default, str) or type_default == "":
            return

        existing = EnvValue._subclass_registry.get(type_default)
        if existing is not None and existing is not cls:
            raise TypeError(
                f"env value type `{type_default}` is already registered by {existing.__name__}"
            )
        EnvValue._subclass_registry[type_default] = cls

    @classmethod
    def parse(cls, payload: object) -> "EnvValue":
        """Deserialize a mapping into the subclass registered under its `type` field.

        An existing `EnvValue` instance is returned unchanged.
        """
        if isinstance(payload, EnvValue):
            return payload
        if not isinstance(payload, Mapping):
            raise TypeError(
                f"env value must be an EnvValue or mapping, got {type(payload).__name__}"
            )

        value = payload.get("value")
        if set(payload) == {"value"} and isinstance(value, str):
            return StrEnvValue(value=value)

        env_value_type = payload.get("type")
        if not isinstance(env_value_type, str):
            raise ValueError("env value mapping must include a string `type` field")

        env_value_class = EnvValue._subclass_registry.get(env_value_type)
        if env_value_class is None:
            known = ", ".join(sorted(EnvValue._subclass_registry)) or "<none>"
            raise ValueError(
                f"Unknown env value type `{env_value_type}`. Registered types: {known}"
            )
        return env_value_class.model_validate(dict(payload))


class StrEnvValue(EnvValue):
    type: Literal["str"] = "str"
    value: str

    async def resolve(self) -> str:
        return self.value


class ProcessEnvValue(EnvValue):
    """References a value in the SDK process environment.

    The source name defaults to the containing environment mapping key. Process
    environment access is granted by trusted sandbox client runtime configuration,
    not by serialized manifest data.
    """

    type: Literal["process_env"] = "process_env"
    name: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_process_environment_name(value)
        return value

    async def resolve(self) -> str:
        raise _process_environment_error(
            "ProcessEnvValue must be resolved through a sandbox client with "
            "trusted process environment bindings"
        )


def _validate_process_environment_name(name: str) -> None:
    if not isinstance(name, str):
        raise TypeError("Process environment variable names must be strings.")
    if not name:
        raise ValueError("Process environment variable names must not be empty.")
    if "=" in name or "\x00" in name:
        raise ValueError("Process environment variable names must not contain '=' or NUL.")


def _normalize_process_environment_bindings(
    *,
    allowed_process_environment_keys: Iterable[str] = (),
    process_environment_bindings: Mapping[str, str] | None = None,
) -> frozenset[tuple[str, str]]:
    """Normalize trusted client configuration into exact destination/source bindings."""

    if isinstance(allowed_process_environment_keys, str):
        raise TypeError(
            "allowed_process_environment_keys must be an iterable of names, not a string."
        )
    normalized: dict[str, str] = {}
    for key in allowed_process_environment_keys:
        _validate_process_environment_name(key)
        normalized[key] = key
    for destination, source_name in (process_environment_bindings or {}).items():
        _validate_process_environment_name(destination)
        _validate_process_environment_name(source_name)
        existing_source = normalized.get(destination)
        if existing_source is not None and existing_source != source_name:
            raise ValueError(
                "Process environment client configuration has conflicting bindings for "
                f"destination {destination!r}"
            )
        normalized[destination] = source_name
    return frozenset(normalized.items())


def _process_environment_error(message: str) -> ValueError:
    error = ValueError(message)
    _mark_process_environment_error_safe(error)
    return error


def _serialize_env_value_with_type(value: EnvValue, serialized: object) -> dict[str, Any]:
    if EnvValue._subclass_registry.get(value.type) is not type(value):
        raise PydanticSerializationError(
            f"{type(value).__name__} must explicitly declare its own non-empty `type` "
            "to be serialized"
        )
    if not isinstance(serialized, Mapping):
        raise PydanticSerializationError(
            f"{type(value).__name__} serializer must return a mapping to preserve its `type`"
        )

    data = dict(serialized)
    data["type"] = value.type
    return data


class EnvEntry(BaseModel):
    description: str | None = None
    ephemeral: bool = Field(default=False)
    value: SerializeAsAny[EnvValue]

    @field_validator("value", mode="before")
    @classmethod
    def _parse_value(cls, value: object) -> EnvValue:
        return EnvValue.parse(value)

    @field_serializer("value", mode="wrap")
    def _serialize_value(self, value: EnvValue, handler: Any) -> dict[str, Any]:
        return _serialize_env_value_with_type(value, handler(value))


def _parse_environment_value(payload: object) -> "str | EnvValue | EnvEntry":
    """Route one environment member to the shape it represents."""
    if isinstance(payload, str | EnvValue | EnvEntry):
        return payload
    if not isinstance(payload, Mapping):
        raise TypeError(
            f"environment value must be a str, EnvValue, or EnvEntry, got {type(payload).__name__}"
        )
    if "type" in payload or isinstance(payload.get("value"), str):
        return EnvValue.parse(payload)
    return EnvEntry.model_validate(dict(payload))


class Environment(BaseModel):
    value: dict[str, str | SerializeAsAny[EnvValue] | EnvEntry] = Field(default_factory=dict)

    @field_validator("value", mode="before")
    @classmethod
    def _parse_value(cls, value: object) -> dict[str, "str | EnvValue | EnvEntry"]:
        if not isinstance(value, Mapping):
            raise ValueError(f"Environment mapping must be a mapping, got {type(value).__name__}")
        return {key: _parse_environment_value(entry) for key, entry in value.items()}

    @field_serializer("value", mode="wrap")
    def _serialize_value(
        self,
        values: dict[str, "str | EnvValue | EnvEntry"],
        handler: Any,
    ) -> dict[str, Any]:
        serialized = handler(values)
        if not isinstance(serialized, Mapping):
            raise PydanticSerializationError("Environment serializer must return a mapping")

        data = dict(serialized)
        for key, value in values.items():
            if isinstance(value, EnvValue) and key in data:
                data[key] = _serialize_env_value_with_type(value, data[key])
        return data

    def normalized(self) -> dict[str, EnvEntry]:
        result: dict[str, EnvEntry] = {}
        for key, value in self.value.items():
            match value:
                case str():
                    result[key] = EnvEntry(value=StrEnvValue(value=value))
                case EnvValue():
                    result[key] = EnvEntry(value=value)
                case EnvEntry():
                    result[key] = value
                case _:
                    assert_never(value)

        return result

    async def resolve(self) -> dict[str, str]:
        return await self._resolve(process_environment_access=frozenset())

    async def _resolve(
        self,
        *,
        process_environment_access: frozenset[tuple[str, str]],
        include_process_values: bool = True,
        include_non_process_values: bool = True,
    ) -> dict[str, str]:
        normalized = self.normalized()
        process_bindings = _validate_process_environment_bindings(
            normalized,
            process_environment_access=process_environment_access,
            require_values_present=include_process_values,
        )
        process_values = (
            {
                key: _read_process_environment_value(source_name)
                for key, source_name in process_bindings.items()
            }
            if include_process_values
            else {}
        )
        custom_keys = (
            [
                key
                for key, entry in normalized.items()
                if not isinstance(entry.value, ProcessEnvValue)
            ]
            if include_non_process_values
            else []
        )

        # `EnvValue` is an extension point, so these are user-supplied coroutines that
        # can reach a secret store or the network. A bare gather returns on the first
        # failure and leaves the rest running, which is how a rejected lookup ends up
        # with sibling fetches still in flight after the manifest has already failed.
        custom_values: tuple[str, ...] = ()
        resolved_custom_values: dict[str, str] = {}
        try:
            custom_values = await gather_with_cancel(
                *[normalized[key].value.resolve() for key in custom_keys]
            )
            resolved_custom_values = dict(zip(custom_keys, custom_values, strict=False))
            return {
                key: (
                    process_values[key] if key in process_bindings else resolved_custom_values[key]
                )
                for key in normalized
                if (include_process_values and key in process_bindings)
                or (include_non_process_values and key not in process_bindings)
            }
        except BaseException:
            custom_values = ()
            resolved_custom_values.clear()
            process_values.clear()
            raise


def _validate_process_environment_bindings(
    normalized: Mapping[str, EnvEntry],
    *,
    process_environment_access: frozenset[tuple[str, str]],
    require_values_present: bool = True,
) -> dict[str, str]:
    process_bindings: dict[str, str] = {}
    for key, entry in normalized.items():
        if not isinstance(entry.value, ProcessEnvValue):
            continue
        _validate_process_environment_name(key)
        source_name = entry.value.name if entry.value.name is not None else key
        _validate_process_environment_name(source_name)
        if (key, source_name) not in process_environment_access:
            raise _process_environment_error(
                f"Process environment binding {source_name!r} -> {key!r} is not granted; "
                "configure the sandbox client with an allowed process environment binding"
            )
        if require_values_present and source_name not in os.environ:
            raise _process_environment_error(
                f"Process environment variable {source_name!r} is not set"
            )
        process_bindings[key] = source_name
    return process_bindings


def _read_process_environment_value(source_name: str) -> str:
    try:
        return os.environ[source_name]
    except KeyError:
        raise _process_environment_error(
            f"Process environment variable {source_name!r} is not set"
        ) from None


class Manifest(BaseModel):
    version: Literal[1] = 1
    root: str = Field(default="/workspace")
    entries: dict[str | Path, BaseEntry] = Field(default_factory=dict)
    environment: Environment = Field(default_factory=Environment)
    users: list[User] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    extra_path_grants: tuple[SandboxPathGrant, ...] = Field(default_factory=tuple)
    remote_mount_command_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_REMOTE_MOUNT_COMMAND_ALLOWLIST)
    )
    _mount_credential_exposure_policy: _MountCredentialExposurePolicy = PrivateAttr(
        default_factory=_MountCredentialExposurePolicy
    )
    _process_environment_access: frozenset[tuple[str, str]] = PrivateAttr(default_factory=frozenset)

    def __getstate__(self) -> dict[Any, Any]:
        state = super().__getstate__()
        private_state = dict(state.get("__pydantic_private__") or {})
        private_state["_process_environment_access"] = frozenset()
        state["__pydantic_private__"] = private_state
        return state

    @model_validator(mode="before")
    @classmethod
    def _reject_mount_credential_exposure_policy_input(cls, value: object) -> object:
        if isinstance(value, Mapping):
            if _MOUNT_CREDENTIAL_EXPOSURE_POLICY_KEYS.intersection(value):
                raise TypeError(
                    "In-container mount credential exposure must be configured on a trusted "
                    "Manifest instance, not in manifest input."
                )
            if _PROCESS_ENVIRONMENT_ACCESS_KEYS.intersection(value):
                raise TypeError(
                    "Process environment access must be configured by trusted sandbox client "
                    "runtime configuration, not in manifest input."
                )
        return value

    @field_validator("entries", mode="before")
    @classmethod
    def _parse_entries(cls, value: object) -> dict[str | Path, BaseEntry]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"Artifact mapping must be a mapping, got {type(value).__name__}")
        return {key: BaseEntry.parse(entry) for key, entry in value.items()}

    @field_serializer("entries", when_used="json")
    def _serialize_entries(self, entries: Mapping[str | Path, BaseEntry]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, entry in entries.items():
            key_str = key.as_posix() if isinstance(key, Path) else str(key)
            out[key_str] = entry.model_dump(mode="json")
        return out

    def validated_entries(self) -> dict[str | Path, BaseEntry]:
        validated: dict[str | Path, BaseEntry] = dict(self.entries)
        for _path, _artifact in self.iter_entries():
            pass
        return validated

    def _with_process_environment_access(
        self,
        *bindings: frozenset[tuple[str, str]] | str | tuple[str, str],
    ) -> "Manifest":
        """Attach trusted client-owned process environment bindings at runtime."""

        declared_bindings = self._declared_process_environment_bindings()
        if len(bindings) == 1 and isinstance(bindings[0], frozenset):
            requested_bindings = bindings[0]
        else:
            same_name_keys = [binding for binding in bindings if isinstance(binding, str)]
            renamed_bindings = {
                binding[0]: binding[1]
                for binding in bindings
                if isinstance(binding, tuple) and len(binding) == 2
            }
            requested_bindings = _normalize_process_environment_bindings(
                allowed_process_environment_keys=same_name_keys,
                process_environment_bindings=renamed_bindings,
            )
        environment_values: dict[str, str | EnvValue | EnvEntry] = {}
        for key, value in self.environment.value.items():
            if isinstance(value, ProcessEnvValue):
                environment_values[key] = value.model_copy()
            elif isinstance(value, EnvEntry) and isinstance(value.value, ProcessEnvValue):
                environment_values[key] = value.model_copy(
                    update={"value": value.value.model_copy()}
                )
            else:
                environment_values[key] = value
        trusted = self.model_copy(
            update={
                "environment": self.environment.model_copy(update={"value": environment_values})
            }
        )
        trusted._process_environment_access = requested_bindings & declared_bindings
        return trusted

    async def resolve_environment(self) -> dict[str, str]:
        """Resolve the environment on a runtime-only copy bound by a trusted sandbox client."""

        return await self.environment._resolve(
            process_environment_access=self._process_environment_access
        )

    async def _resolve_environment_without_process_values(self) -> dict[str, str]:
        """Resolve non-process values without materializing protected process values."""

        return await self.environment._resolve(
            process_environment_access=self._process_environment_access,
            include_process_values=False,
        )

    async def _resolve_process_environment_values(self) -> dict[str, str]:
        """Resolve only protected process values for an out-of-band provider channel."""

        return await self.environment._resolve(
            process_environment_access=self._process_environment_access,
            include_non_process_values=False,
        )

    def _validate_process_environment_access(self) -> None:
        """Validate process environment references without returning their values."""

        _validate_process_environment_bindings(
            self.environment.normalized(),
            process_environment_access=self._process_environment_access,
        )

    def _snapshot_process_environment_values(self) -> dict[str, str]:
        """Snapshot protected process values before provider or resolver side effects."""

        normalized = self.environment.normalized()
        process_bindings = _validate_process_environment_bindings(
            normalized,
            process_environment_access=self._process_environment_access,
        )
        return {
            key: _read_process_environment_value(source_name)
            for key, source_name in process_bindings.items()
        }

    def _declared_process_environment_bindings(self) -> frozenset[tuple[str, str]]:
        return frozenset(
            (key, entry.value.name if entry.value.name is not None else key)
            for key, entry in self.environment.normalized().items()
            if isinstance(entry.value, ProcessEnvValue)
        )

    def _has_process_environment_values(self) -> bool:
        return bool(self._declared_process_environment_bindings())

    def _has_process_environment_access(self) -> bool:
        return bool(
            self._process_environment_access & self._declared_process_environment_bindings()
        )

    def _reject_process_environment_values(
        self,
        *,
        backend_id: str,
        supported_alternative: str,
    ) -> None:
        if not self._has_process_environment_values():
            return
        raise _process_environment_error(
            f"{backend_id} does not support ProcessEnvValue because it cannot transport "
            "protected values while enforcing the required host-environment isolation and "
            f"out-of-band provider boundary; {supported_alternative}"
        )

    @redact_mount_validation_error_data_sync
    def with_in_container_mount_credential_exposure_acknowledged(
        self, *mount_paths: str | PurePath
    ) -> "Manifest":
        """Acknowledge mount-scoped credential exposure for exact in-container mount paths.

        This trusted application-side policy is runtime-only and is not serialized.
        """

        return self._with_mount_credential_exposure_acknowledged(
            "mount_scoped",
            mount_paths,
        )

    @redact_mount_validation_error_data_sync
    def with_in_container_mount_broad_credential_exposure_acknowledged(
        self, *mount_paths: str | PurePath
    ) -> "Manifest":
        """Acknowledge broad credential exposure for exact in-container mount paths.

        Broad authority includes managed or workload identity and external credential files.
        This trusted application-side policy is runtime-only and is not serialized.
        """

        return self._with_mount_credential_exposure_acknowledged(
            "broad",
            mount_paths,
        )

    def _with_mount_credential_exposure_acknowledged(
        self,
        authority: Literal["mount_scoped", "broad"],
        mount_paths: tuple[str | PurePath, ...],
    ) -> "Manifest":
        if not mount_paths:
            raise TypeError("At least one in-container mount path is required.")

        acknowledged: set[str] = set()
        for path in mount_paths:
            key = self._mount_credential_exposure_policy_key(path, reject_root=True)
            assert key is not None
            acknowledged.add(key)
        from ._mount_security import _validate_manifest_mount_provenance

        _validate_manifest_mount_provenance(self)
        trusted = self.model_copy(deep=True)
        current = self._mount_credential_exposure_policy
        trusted._mount_credential_exposure_policy = _MountCredentialExposurePolicy(
            mount_scoped=(
                current.mount_scoped | acknowledged
                if authority == "mount_scoped"
                else current.mount_scoped
            ),
            broad=(current.broad | acknowledged if authority == "broad" else current.broad),
        )
        return trusted

    def _acknowledges_in_container_mount_credential_exposure(
        self,
        mount_path: str | PurePath,
        authority: Literal["mount_scoped", "broad"],
    ) -> bool:
        key = self._mount_credential_exposure_policy_key(mount_path, reject_root=False)
        if key is None:
            return False
        lookup_keys = {key}
        kind, _, path_text = key.partition(":")
        root = coerce_posix_path(self.root)
        root_normalized = PurePosixPath(
            "/",
            *[part for part in root.parts if part not in {"/", ""}],
        )
        if kind == "absolute":
            try:
                relative = PurePosixPath(path_text).relative_to(root_normalized)
            except ValueError:
                pass
            else:
                if relative.parts:
                    lookup_keys.add(f"relative:{relative.as_posix()}")
        else:
            absolute = root_normalized / PurePosixPath(path_text)
            lookup_keys.add(f"absolute:{absolute.as_posix()}")
        acknowledged = getattr(self._mount_credential_exposure_policy, authority)
        return not lookup_keys.isdisjoint(acknowledged)

    def _copy_mount_credential_exposure_policy_from(self, *sources: "Manifest") -> None:
        mount_scoped: set[str] = set()
        broad: set[str] = set()
        for source in sources:
            mount_scoped.update(source._mount_credential_exposure_policy.mount_scoped)
            broad.update(source._mount_credential_exposure_policy.broad)
        self._mount_credential_exposure_policy = _MountCredentialExposurePolicy(
            mount_scoped=frozenset(mount_scoped),
            broad=frozenset(broad),
        )

    def _merge_mount_credential_exposure_policy(
        self,
        policy: _MountCredentialExposurePolicy,
    ) -> _MountCredentialExposurePolicy:
        current = self._mount_credential_exposure_policy
        merged = _MountCredentialExposurePolicy(
            mount_scoped=current.mount_scoped | policy.mount_scoped,
            broad=current.broad | policy.broad,
        )
        self._mount_credential_exposure_policy = merged
        return merged

    def _mount_credential_exposure_policy_key(
        self,
        value: str | PurePath,
        *,
        reject_root: bool,
    ) -> str | None:
        text = value.as_posix() if isinstance(value, PurePath) else value
        if not text:
            if reject_root:
                raise ValueError("Mount credential exposure path must identify a non-root path.")
            return None
        if "\\" in text:
            raise ValueError("Mount credential exposure paths must use '/' separators.")
        if reject_root and any(character in text for character in "*?[]"):
            raise ValueError("Mount credential exposure paths must not contain wildcard syntax.")

        raw = PurePosixPath(text)
        if reject_root and ".." in raw.parts:
            raise ValueError("Mount credential exposure paths must not contain parent segments.")
        if not raw.is_absolute():
            rel = self._normalize_rel_path_within_root(
                posix_path_as_path(raw),
                original=posix_path_as_path(raw),
            )
            if not rel.parts:
                if reject_root:
                    raise ValueError(
                        "Mount credential exposure path must identify a non-root path."
                    )
                return None
            return f"relative:{coerce_posix_path(rel).as_posix()}"

        normalized_parts: list[str] = []
        for part in raw.parts:
            if part in {"", ".", "/"}:
                continue
            if part == "..":
                if normalized_parts:
                    normalized_parts.pop()
                continue
            normalized_parts.append(part)
        normalized = PurePosixPath("/", *normalized_parts)
        root = coerce_posix_path(self.root)
        root_normalized = PurePosixPath(
            "/",
            *[part for part in root.parts if part not in {"/", ""}],
        )
        if normalized == PurePosixPath("/") or normalized == root_normalized:
            if reject_root:
                raise ValueError("Mount credential exposure path must identify a non-root path.")
            return None
        return f"absolute:{normalized.as_posix()}"

    def ephemeral_entry_paths(self, depth: int | None = 1) -> set[Path]:
        _ = depth
        return {path for path, artifact in self.iter_entries() if artifact.ephemeral}

    def mount_targets(self) -> list[tuple[Mount, Path]]:
        root = posix_path_as_path(coerce_posix_path(self.root))
        mounts: list[tuple[Mount, Path]] = []
        for rel_path, artifact in self.iter_entries():
            if not isinstance(artifact, Mount):
                continue
            dest = resolve_workspace_path(root, rel_path)
            mount_path = artifact._resolve_mount_path_for_root(root, dest)
            normalized_mount_path = self._normalize_in_workspace_path(root, mount_path)
            if normalized_mount_path is not None:
                mount_path = normalized_mount_path
            mounts.append((artifact, mount_path))
        mounts.sort(key=lambda item: len(item[1].parts), reverse=True)
        return mounts

    def ephemeral_mount_targets(self) -> list[tuple[Mount, Path]]:
        return [(artifact, path) for artifact, path in self.mount_targets() if artifact.ephemeral]

    def ephemeral_persistence_paths(self, depth: int | None = 1) -> set[Path]:
        _ = depth
        root = posix_path_as_path(coerce_posix_path(self.root))
        skip = self.ephemeral_entry_paths(depth=depth)
        for _mount, mount_path in self.ephemeral_mount_targets():
            try:
                rel_mount_path = mount_path.relative_to(root)
            except ValueError:
                continue
            if rel_mount_path.parts:
                skip.add(rel_mount_path)
        return skip

    @staticmethod
    def _coerce_rel_path(path: str | PurePath) -> Path:
        if (windows_path := windows_absolute_path(path)) is not None:
            raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
        return posix_path_as_path(coerce_posix_path(path))

    @staticmethod
    def _validate_rel_path(rel: Path) -> None:
        if (windows_path := windows_absolute_path(rel)) is not None:
            raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
        rel_path = coerce_posix_path(rel)
        if rel_path.is_absolute():
            raise InvalidManifestPathError(rel=rel_path.as_posix(), reason="absolute")
        if ".." in rel_path.parts:
            raise InvalidManifestPathError(rel=rel_path.as_posix(), reason="escape_root")

    @staticmethod
    def _normalize_rel_path_within_root(rel: Path, *, original: Path) -> Path:
        rel_path = coerce_posix_path(rel)
        original_path = coerce_posix_path(original)
        if (windows_path := windows_absolute_path(original)) is not None:
            raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
        if rel_path.is_absolute():
            raise InvalidManifestPathError(rel=original_path.as_posix(), reason="absolute")

        normalized_parts: list[str] = []
        for part in rel_path.parts:
            if part in ("", "."):
                continue
            if part == "..":
                if not normalized_parts:
                    raise InvalidManifestPathError(
                        rel=original_path.as_posix(), reason="escape_root"
                    )
                normalized_parts.pop()
                continue
            normalized_parts.append(part)

        return posix_path_as_path(PurePosixPath(*normalized_parts))

    @classmethod
    def _normalize_in_workspace_path(cls, root: Path, path: Path) -> Path | None:
        root_path = coerce_posix_path(root)
        if (windows_path := windows_absolute_path(path)) is not None:
            raise InvalidManifestPathError(rel=windows_path.as_posix(), reason="absolute")
        path_posix = coerce_posix_path(path)
        if not path_posix.is_absolute():
            normalized_rel = cls._normalize_rel_path_within_root(
                posix_path_as_path(path_posix),
                original=posix_path_as_path(path_posix),
            )
            return root / normalized_rel if normalized_rel.parts else root

        try:
            rel_path = path_posix.relative_to(root_path)
        except ValueError:
            return None

        normalized_rel = cls._normalize_rel_path_within_root(
            posix_path_as_path(rel_path),
            original=posix_path_as_path(path_posix),
        )
        root_as_path = posix_path_as_path(root_path)
        return root_as_path / normalized_rel if normalized_rel.parts else root_as_path

    def iter_entries(self) -> Iterator[tuple[Path, BaseEntry]]:
        stack = [
            (self._coerce_rel_path(path), artifact)
            for path, artifact in reversed(list(self.entries.items()))
        ]
        while stack:
            rel_path, artifact = stack.pop()
            self._validate_rel_path(rel_path)
            yield rel_path, artifact
            if not isinstance(artifact, Dir):
                continue

            for child_name, child_artifact in reversed(list(artifact.children.items())):
                child_rel_path = rel_path / self._coerce_rel_path(child_name)
                stack.append((child_rel_path, child_artifact))

    def describe(self, depth: int | None = 1) -> str:
        """
        print a nice fs representation of things inside root with inline descriptions
        depth controls how deep the tree is rendered; None renders all levels
        eg:

        /workspace                      (root)
        ├── repo/                       # /workspace/repo — my repo
        │   └── README.md               # /workspace/repo/README.md
        ├── data/                       # /workspace/data
        │   └── config.json             # /workspace/data/config.json — config
        ├── mount-data/                 # /workspace/mount-data (mount)
        └── notes.txt                   # /workspace/notes.txt
        ...
        """
        return render_manifest_description(
            root=self.root,
            entries=self.validated_entries(),
            coerce_rel_path=self._coerce_rel_path,
            depth=depth,
        )


def _coerce_manifest(value: Manifest | dict[str, Any], *, parameter_name: str) -> Manifest:
    """Normalize manifest dictionaries without granting untrusted host filesystem access."""
    if isinstance(value, dict) and "extra_path_grants" in value:
        extra_path_grants = value["extra_path_grants"]
        if not isinstance(extra_path_grants, list | tuple) or extra_path_grants:
            raise TypeError(
                f"{parameter_name}.extra_path_grants must be configured on a trusted "
                "Manifest instance, not in a dictionary"
            )
    return coerce_pydantic_config(value, Manifest, parameter_name=parameter_name)
