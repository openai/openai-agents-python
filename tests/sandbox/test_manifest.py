import asyncio
import contextlib
import json
import pickle
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
from agents.sandbox.manifest import (
    EnvEntry,
    Environment,
    EnvValue,
    Manifest,
    ProcessEnvValue,
    StrEnvValue,
    _normalize_process_environment_bindings,
)
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


class _NonCopyableClient:
    def __deepcopy__(self, _memo: object) -> "_NonCopyableClient":
        raise RuntimeError("client must not be copied")


class _ClientBackedEnvValue(EnvValue):
    type: Literal["test.client_backed"] = "test.client_backed"
    client: object

    async def resolve(self) -> str:
        return "resolved"


@pytest.mark.asyncio
async def test_manifest_pickle_revokes_process_environment_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    monkeypatch.setenv(name, "pickle-secret")
    trusted = Manifest(
        environment=Environment(value={name: ProcessEnvValue()}),
    )._with_process_environment_access(name)

    restored = pickle.loads(pickle.dumps(trusted))

    assert restored._process_environment_access == frozenset()
    with pytest.raises(ValueError, match=f"binding {name!r} -> {name!r} is not granted"):
        await restored.resolve_environment()


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


@pytest.mark.asyncio
async def test_manifest_resolves_same_name_and_renamed_process_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_TEST_SAME_NAME", "same-value")
    monkeypatch.setenv("SANDBOX_TEST_SOURCE_NAME", "renamed-value")
    manifest = Manifest(
        environment=Environment(
            value={
                "SANDBOX_TEST_SAME_NAME": ProcessEnvValue(),
                "SANDBOX_TEST_DESTINATION": ProcessEnvValue(name="SANDBOX_TEST_SOURCE_NAME"),
                "PLAIN": "literal",
            }
        )
    )._with_process_environment_access(
        "SANDBOX_TEST_SAME_NAME",
        ("SANDBOX_TEST_DESTINATION", "SANDBOX_TEST_SOURCE_NAME"),
    )

    assert await manifest.resolve_environment() == {
        "SANDBOX_TEST_SAME_NAME": "same-value",
        "SANDBOX_TEST_DESTINATION": "renamed-value",
        "PLAIN": "literal",
    }

    monkeypatch.setenv("SANDBOX_TEST_SAME_NAME", "rotated-value")
    assert (await manifest.resolve_environment())["SANDBOX_TEST_SAME_NAME"] == "rotated-value"


@pytest.mark.asyncio
async def test_process_environment_access_distinguishes_missing_and_empty_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    manifest = Manifest(
        environment=Environment(value={name: ProcessEnvValue()})
    )._with_process_environment_access(name)
    monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match=f"variable {name!r} is not set"):
        await manifest.resolve_environment()

    monkeypatch.setenv(name, "")

    assert await manifest.resolve_environment() == {name: ""}


@pytest.mark.asyncio
async def test_non_process_environment_resolution_does_not_read_process_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    monkeypatch.setenv(name, "creation-only-value")
    manifest = Manifest(
        environment=Environment(
            value={
                name: ProcessEnvValue(),
                "LITERAL": "literal-value",
            }
        )
    )._with_process_environment_access(name)
    monkeypatch.delenv(name)

    assert await manifest._resolve_environment_without_process_values() == {
        "LITERAL": "literal-value"
    }
    monkeypatch.setenv(name, "current-value")
    assert await manifest._resolve_process_environment_values() == {name: "current-value"}


@pytest.mark.asyncio
async def test_process_environment_access_is_runtime_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    secret = "must-not-be-serialized"
    monkeypatch.setenv(name, secret)
    untrusted = Manifest(environment=Environment(value={"TOKEN": ProcessEnvValue(name=name)}))
    manifest = untrusted._with_process_environment_access(("TOKEN", name))

    payload = manifest.model_dump(mode="json")
    serialized = json.dumps(payload)
    restored = Manifest.model_validate(payload)

    assert payload["environment"] == {"value": {"TOKEN": {"type": "process_env", "name": name}}}
    assert secret not in serialized
    assert "process_environment_access" not in serialized
    assert type(restored.environment.value["TOKEN"]) is ProcessEnvValue

    with pytest.raises(ValueError, match=f"binding {name!r} -> 'TOKEN' is not granted"):
        await untrusted.resolve_environment()
    with pytest.raises(ValueError, match=f"binding {name!r} -> 'TOKEN' is not granted"):
        await restored.resolve_environment()

    rebound = restored._with_process_environment_access(("TOKEN", name))
    assert await rebound.resolve_environment() == {"TOKEN": secret}


@pytest.mark.asyncio
async def test_process_environment_reference_requires_client_runtime_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    monkeypatch.setenv(name, "from-process")
    value = ProcessEnvValue(name=name)
    environment = Environment(value={"TOKEN": value})

    with pytest.raises(ValueError, match="sandbox client with trusted"):
        await value.resolve()
    with pytest.raises(ValueError, match=f"binding {name!r} -> 'TOKEN' is not granted"):
        await environment.resolve()


@pytest.mark.asyncio
async def test_process_environment_access_is_bound_to_the_sandbox_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    monkeypatch.setenv(name, "secret")
    trusted = Manifest(
        environment=Environment(value={"TOKEN": ProcessEnvValue(name=name)})
    )._with_process_environment_access(("TOKEN", name))
    tampered = trusted.model_copy(
        update={
            "environment": Environment(value={"EXFIL": ProcessEnvValue(name=name)}),
        },
        deep=True,
    )

    with pytest.raises(ValueError, match=f"binding {name!r} -> 'EXFIL' is not granted"):
        await tampered.resolve_environment()


def test_process_environment_client_config_rejects_conflicting_destinations() -> None:
    with pytest.raises(ValueError, match="conflicting bindings"):
        _normalize_process_environment_bindings(
            allowed_process_environment_keys={"TOKEN"},
            process_environment_bindings={"TOKEN": "PROD_TOKEN"},
        )


def test_process_environment_client_config_rejects_bare_string_allowlist() -> None:
    with pytest.raises(TypeError, match="iterable of names, not a string"):
        _normalize_process_environment_bindings(allowed_process_environment_keys="TOKEN")


def test_process_environment_access_does_not_copy_unrelated_resolvers() -> None:
    resolver = _ClientBackedEnvValue(client=_NonCopyableClient())
    manifest = Manifest(
        environment=Environment(
            value={
                "TOKEN": ProcessEnvValue(),
                "CUSTOM": resolver,
            }
        )
    )

    trusted = manifest._with_process_environment_access("TOKEN")

    assert trusted is not manifest
    assert trusted.environment.value["CUSTOM"] is resolver


def test_process_environment_access_snapshots_process_declarations() -> None:
    source = ProcessEnvValue(name="PROD_TOKEN")
    entry = EnvEntry(value=ProcessEnvValue(name="ENTRY_TOKEN"))
    manifest = Manifest(
        environment=Environment(
            value={
                "TOKEN": source,
                "ENTRY": entry,
            }
        )
    )

    trusted = manifest._with_process_environment_access(
        ("TOKEN", "PROD_TOKEN"),
        ("ENTRY", "ENTRY_TOKEN"),
    )
    manifest.environment.value.clear()
    source.name = "MUTATED_TOKEN"
    entry.value.name = "MUTATED_ENTRY_TOKEN"

    assert trusted._declared_process_environment_bindings() == frozenset(
        {("TOKEN", "PROD_TOKEN"), ("ENTRY", "ENTRY_TOKEN")}
    )
    assert trusted._has_process_environment_access() is True


def test_process_environment_client_rebind_replaces_prior_runtime_authority() -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    privileged = Manifest(
        environment=Environment(value={name: ProcessEnvValue()})
    )._with_process_environment_access(name)

    rebound = privileged._with_process_environment_access(frozenset())

    assert rebound._process_environment_access == frozenset()


@pytest.mark.asyncio
async def test_process_environment_access_grants_only_requested_destination_for_shared_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_name = "SANDBOX_TEST_SHARED_PROCESS_ENV_VALUE"
    monkeypatch.setenv(source_name, "secret")
    manifest = Manifest(
        environment=Environment(
            value={
                "TOKEN": ProcessEnvValue(name=source_name),
                "EXFIL": ProcessEnvValue(name=source_name),
            }
        )
    )

    trusted = manifest._with_process_environment_access(("TOKEN", source_name))

    assert trusted._process_environment_access == frozenset({("TOKEN", source_name)})
    with pytest.raises(ValueError, match=f"binding {source_name!r} -> 'EXFIL' is not granted"):
        await trusted.resolve_environment()


@pytest.mark.asyncio
async def test_process_environment_bindings_are_validated_before_custom_resolvers_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    monkeypatch.setenv(name, "secret")
    resolver_started = False

    class RecordingEnvValue(EnvValue):
        type: Literal["test.recording_process_env_sibling"] = "test.recording_process_env_sibling"

        async def resolve(self) -> str:
            nonlocal resolver_started
            resolver_started = True
            return "resolved"

    manifest = Manifest(
        environment=Environment(
            value={
                "CUSTOM": RecordingEnvValue(),
                "TOKEN": ProcessEnvValue(name=name),
            }
        )
    )

    with pytest.raises(ValueError, match=f"binding {name!r} -> 'TOKEN' is not granted"):
        await manifest.resolve_environment()

    assert resolver_started is False


@pytest.mark.asyncio
async def test_process_environment_destination_names_are_validated_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    monkeypatch.setenv(name, "secret")
    resolver_started = False

    class RecordingEnvValue(EnvValue):
        type: Literal["test.recording_invalid_destination_sibling"] = (
            "test.recording_invalid_destination_sibling"
        )

        async def resolve(self) -> str:
            nonlocal resolver_started
            resolver_started = True
            return "resolved"

    for destination in ("INVALID=DEST", "INVALID\x00DEST"):
        with pytest.raises(ValueError, match="must not contain '=' or NUL"):
            Manifest(
                environment=Environment(
                    value={
                        destination: ProcessEnvValue(name=name),
                        "CUSTOM": RecordingEnvValue(),
                    }
                )
            )._with_process_environment_access((destination, name))

    assert resolver_started is False


def _manifest_traceback_locals(error: BaseException) -> str:
    frames: list[dict[str, object]] = []
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_filename.endswith("/agents/sandbox/manifest.py"):
            frames.append(dict(frame.f_locals))
        traceback = traceback.tb_next
    return repr(frames)


@pytest.mark.asyncio
async def test_custom_resolver_failure_does_not_retain_process_values_in_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    secret = "process-secret-must-not-reach-traceback"
    monkeypatch.setenv(name, secret)

    class FailingEnvValue(EnvValue):
        type: Literal["test.failing_process_env_sibling"] = "test.failing_process_env_sibling"

        async def resolve(self) -> str:
            raise RuntimeError("custom resolver failed")

    manifest = Manifest(
        environment=Environment(
            value={
                "TOKEN": ProcessEnvValue(name=name),
                "CUSTOM": FailingEnvValue(),
            }
        )
    )._with_process_environment_access(("TOKEN", name))

    with pytest.raises(RuntimeError, match="custom resolver failed") as exc_info:
        await manifest.resolve_environment()

    assert secret not in _manifest_traceback_locals(exc_info.value)


@pytest.mark.asyncio
async def test_process_values_are_snapshotted_before_custom_resolvers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_name = "SANDBOX_TEST_FIRST_PROCESS_ENV_VALUE"
    second_name = "SANDBOX_TEST_SECOND_PROCESS_ENV_VALUE"
    secret = "partial-process-secret-must-not-reach-traceback"
    monkeypatch.setenv(first_name, secret)
    monkeypatch.setenv(second_name, "removed-before-materialization")

    class RemovingEnvValue(EnvValue):
        type: Literal["test.removing_process_env_sibling"] = "test.removing_process_env_sibling"

        async def resolve(self) -> str:
            monkeypatch.delenv(second_name)
            return "custom"

    manifest = Manifest(
        environment=Environment(
            value={
                "FIRST": ProcessEnvValue(name=first_name),
                "SECOND": ProcessEnvValue(name=second_name),
                "CUSTOM": RemovingEnvValue(),
            }
        )
    )._with_process_environment_access(("FIRST", first_name), ("SECOND", second_name))

    assert await manifest.resolve_environment() == {
        "FIRST": secret,
        "SECOND": "removed-before-materialization",
        "CUSTOM": "custom",
    }


@pytest.mark.asyncio
async def test_process_snapshot_survives_custom_resolver_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "SANDBOX_TEST_PROCESS_ENV_VALUE"
    custom_secret = "custom-secret-must-not-reach-traceback"
    monkeypatch.setenv(name, "removed-before-materialization")

    class SecretRemovingEnvValue(EnvValue):
        type: Literal["test.secret_removing_process_env_sibling"] = (
            "test.secret_removing_process_env_sibling"
        )

        async def resolve(self) -> str:
            monkeypatch.delenv(name)
            return custom_secret

    manifest = Manifest(
        environment=Environment(
            value={
                "TOKEN": ProcessEnvValue(name=name),
                "CUSTOM": SecretRemovingEnvValue(),
            }
        )
    )._with_process_environment_access(("TOKEN", name))

    assert await manifest.resolve_environment() == {
        "TOKEN": "removed-before-materialization",
        "CUSTOM": custom_secret,
    }


@pytest.mark.parametrize(
    "payload_key",
    [
        "process_environment_access",
        "_process_environment_access",
        "processEnvironmentAllowedNames",
    ],
)
def test_manifest_rejects_serialized_process_environment_authority(payload_key: str) -> None:
    with pytest.raises(TypeError, match="trusted sandbox client runtime configuration"):
        Manifest.model_validate({payload_key: ["OPENAI_API_KEY"]})


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
