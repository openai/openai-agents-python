import abc
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from pydantic import Field, ValidationError

from agents.sandbox.entries import (
    Dir,
    File,
    GCSMount,
    InContainerMountStrategy,
    MountpointMountPattern,
)
from agents.sandbox.errors import InvalidManifestPathError
from agents.sandbox.manifest import EnvEntry, Environment, EnvValue, Manifest, StrEnvValue
from agents.sandbox.manifest_render import _truncate_manifest_description


class _ReferenceEnvValue(EnvValue):
    type: Literal["test-reference"] = "test-reference"
    secret_name: str

    async def resolve(self) -> str:
        return f"resolved-{self.secret_name}"


class _JoinedEnvValue(EnvValue):
    type: Literal["test-joined"] = "test-joined"
    parts: list[str]

    async def resolve(self) -> str:
        return "-".join(self.parts)


@pytest.fixture
def restore_env_value_registry() -> Iterator[None]:
    registered = dict(EnvValue._subclass_registry)
    yield
    EnvValue._subclass_registry.clear()
    EnvValue._subclass_registry.update(registered)


def test_manifest_rejects_nested_child_paths_that_escape_workspace() -> None:
    manifest = Manifest(
        entries={
            "safe": Dir(
                children={
                    "../outside.txt": File(content=b"nope"),
                }
            )
        }
    )

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        manifest.validated_entries()


def test_manifest_rejects_nested_absolute_child_paths() -> None:
    manifest = Manifest(
        entries={
            "safe": Dir(
                children={
                    "/tmp/outside.txt": File(content=b"nope"),
                }
            )
        }
    )

    with pytest.raises(InvalidManifestPathError, match="must be relative"):
        manifest.validated_entries()


def test_manifest_rejects_windows_drive_absolute_entry_paths() -> None:
    manifest = Manifest(entries={"C:\\tmp\\outside.txt": File(content=b"nope")})

    with pytest.raises(InvalidManifestPathError) as exc_info:
        manifest.validated_entries()

    assert str(exc_info.value) == "manifest path must be relative: C:/tmp/outside.txt"
    assert exc_info.value.context == {"rel": "C:/tmp/outside.txt", "reason": "absolute"}


def test_manifest_ephemeral_entry_paths_include_nested_children() -> None:
    manifest = Manifest(
        entries={
            "dir": Dir(
                children={
                    "keep.txt": File(content=b"keep"),
                    "tmp.txt": File(content=b"tmp", ephemeral=True),
                }
            )
        }
    )

    assert manifest.ephemeral_entry_paths() == {Path("dir/tmp.txt")}


def test_manifest_ephemeral_persistence_paths_include_resolved_mount_targets() -> None:
    manifest = Manifest(
        root="/workspace",
        entries={
            "logical": GCSMount(
                bucket="bucket",
                mount_path=Path("actual"),
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
            "dir": Dir(
                children={
                    "tmp.txt": File(content=b"tmp", ephemeral=True),
                }
            ),
        },
    )

    assert manifest.ephemeral_persistence_paths() == {
        Path("logical"),
        Path("actual"),
        Path("dir/tmp.txt"),
    }


def test_manifest_ephemeral_mount_targets_sort_by_resolved_depth() -> None:
    parent = GCSMount(
        bucket="parent",
        mount_path=Path("repo"),
        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
    )
    child = GCSMount(
        bucket="child",
        mount_path=Path("repo/sub"),
        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
    )
    manifest = Manifest(
        root="/workspace",
        entries={
            "parent": parent,
            "nested": Dir(children={"child": child}),
        },
    )

    assert manifest.ephemeral_mount_targets() == [
        (child, Path("/workspace/repo/sub")),
        (parent, Path("/workspace/repo")),
    ]


def test_manifest_ephemeral_mount_targets_normalize_non_escaping_mount_paths() -> None:
    mount = GCSMount(
        bucket="bucket",
        mount_path=Path("/workspace/repo/../actual"),
        mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
    )
    manifest = Manifest(root="/workspace", entries={"logical": mount})

    assert manifest.ephemeral_mount_targets() == [
        (mount, Path("/workspace/actual")),
    ]
    assert manifest.ephemeral_persistence_paths() == {
        Path("logical"),
        Path("actual"),
    }


def test_manifest_ephemeral_mount_targets_reject_escaping_mount_paths() -> None:
    manifest = Manifest(
        root="/workspace",
        entries={
            "logical": GCSMount(
                bucket="bucket",
                mount_path=Path("/workspace/../../tmp"),
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        },
    )

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        manifest.ephemeral_mount_targets()

    with pytest.raises(InvalidManifestPathError, match="must not escape root"):
        manifest.ephemeral_persistence_paths()


def test_manifest_ephemeral_mount_targets_reject_windows_drive_mount_path() -> None:
    manifest = Manifest(
        root="/workspace",
        entries={
            "logical": GCSMount(
                bucket="bucket",
                mount_path=Path("C:\\tmp\\mount"),
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        },
    )

    with pytest.raises(InvalidManifestPathError) as exc_info:
        manifest.ephemeral_mount_targets()

    assert str(exc_info.value) == "manifest path must be relative: C:/tmp/mount"
    assert exc_info.value.context == {"rel": "C:/tmp/mount", "reason": "absolute"}


def test_manifest_describe_preserves_tree_rendering_after_renderer_extract() -> None:
    manifest = Manifest(
        root="/workspace",
        entries={
            "repo": Dir(
                description="project root",
                children={
                    "README.md": File(content=b"hi", description="overview"),
                },
            ),
            "data": GCSMount(
                bucket="bucket",
                description="shared data",
                mount_strategy=InContainerMountStrategy(pattern=MountpointMountPattern()),
            ),
        },
    )

    description = manifest.describe(depth=2)

    assert description.startswith("/workspace\n")
    assert "data/" in description
    assert "/workspace/data" in description
    assert "repo/" in description
    assert "/workspace/repo/README.md" in description


def test_manifest_description_truncation_respects_short_limits() -> None:
    description = "0123456789" * 20

    for max_chars in range(0, 40):
        truncated = _truncate_manifest_description(description, max_chars)
        assert len(truncated) <= max_chars


def test_manifest_description_truncation_preserves_unbounded_description() -> None:
    description = "short"

    assert _truncate_manifest_description(description, None) == description


def test_manifest_roundtrips_custom_env_value_subclass() -> None:
    manifest = Manifest(
        environment=Environment(value={"TOKEN": _ReferenceEnvValue(secret_name="token")})
    )

    payload = manifest.model_dump(mode="json")

    assert payload["environment"] == {
        "value": {"TOKEN": {"type": "test-reference", "secret_name": "token"}}
    }

    parsed = Manifest.model_validate(payload)
    parsed_value = parsed.environment.value["TOKEN"]

    assert isinstance(parsed_value, _ReferenceEnvValue)
    assert parsed_value.secret_name == "token"


def test_manifest_roundtrips_custom_env_value_inside_env_entry() -> None:
    manifest = Manifest(
        environment=Environment(
            value={
                "TOKEN": EnvEntry(
                    description="api token",
                    ephemeral=True,
                    value=_ReferenceEnvValue(secret_name="token"),
                )
            }
        )
    )

    payload = manifest.model_dump(mode="json")

    assert payload["environment"] == {
        "value": {
            "TOKEN": {
                "description": "api token",
                "ephemeral": True,
                "value": {"type": "test-reference", "secret_name": "token"},
            }
        }
    }

    parsed = Manifest.model_validate(payload)
    parsed_entry = parsed.environment.value["TOKEN"]

    assert isinstance(parsed_entry, EnvEntry)
    assert parsed_entry.description == "api token"
    assert parsed_entry.ephemeral is True
    assert isinstance(parsed_entry.value, _ReferenceEnvValue)
    assert parsed_entry.value.secret_name == "token"


def test_manifest_roundtrips_distinct_env_value_subclasses_together() -> None:
    manifest = Manifest(
        environment=Environment(
            value={
                "TOKEN": _ReferenceEnvValue(secret_name="token"),
                "PATH_PARTS": _JoinedEnvValue(parts=["a", "b"]),
                "ENTRY": EnvEntry(value=_JoinedEnvValue(parts=["c", "d"])),
            }
        )
    )

    parsed = Manifest.model_validate(manifest.model_dump(mode="json"))

    assert parsed.environment.value == {
        "TOKEN": _ReferenceEnvValue(secret_name="token"),
        "PATH_PARTS": _JoinedEnvValue(parts=["a", "b"]),
        "ENTRY": EnvEntry(value=_JoinedEnvValue(parts=["c", "d"])),
    }


def test_manifest_roundtrips_str_env_values() -> None:
    manifest = Manifest(
        environment=Environment(value={"PLAIN": "plain", "TYPED": StrEnvValue(value="typed")})
    )

    payload = manifest.model_dump(mode="json")

    assert payload["environment"] == {
        "value": {"PLAIN": "plain", "TYPED": {"type": "str", "value": "typed"}}
    }

    parsed = Manifest.model_validate(payload)

    assert parsed.environment.value["PLAIN"] == "plain"
    assert parsed.environment.value["TYPED"] == StrEnvValue(value="typed")


@pytest.mark.parametrize(("exclude_unset", "exclude_defaults"), [(True, False), (False, True)])
def test_manifest_env_value_type_survives_narrowed_dumps(
    exclude_unset: bool,
    exclude_defaults: bool,
) -> None:
    manifest = Manifest(
        environment=Environment(
            value={
                "TOKEN": _ReferenceEnvValue(secret_name="token"),
                "ENTRY": EnvEntry(value=StrEnvValue(value="typed")),
            }
        )
    )

    payload = manifest.model_dump(
        mode="json",
        exclude_unset=exclude_unset,
        exclude_defaults=exclude_defaults,
    )
    environment = payload["environment"]["value"]

    assert environment["TOKEN"]["type"] == "test-reference"
    assert environment["ENTRY"]["value"]["type"] == "str"

    parsed = Manifest.model_validate(payload)

    assert parsed.environment.value == manifest.environment.value


@pytest.mark.asyncio
async def test_manifest_env_values_resolve_after_round_trip() -> None:
    manifest = Manifest(
        environment=Environment(
            value={
                "DIRECT": _ReferenceEnvValue(secret_name="direct"),
                "ENTRY": EnvEntry(value=_ReferenceEnvValue(secret_name="entry")),
            }
        )
    )

    parsed = Manifest.model_validate(manifest.model_dump(mode="json"))

    assert await parsed.environment.resolve() == {
        "DIRECT": "resolved-direct",
        "ENTRY": "resolved-entry",
    }


@pytest.mark.asyncio
async def test_manifest_env_resolution_does_not_persist_resolved_values() -> None:
    manifest = Manifest(
        environment=Environment(value={"TOKEN": _ReferenceEnvValue(secret_name="token")})
    )

    assert await manifest.environment.resolve() == {"TOKEN": "resolved-token"}

    assert manifest.environment.value == {"TOKEN": _ReferenceEnvValue(secret_name="token")}
    assert "resolved-token" not in json.dumps(manifest.model_dump(mode="json"))


def test_env_entry_has_no_type_field() -> None:
    # Environment members are routed by the presence of `type`, so an EnvEntry never carries one.
    assert "type" not in EnvEntry.model_fields


def test_manifest_rejects_unknown_env_value_type() -> None:
    payload = {"environment": {"value": {"TOKEN": {"type": "test-unregistered"}}}}

    with pytest.raises(ValueError, match="Unknown env value type `test-unregistered`"):
        Manifest.model_validate(payload)


def test_manifest_rejects_malformed_env_values() -> None:
    with pytest.raises(ValueError, match="must include a string `type` field"):
        Manifest.model_validate({"environment": {"value": {"TOKEN": {"type": 1}}}})

    with pytest.raises(TypeError, match="environment value must be a str, EnvValue, or EnvEntry"):
        Manifest.model_validate({"environment": {"value": {"TOKEN": 1}}})

    with pytest.raises(TypeError, match="env value must be an EnvValue or mapping"):
        Manifest.model_validate({"environment": {"value": {"TOKEN": {"value": 1}}}})

    with pytest.raises(ValidationError, match="Environment mapping must be a mapping"):
        Manifest.model_validate({"environment": {"value": None}})


@pytest.mark.asyncio
async def test_untagged_env_value_subclasses_are_usable_but_not_deserializable(
    restore_env_value_registry: None,
) -> None:
    class _UntaggedEnvValue(EnvValue):
        async def resolve(self) -> str:
            return "untagged"

    class _UntaggedChildEnvValue(_UntaggedEnvValue):
        note: str = "child"

    registered = EnvValue._subclass_registry.values()
    assert _UntaggedEnvValue not in registered
    assert _UntaggedChildEnvValue not in registered

    manifest = Manifest(
        environment=Environment(
            value={"TOKEN": _UntaggedEnvValue(), "CHILD": _UntaggedChildEnvValue()}
        )
    )

    assert await manifest.environment.resolve() == {"TOKEN": "untagged", "CHILD": "untagged"}

    with pytest.raises(ValueError, match="empty `type` field"):
        Manifest.model_validate(manifest.model_dump(mode="json"))


def test_subclassing_a_registered_env_value_without_a_new_type_raises(
    restore_env_value_registry: None,
) -> None:
    with pytest.raises(TypeError, match="already registered by _ReferenceEnvValue"):

        class _ReferenceEnvValueWithVersion(_ReferenceEnvValue):
            version: str = "v1"


def test_env_value_subclasses_cannot_declare_type_as_required_or_computed(
    restore_env_value_registry: None,
) -> None:
    with pytest.raises(TypeError, match="must define a non-empty string default for `type`"):

        class _NoDefaultEnvValue(EnvValue):
            type: Literal["test-no-default"]

            async def resolve(self) -> str:
                return "no-default"

    with pytest.raises(TypeError, match="must define a non-empty string default for `type`"):

        class _FactoryDefaultEnvValue(EnvValue):
            type: str = Field(default_factory=lambda: "test-factory-default")

            async def resolve(self) -> str:
                return "factory-default"


def test_duplicate_env_value_type_registration_raises(
    restore_env_value_registry: None,
) -> None:
    class _DuplicateEnvValueA(EnvValue):
        type: Literal["test-duplicate"] = "test-duplicate"

        async def resolve(self) -> str:
            return "a"

    with pytest.raises(TypeError, match="already registered"):

        class _DuplicateEnvValueB(EnvValue):
            type: Literal["test-duplicate"] = "test-duplicate"

            async def resolve(self) -> str:
                return "b"


def test_re_registering_the_same_env_value_class_is_allowed(
    restore_env_value_registry: None,
) -> None:
    def define() -> type[EnvValue]:
        class _ReimportedEnvValue(EnvValue):
            type: Literal["test-reimport"] = "test-reimport"

            async def resolve(self) -> str:
                return "reimported"

        return _ReimportedEnvValue

    first = define()
    second = define()

    assert first is not second
    assert EnvValue._subclass_registry["test-reimport"] is second


def test_env_value_parse_restores_subclass_of_abstract_intermediate_base(
    restore_env_value_registry: None,
) -> None:
    class _AbstractLookupEnvValue(EnvValue, abc.ABC):
        @abc.abstractmethod
        async def lookup(self) -> str: ...

        async def resolve(self) -> str:
            return await self.lookup()

    class _ConcreteLookupEnvValue(_AbstractLookupEnvValue):
        type: Literal["test-concrete"] = "test-concrete"

        async def lookup(self) -> str:
            return "looked-up"

    assert EnvValue.parse({"type": "test-concrete"}) == _ConcreteLookupEnvValue()
