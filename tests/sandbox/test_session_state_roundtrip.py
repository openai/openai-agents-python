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
