from __future__ import annotations

from typing import Any, cast

from agents.memory.session import Session
from agents.run_internal.run_grouping import (
    get_session_id_if_available,
    resolve_run_grouping,
)


class _StubSession:
    session_settings = None

    def __init__(self, session_id: Any) -> None:
        self.session_id = session_id


def test_get_session_id_handles_non_string_session_id() -> None:
    """A misbehaving session whose session_id is None or non-string falls back."""
    assert get_session_id_if_available(cast(Session, _StubSession(None))) is None
    assert get_session_id_if_available(cast(Session, _StubSession(12345))) is None


def test_get_session_id_handles_blank_and_valid() -> None:
    assert get_session_id_if_available(cast(Session, _StubSession("   "))) is None
    assert get_session_id_if_available(cast(Session, _StubSession("  sess-1  "))) == "sess-1"
    assert get_session_id_if_available(None) is None


def test_resolve_run_grouping_falls_back_when_session_id_invalid() -> None:
    kind, value = resolve_run_grouping(
        conversation_id=None,
        session=cast(Session, _StubSession(None)),
        group_id="grp-1",
    )
    assert (kind, value) == ("group", "grp-1")
