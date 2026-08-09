import abc
import inspect
from collections.abc import Iterator, Mapping
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
from ._mount_security import redact_mount_validation_error_data_sync
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
        normalized = self.normalized()
        keys = normalized.keys()
        # `EnvValue` is an extension point, so these are user-supplied coroutines that
        # can reach a secret store or the network. A bare gather returns on the first
        # failure and leaves the rest running, which is how a rejected lookup ends up
        # with sibling fetches still in flight after the manifest has already failed.
        values = await gather_with_cancel(*[normalized[key].value.resolve() for key in keys])
        return dict(zip(keys, values, strict=False))


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

    @model_validator(mode="before")
    @classmethod
    def _reject_mount_credential_exposure_policy_input(cls, value: object) -> object:
        if isinstance(value, Mapping) and _MOUNT_CREDENTIAL_EXPOSURE_POLICY_KEYS.intersection(
            value
        ):
            raise TypeError(
                "In-container mount credential exposure must be configured on a trusted "
                "Manifest instance, not in manifest input."
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
