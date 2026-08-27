"""Tests for JSON round-trip safety of SandboxSessionState.

Verifies that SandboxSessionState can survive serialization to JSON and
deserialization back without losing subclass identity, subclass-specific
fields, or the ``type`` discriminator under ``exclude_unset``.
"""

from __future__ import annotations

import io
import json
import uuid
from pathlib import Path
from typing import ClassVar, Literal, cast

import pytest
from pydantic import ConfigDict, ValidationError, field_serializer, field_validator

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.manifest import EnvEntry, Environment, EnvValue, StrEnvValue
from agents.sandbox.session import (
    BaseSandboxClient,
    Dependencies,
    SandboxSession,
    SandboxSessionState,
)
from agents.sandbox.snapshot import LocalSnapshot, NoopSnapshot, SnapshotBase

# ---------------------------------------------------------------------------
# Test-only stubs
# ---------------------------------------------------------------------------


class _StubSessionState(SandboxSessionState):
    __test__ = False
    type: Literal["stub-roundtrip"] = "stub-roundtrip"
    custom_field: str


class _PlainTypeSessionState(SandboxSessionState):
    __test__ = False
    type: str = "plain-type"


class _EmptyDefaultSessionState(SandboxSessionState):
    __test__ = False
    type: Literal[""] = ""


class _SimpleSessionState(SandboxSessionState):
    __test__ = False
    type: Literal["simple-roundtrip"] = "simple-roundtrip"


class _SecretReferenceEnvValue(EnvValue):
    __test__ = False
    type: Literal["test.session-secret-reference"] = "test.session-secret-reference"
    key: str

    async def resolve(self) -> str:
        return f"resolved-secret-for-{self.key}"


class _RoundTripClient(BaseSandboxClient[None]):
    backend_id = "roundtrip"
    supports_default_options = True

    def __init__(self) -> None:
        self.resume_state: SandboxSessionState | None = None

    async def create(
        self,
        *,
        snapshot: object | None = None,
        manifest: Manifest | None = None,
        options: None = None,
    ) -> SandboxSession:
        _ = (snapshot, manifest, options)
        raise AssertionError("create() is not used by round-trip tests")

    async def delete(self, session: SandboxSession) -> SandboxSession:
        raise AssertionError("delete() is not used by round-trip tests")

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        state.assert_path_grants_rebound()
        self.resume_state = state
        return cast(SandboxSession, object())

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return self._deserialize_session_state_payload(payload, _SimpleSessionState)


class _NonCopyable:
    def __deepcopy__(self, memo: dict[int, object]) -> object:
        _ = memo
        raise RuntimeError("not copyable")


class _SerializableNonCopyableSnapshot(SnapshotBase):
    __test__ = False
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    type: Literal["serializable-noncopyable-roundtrip"] = "serializable-noncopyable-roundtrip"
    token: _NonCopyable

    @field_serializer("token")
    def _serialize_token(self, value: _NonCopyable) -> str:
        _ = value
        return "token"

    @field_validator("token", mode="before")
    @classmethod
    def _parse_token(cls, value: object) -> object:
        return _NonCopyable() if value == "token" else value

    async def persist(
        self,
        data: io.IOBase,
        *,
        dependencies: Dependencies | None = None,
    ) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        raise FileNotFoundError(Path("<serializable-noncopyable>"))

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_state() -> _StubSessionState:
    return _StubSessionState(
        session_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        snapshot=LocalSnapshot(id="snap-1", base_path=Path("/tmp/snapshots")),
        manifest=Manifest(),
        custom_field="my-value",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSandboxSessionStateRoundTrip:
    def test_parse_reconstructs_subclass_from_json(self) -> None:
        """SandboxSessionState.parse() must reconstruct the correct subclass from a dict."""
        original = _make_session_state()
        payload = json.loads(original.model_dump_json())

        reconstructed = SandboxSessionState.parse(payload)

        assert type(reconstructed) is _StubSessionState
        assert reconstructed.custom_field == "my-value"

    def test_model_validate_json_loses_subclass(self) -> None:
        """Pydantic's model_validate_json against the base class loses subclass identity.

        This documents the limitation that parse() exists to solve.
        """
        original = _make_session_state()
        json_str = original.model_dump_json()

        base_instance = SandboxSessionState.model_validate_json(json_str)

        assert type(base_instance) is SandboxSessionState
        assert not hasattr(base_instance, "custom_field")

    def test_type_survives_exclude_unset(self) -> None:
        """The ``type`` discriminator must survive model_dump(exclude_unset=True).

        Since ``type`` is set via a class-level default it is not in
        model_fields_set.  Without the model_serializer, exclude_unset=True
        drops it, making SandboxSessionState.parse() fail.
        """
        state = _make_session_state()
        dumped = state.model_dump(exclude_unset=True)

        assert "type" in dumped
        assert dumped["type"] == "stub-roundtrip"

    @pytest.mark.asyncio
    async def test_parse_restores_manifest_env_value_subclasses(self) -> None:
        original = _StubSessionState(
            session_id=uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc"),
            snapshot=LocalSnapshot(id="snap-1", base_path=Path("/tmp/snapshots")),
            manifest=Manifest(
                environment=Environment(
                    value={
                        "DIRECT": _SecretReferenceEnvValue(key="direct"),
                        "ENTRY": EnvEntry(value=_SecretReferenceEnvValue(key="entry")),
                    }
                )
            ),
            custom_field="my-value",
        )

        payload = original.model_dump(mode="json")
        serialized = json.dumps(payload)

        assert "resolved-secret" not in serialized

        restored = SandboxSessionState.parse(payload)
        restored_environment = restored.manifest.environment.value

        assert type(restored_environment["DIRECT"]) is _SecretReferenceEnvValue
        restored_entry = restored_environment["ENTRY"]
        assert isinstance(restored_entry, EnvEntry)
        assert type(restored_entry.value) is _SecretReferenceEnvValue
        assert await restored.manifest.environment.resolve() == {
            "DIRECT": "resolved-secret-for-direct",
            "ENTRY": "resolved-secret-for-entry",
        }

    def test_parse_reads_legacy_discriminator_free_str_env_values(self) -> None:
        payload = _make_session_state().model_dump(mode="json")
        payload["manifest"]["environment"] = {
            "value": {
                "DIRECT": {"value": "direct-value"},
                "ENTRY": {
                    "description": "typed entry",
                    "ephemeral": True,
                    "value": {"value": "entry-value"},
                },
            }
        }

        restored = SandboxSessionState.parse(payload)

        assert restored.manifest.environment.value == {
            "DIRECT": StrEnvValue(value="direct-value"),
            "ENTRY": EnvEntry(
                description="typed entry",
                ephemeral=True,
                value=StrEnvValue(value="entry-value"),
            ),
        }

    def test_model_dump_preserves_snapshot_subclass_fields(self) -> None:
        """model_dump() must preserve snapshot subclass fields (e.g. LocalSnapshot.base_path).

        Without SerializeAsAny, Pydantic serializes using the declared field
        type (SnapshotBase), silently dropping subclass-specific fields.
        """
        state = _make_session_state()
        dumped = state.model_dump()

        assert "base_path" in dumped["snapshot"]

    def test_parse_returns_subclass_instances_as_is(self) -> None:
        state = _make_session_state()

        assert SandboxSessionState.parse(state) is state

    def test_parse_upgrades_base_instance_through_registry(self) -> None:
        state = _SimpleSessionState(
            session_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
            snapshot=LocalSnapshot(id="snap-1", base_path=Path("/tmp/snapshots")),
            manifest=Manifest(),
        )
        base_instance = SandboxSessionState.model_validate(state.model_dump())

        reconstructed = SandboxSessionState.parse(base_instance)

        assert type(reconstructed) is _SimpleSessionState
        assert reconstructed.session_id == uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

    @pytest.mark.parametrize(
        ("payload", "error_type", "message"),
        [
            ({}, ValueError, "sandbox session state payload is invalid"),
            ({"type": "missing"}, ValueError, "sandbox session state payload is invalid"),
            ("not-a-state", TypeError, "session state payload must be"),
        ],
    )
    def test_parse_rejects_invalid_payloads(
        self,
        payload: object,
        error_type: type[Exception],
        message: str,
    ) -> None:
        with pytest.raises(error_type, match=message):
            SandboxSessionState.parse(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "session-state-parse-secret"},
            {
                "type": "simple-roundtrip",
                "snapshot": {"type": "noop", "id": "snapshot"},
                "manifest": {
                    "entries": {
                        "data": {
                            "type": "unknown",
                            "token": "session-state-parse-secret",
                        }
                    }
                },
            },
        ],
    )
    def test_parse_redacts_malformed_payload_errors(self, payload: dict[str, object]) -> None:
        sentinel = "session-state-parse-secret"

        with pytest.raises(ValueError, match="sandbox session state payload is invalid") as exc:
            SandboxSessionState.parse(payload)

        assert sentinel not in str(exc.value)
        traceback = exc.value.__traceback__
        while traceback is not None:
            frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
            if "/src/agents/" in frame_path:
                assert sentinel not in repr(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next

    @pytest.mark.parametrize("as_json", [False, True])
    def test_direct_model_validation_redacts_malformed_mount_authority(
        self,
        as_json: bool,
    ) -> None:
        sentinel = "direct-model-validation-secret"
        payload: dict[str, object] = {
            "type": "simple-roundtrip",
            "session_id": [],
            "snapshot": {"type": "noop", "id": "snapshot"},
            "manifest": {
                "entries": {
                    "data": {
                        "type": "s3_mount",
                        "bucket": "bucket",
                        "secret_access_key": {"secret": sentinel},
                        "mount_strategy": {"type": "docker_volume", "driver": "rclone"},
                    }
                }
            },
        }
        model_input: object = json.dumps(payload) if as_json else payload

        with pytest.raises(ValidationError) as exc:
            if as_json:
                _SimpleSessionState.model_validate_json(cast(str, model_input))
            else:
                _SimpleSessionState.model_validate(model_input)

        assert sentinel not in str(exc.value)
        assert sentinel not in repr(exc.value)
        traceback = exc.value.__traceback__
        while traceback is not None:
            frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
            if "/src/agents/" in frame_path:
                assert sentinel not in repr(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next

    @pytest.mark.parametrize("as_json", [False, True])
    def test_direct_model_validation_redacts_non_mapping_manifest(
        self,
        as_json: bool,
    ) -> None:
        sentinel = "non-mapping-manifest-secret"
        payload: dict[str, object] = {
            "type": "simple-roundtrip",
            "snapshot": {"type": "noop", "id": "snapshot"},
            "manifest": [sentinel],
        }
        model_input: object = json.dumps(payload) if as_json else payload

        with pytest.raises(ValidationError) as exc:
            if as_json:
                _SimpleSessionState.model_validate_json(cast(str, model_input))
            else:
                _SimpleSessionState.model_validate(model_input)

        assert sentinel not in str(exc.value)
        assert sentinel not in repr(exc.value)
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None
        traceback = exc.value.__traceback__
        while traceback is not None:
            frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
            if "/src/agents/" in frame_path:
                assert sentinel not in repr(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next

    def test_model_validate_json_redacts_malformed_json(self) -> None:
        sentinel = "malformed-session-state-secret"
        malformed_json = (
            '{"type":"simple-roundtrip","manifest":{"entries":{"data":'
            f'{{"secret_access_key":"{sentinel}"}}}}'
        )

        with pytest.raises(ValueError, match="sandbox session state JSON is invalid") as exc:
            _SimpleSessionState.model_validate_json(malformed_json)

        assert sentinel not in str(exc.value)
        assert sentinel not in repr(exc.value)
        assert exc.value.__cause__ is None
        assert exc.value.__context__ is None
        traceback = exc.value.__traceback__
        while traceback is not None:
            frame_path = Path(traceback.tb_frame.f_code.co_filename).as_posix()
            if "/src/agents/" in frame_path:
                assert sentinel not in repr(traceback.tb_frame.f_locals)
            traceback = traceback.tb_next

    def test_subclass_registration_skips_non_literal_or_empty_type_defaults(self) -> None:
        assert "plain-type" not in SandboxSessionState._subclass_registry
        assert "" not in SandboxSessionState._subclass_registry

    def test_subclass_registration_skips_missing_type_field(self) -> None:
        class _NoTypeFieldSessionState(SandboxSessionState):
            type: ClassVar[str] = "no-type-field"  # type: ignore[misc]

        assert "no-type-field" not in SandboxSessionState._subclass_registry
        assert "type" not in _NoTypeFieldSessionState.model_fields

    @pytest.mark.parametrize(
        ("raw_ports", "expected"),
        [
            (None, ()),
            (8080, (8080,)),
            ([8080, 9000, 8080], (8080, 9000)),
        ],
    )
    def test_exposed_ports_are_normalized(
        self, raw_ports: object, expected: tuple[int, ...]
    ) -> None:
        state = _StubSessionState(
            snapshot=LocalSnapshot(id="snap-1", base_path=Path("/tmp/snapshots")),
            manifest=Manifest(),
            custom_field="my-value",
            exposed_ports=raw_ports,  # type: ignore[arg-type]
        )

        assert state.exposed_ports == expected

    @pytest.mark.parametrize(
        ("raw_ports", "message"),
        [
            ("8080", "exposed_ports must be an iterable"),
            ([8080, "9000"], "exposed_ports must contain integers"),
            ([0], "exposed_ports entries must be between 1 and 65535"),
            ([65536], "exposed_ports entries must be between 1 and 65535"),
        ],
    )
    def test_exposed_ports_reject_invalid_values(self, raw_ports: object, message: str) -> None:
        with pytest.raises((TypeError, ValidationError), match=message):
            _StubSessionState(
                snapshot=LocalSnapshot(id="snap-1", base_path=Path("/tmp/snapshots")),
                manifest=Manifest(),
                custom_field="my-value",
                exposed_ports=raw_ports,  # type: ignore[arg-type]
            )

    def test_client_serialization_redacts_host_paths_and_rebinds_from_trusted_manifest(
        self,
        tmp_path: Path,
    ) -> None:
        client = _RoundTripClient()
        trusted_manifest = Manifest(
            extra_path_grants=(
                SandboxPathGrant(
                    path="/mnt/shared-data",
                    host_path=str(tmp_path),
                    read_only=True,
                ),
            )
        )
        state = _SimpleSessionState(
            manifest=trusted_manifest,
            snapshot=NoopSnapshot(id="snapshot"),
        )

        payload = client.serialize_session_state(state)
        encoded = json.dumps(payload)

        assert str(tmp_path) not in encoded
        assert payload["__openai_agents_redacted_host_path_grant_paths"] == ["/mnt/shared-data"]
        manifest_payload = payload["manifest"]
        assert isinstance(manifest_payload, dict)
        assert manifest_payload["extra_path_grants"] == []
        restored = client.deserialize_session_state(payload)
        assert restored.manifest.extra_path_grants == ()
        assert restored.path_grants_require_rebind == ("/mnt/shared-data",)

        rebound = restored.rebind_persisted_path_grants(trusted_manifest)

        assert rebound.manifest.extra_path_grants == trusted_manifest.extra_path_grants
        assert rebound.path_grants_require_rebind == ()
        assert restored.manifest.extra_path_grants == ()

    @pytest.mark.asyncio
    async def test_path_only_grants_preserve_direct_client_resume_roundtrip(self) -> None:
        client = _RoundTripClient()
        manifest = Manifest(
            extra_path_grants=(
                SandboxPathGrant(path="/mnt/shared-data", read_only=True),
                SandboxPathGrant(path="/mnt/shared-data", read_only=False),
            )
        )
        state = _SimpleSessionState(
            manifest=manifest,
            snapshot=NoopSnapshot(id="snapshot"),
        )

        restored = client.deserialize_session_state(client.serialize_session_state(state))
        await client.resume(restored)

        assert restored.path_grants_require_rebind == ()
        assert client.resume_state is not None
        assert client.resume_state.manifest.extra_path_grants == manifest.extra_path_grants

    def test_client_state_roundtrip_does_not_deepcopy_extension_state(self) -> None:
        client = _RoundTripClient()
        trusted_manifest = Manifest(
            extra_path_grants=(SandboxPathGrant(path="/mnt/shared-data"),),
        )
        state = _SimpleSessionState(
            manifest=trusted_manifest,
            snapshot=_SerializableNonCopyableSnapshot(
                id="snapshot",
                token=_NonCopyable(),
            ),
        )

        payload = client.serialize_session_state(state)

        assert payload["snapshot"] == {
            "type": "serializable-noncopyable-roundtrip",
            "id": "snapshot",
            "token": "token",
        }
        restored = client.deserialize_session_state(payload)
        rebound = restored.rebind_persisted_path_grants(trusted_manifest)
        snapshot = rebound.snapshot
        assert isinstance(snapshot, _SerializableNonCopyableSnapshot)
        assert isinstance(snapshot.token, _NonCopyable)

    @pytest.mark.asyncio
    async def test_removed_redaction_marker_does_not_restore_host_backed_grant(
        self,
        tmp_path: Path,
    ) -> None:
        client = _RoundTripClient()
        state = _SimpleSessionState(
            manifest=Manifest(
                extra_path_grants=(
                    SandboxPathGrant(
                        path="/mnt/shared-data",
                        host_path=str(tmp_path),
                    ),
                )
            ),
            snapshot=NoopSnapshot(id="snapshot"),
        )
        payload = client.serialize_session_state(state)
        payload.pop("__openai_agents_redacted_host_path_grant_paths", None)

        restored = client.deserialize_session_state(payload)
        await client.resume(restored)

        assert restored.path_grants_require_rebind == ()
        assert client.resume_state is not None
        assert client.resume_state.manifest.extra_path_grants == ()

    @pytest.mark.asyncio
    async def test_deserialization_discards_unmarked_serialized_host_path(
        self,
        tmp_path: Path,
    ) -> None:
        client = _RoundTripClient()
        trusted_manifest = Manifest(
            extra_path_grants=(
                SandboxPathGrant(
                    path="/mnt/shared-data",
                    host_path=str(tmp_path),
                ),
            )
        )
        state = _SimpleSessionState(
            manifest=trusted_manifest,
            snapshot=NoopSnapshot(id="snapshot"),
        )
        payload = cast(dict[str, object], state.model_dump(mode="json"))

        restored = client.deserialize_session_state(payload)

        assert restored.manifest.extra_path_grants == ()
        assert restored.path_grants_require_rebind == ("/mnt/shared-data",)
        with pytest.raises(ValueError, match="must be rebound"):
            await client.resume(restored)

        rebound = restored.rebind_persisted_path_grants(trusted_manifest)
        await client.resume(rebound)

        assert client.resume_state is rebound
        assert rebound.manifest.extra_path_grants == trusted_manifest.extra_path_grants
