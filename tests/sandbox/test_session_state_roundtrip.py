"""Tests for JSON round-trip safety of SandboxSessionState.

Verifies that SandboxSessionState can survive serialization to JSON and
deserialization back without losing subclass identity, subclass-specific
fields, or the ``type`` discriminator under ``exclude_unset``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from agents.sandbox import Manifest
from agents.sandbox.session import SandboxSessionState
from agents.sandbox.snapshot import LocalSnapshot

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
            ({}, ValueError, "must include a string `type`"),
            ({"type": "missing"}, ValueError, "unknown sandbox session state type `missing`"),
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

    def test_subclass_registration_skips_non_literal_or_empty_type_defaults(self) -> None:
        assert "plain-type" not in SandboxSessionState._subclass_registry
        assert "" not in SandboxSessionState._subclass_registry

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
