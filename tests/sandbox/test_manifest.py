import asyncio
import contextlib
import json
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from pydantic import model_serializer
from pydantic_core import PydanticSerializationError

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


class _SecretReferenceEnvValue(EnvValue):
    type: Literal["test.secret_reference"] = "test.secret_reference"
    key: str

    async def resolve(self) -> str:
        return f"resolved-secret-for-{self.key}"


class _CustomSerializedEnvValue(EnvValue):
    type: Literal["test.custom_serializer"] = "test.custom_serializer"
    key: str
    internal_value: str = ""

    async def resolve(self) -> str:
        return self.internal_value

    @model_serializer
    def _serialize_reference(self) -> dict[str, str]:
        return {"key": self.key}


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


@pytest.mark.asyncio
async def test_manifest_round_trips_tagged_env_values_without_resolved_secrets() -> None:
    manifest = Manifest(
        environment=Environment(
            value={
                "DIRECT": _SecretReferenceEnvValue(key="direct"),
                "ENTRY": EnvEntry(
                    description="secret reference",
                    ephemeral=True,
                    value=_SecretReferenceEnvValue(key="entry"),
                ),
            }
        )
    )

    payload_json = manifest.model_dump_json()
    payload = json.loads(payload_json)

    assert payload["environment"] == {
        "value": {
            "DIRECT": {"type": "test.secret_reference", "key": "direct"},
            "ENTRY": {
                "description": "secret reference",
                "ephemeral": True,
                "value": {"type": "test.secret_reference", "key": "entry"},
            },
        }
    }
    assert "resolved-secret" not in payload_json

    restored = Manifest.model_validate_json(payload_json)

    assert type(restored.environment.value["DIRECT"]) is _SecretReferenceEnvValue
    restored_entry = restored.environment.value["ENTRY"]
    assert isinstance(restored_entry, EnvEntry)
    assert type(restored_entry.value) is _SecretReferenceEnvValue
    assert await restored.environment.resolve() == {
        "DIRECT": "resolved-secret-for-direct",
        "ENTRY": "resolved-secret-for-entry",
    }


def test_manifest_preserves_type_from_env_value_custom_serializer() -> None:
    manifest = Manifest(
        environment=Environment(
            value={
                "DIRECT": _CustomSerializedEnvValue(
                    key="direct",
                    internal_value="direct-secret",
                ),
                "ENTRY": EnvEntry(
                    value=_CustomSerializedEnvValue(
                        key="entry",
                        internal_value="entry-secret",
                    )
                ),
            }
        )
    )

    payload = manifest.model_dump(mode="json")
    serialized = json.dumps(payload)

    assert payload["environment"]["value"] == {
        "DIRECT": {"type": "test.custom_serializer", "key": "direct"},
        "ENTRY": {
            "description": None,
            "ephemeral": False,
            "value": {"type": "test.custom_serializer", "key": "entry"},
        },
    }
    assert "direct-secret" not in serialized
    assert "entry-secret" not in serialized

    restored = Manifest.model_validate(payload)

    assert type(restored.environment.value["DIRECT"]) is _CustomSerializedEnvValue
    restored_entry = restored.environment.value["ENTRY"]
    assert isinstance(restored_entry, EnvEntry)
    assert type(restored_entry.value) is _CustomSerializedEnvValue


def test_manifest_round_trips_str_env_value() -> None:
    manifest = Manifest(
        environment=Environment(value={"PLAIN": "plain", "TYPED": StrEnvValue(value="typed")})
    )

    payload = manifest.model_dump(mode="json")
    restored = Manifest.model_validate(payload)

    assert payload["environment"] == {
        "value": {"PLAIN": "plain", "TYPED": {"type": "str", "value": "typed"}}
    }
    assert restored.environment.value == {
        "PLAIN": "plain",
        "TYPED": StrEnvValue(value="typed"),
    }


def test_manifest_reads_legacy_discriminator_free_str_env_values() -> None:
    payload = {
        "environment": {
            "value": {
                "DIRECT": {"value": "direct-value"},
                "ENTRY": {
                    "description": "typed entry",
                    "ephemeral": True,
                    "value": {"value": "entry-value"},
                },
            }
        }
    }

    restored = Manifest.model_validate(payload)

    assert restored.environment.value == {
        "DIRECT": StrEnvValue(value="direct-value"),
        "ENTRY": EnvEntry(
            description="typed entry",
            ephemeral=True,
            value=StrEnvValue(value="entry-value"),
        ),
    }


def test_manifest_rejects_ambiguous_discriminator_free_env_values() -> None:
    payload = {
        "environment": {
            "value": {
                "AMBIGUOUS": {"value": "plain", "description": "not a legacy StrEnvValue"},
            }
        }
    }

    with pytest.raises(ValueError, match="must include a string `type` field"):
        Manifest.model_validate(payload)


@pytest.mark.parametrize(("exclude_unset", "exclude_defaults"), [(True, False), (False, True)])
def test_manifest_env_value_type_survives_narrowed_dumps(
    exclude_unset: bool,
    exclude_defaults: bool,
) -> None:
    manifest = Manifest(
        environment=Environment(value={"TOKEN": _SecretReferenceEnvValue(key="token")})
    )

    payload = manifest.model_dump(
        mode="json",
        exclude_unset=exclude_unset,
        exclude_defaults=exclude_defaults,
    )

    assert payload["environment"]["value"]["TOKEN"]["type"] == "test.secret_reference"
    assert Manifest.model_validate(payload).environment == manifest.environment


def test_manifest_rejects_unknown_env_value_type() -> None:
    payload = {"environment": {"value": {"TOKEN": {"type": "unknown.env.value"}}}}

    with pytest.raises(ValueError, match="Unknown env value type `unknown.env.value`"):
        Manifest.model_validate(payload)


@pytest.mark.asyncio
async def test_untagged_env_value_imports_and_resolves_but_does_not_serialize() -> None:
    class _UntaggedEnvValue(EnvValue):
        key: str

        async def resolve(self) -> str:
            return f"resolved-secret-for-{self.key}"

    value = _UntaggedEnvValue(key="token")

    assert await value.resolve() == "resolved-secret-for-token"
    with pytest.raises(
        PydanticSerializationError,
        match="_UntaggedEnvValue must explicitly declare its own non-empty `type`",
    ):
        Manifest(environment=Environment(value={"TOKEN": value})).model_dump_json()


@pytest.mark.asyncio
async def test_inherited_env_value_tag_imports_and_resolves_but_does_not_serialize() -> None:
    class _LabeledStrEnvValue(StrEnvValue):
        label: str

    value = _LabeledStrEnvValue(value="plain", label="example")

    assert await value.resolve() == "plain"
    with pytest.raises(
        PydanticSerializationError,
        match="_LabeledStrEnvValue must explicitly declare its own non-empty `type`",
    ):
        Manifest(environment=Environment(value={"VALUE": value})).model_dump_json()


def test_duplicate_env_value_type_registration_raises() -> None:
    with pytest.raises(
        TypeError,
        match="already registered by _SecretReferenceEnvValue",
    ):

        class _DuplicateSecretReferenceEnvValue(EnvValue):
            type: Literal["test.secret_reference"] = "test.secret_reference"

            async def resolve(self) -> str:
                return "unused"


class _BlockingEnvValue(EnvValue):
    """Stands in for a user resolver that reaches a secret store or the network.

    Blocks on a test-owned release signal rather than forever, so a failed
    assertion (or a future regression) cannot leave this task pending for the rest
    of the session.
    """

    type: Literal["test.blocking"] = "test.blocking"

    _started: ClassVar[asyncio.Event]
    _release: ClassVar[asyncio.Event]
    _finished: ClassVar[asyncio.Event]
    _cancelled: ClassVar[bool]

    async def resolve(self) -> str:
        cls = type(self)
        cls._started.set()
        try:
            await cls._release.wait()
        except asyncio.CancelledError:
            cls._cancelled = True
            raise
        finally:
            cls._finished.set()
        return "unreachable"


class _FailingEnvValue(EnvValue):
    type: Literal["test.failing"] = "test.failing"

    async def resolve(self) -> str:
        # Fail only once the sibling is genuinely in flight, so the test pins the
        # interleaving instead of racing the two resolvers.
        await _BlockingEnvValue._started.wait()
        raise RuntimeError("secret backend rejected the request")


@pytest.mark.asyncio
async def test_environment_resolve_cancels_siblings_when_one_resolver_fails() -> None:
    """A failed env lookup must not leave the other resolvers running.

    `EnvValue` is an extension point, so `Environment.resolve()` fans out
    user-supplied coroutines that can reach a secret store. A bare `asyncio.gather`
    returns on the first failure and leaves the siblings pending, so a rejected
    lookup left other secret fetches in flight after the manifest had already
    failed.
    """
    _BlockingEnvValue._started = asyncio.Event()
    _BlockingEnvValue._release = asyncio.Event()
    _BlockingEnvValue._finished = asyncio.Event()
    _BlockingEnvValue._cancelled = False

    environment = Environment(
        value={"BLOCKING": _BlockingEnvValue(), "FAILING": _FailingEnvValue()}
    )

    try:
        with pytest.raises(RuntimeError, match="secret backend rejected the request"):
            await environment.resolve()

        assert _BlockingEnvValue._cancelled, "sibling resolver was not cancelled"
        await asyncio.wait_for(_BlockingEnvValue._finished.wait(), timeout=1)
    finally:
        # Release the resolver whether or not the assertions held, so running this
        # against the base revision drains its task instead of stranding it.
        _BlockingEnvValue._release.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(_BlockingEnvValue._finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_environment_resolve_still_returns_every_value() -> None:
    """The cancel path must not change the success path's mapping."""
    environment = Environment(
        value={
            "PLAIN": "literal",
            "REF": _SecretReferenceEnvValue(key="alpha"),
            "ENTRY": EnvEntry(value=_SecretReferenceEnvValue(key="beta")),
        }
    )

    resolved = await environment.resolve()

    assert resolved == {
        "PLAIN": "literal",
        "REF": "resolved-secret-for-alpha",
        "ENTRY": "resolved-secret-for-beta",
    }
