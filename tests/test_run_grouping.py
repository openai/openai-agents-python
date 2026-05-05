"""Unit tests for src/agents/run_internal/run_grouping.py pure helpers.

These cover the small, pure functions in `run_grouping` that resolve the
runner's stable grouping hierarchy used for prompt-cache keys and trace
grouping. The module had no direct test file even though it's imported
across the runner and prompt-cache code paths.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents.memory import Session
from agents.run_internal.run_grouping import (
    get_session_id_if_available,
    resolve_run_grouping,
    resolve_run_grouping_id,
)


class _FakeSession:
    """Minimal stand-in for a Session that exposes a `session_id` attribute."""

    def __init__(self, session_id: Any) -> None:
        self.session_id = session_id


class _BrokenSession:
    """Session whose `session_id` access raises (e.g. lazy-loaded backends)."""

    @property
    def session_id(self) -> str:
        raise RuntimeError("backend unavailable")


def _fake(session_id: Any) -> Session:
    return cast(Session, _FakeSession(session_id))


def _broken() -> Session:
    return cast(Session, _BrokenSession())


class TestGetSessionIdIfAvailable:
    def test_returns_none_when_session_is_none(self) -> None:
        assert get_session_id_if_available(None) is None

    def test_returns_session_id_when_present(self) -> None:
        assert get_session_id_if_available(_fake("sess-1")) == "sess-1"

    def test_strips_surrounding_whitespace(self) -> None:
        assert get_session_id_if_available(_fake("  sess-2  ")) == "sess-2"

    def test_returns_none_for_empty_string(self) -> None:
        assert get_session_id_if_available(_fake("")) is None

    def test_returns_none_for_whitespace_only(self) -> None:
        assert get_session_id_if_available(_fake("   ")) is None

    def test_returns_none_when_attribute_access_raises(self) -> None:
        # Backends that raise on attribute access must not propagate.
        assert get_session_id_if_available(_broken()) is None


class TestResolveRunGrouping:
    def test_conversation_id_takes_priority(self) -> None:
        # When all three are set, conversation wins.
        kind, value = resolve_run_grouping(
            conversation_id="conv-1",
            session=_fake("sess-1"),
            group_id="group-1",
        )
        assert (kind, value) == ("conversation", "conv-1")

    def test_conversation_id_is_stripped(self) -> None:
        kind, value = resolve_run_grouping(
            conversation_id="  conv-2  ",
            session=None,
            group_id=None,
        )
        assert (kind, value) == ("conversation", "conv-2")

    def test_empty_conversation_id_falls_through_to_session(self) -> None:
        kind, value = resolve_run_grouping(
            conversation_id="   ",
            session=_fake("sess-1"),
            group_id="group-1",
        )
        assert (kind, value) == ("session", "sess-1")

    def test_session_takes_priority_over_group_id(self) -> None:
        kind, value = resolve_run_grouping(
            conversation_id=None,
            session=_fake("sess-2"),
            group_id="group-2",
        )
        assert (kind, value) == ("session", "sess-2")

    def test_unavailable_session_falls_through_to_group_id(self) -> None:
        # An empty session_id is treated as no session.
        kind, value = resolve_run_grouping(
            conversation_id=None,
            session=_fake("   "),
            group_id="group-3",
        )
        assert (kind, value) == ("group", "group-3")

    def test_group_id_used_when_no_conversation_or_session(self) -> None:
        kind, value = resolve_run_grouping(
            conversation_id=None,
            session=None,
            group_id="group-4",
        )
        assert (kind, value) == ("group", "group-4")

    def test_group_id_is_stripped(self) -> None:
        kind, value = resolve_run_grouping(
            conversation_id=None,
            session=None,
            group_id="  group-5  ",
        )
        assert (kind, value) == ("group", "group-5")

    def test_falls_back_to_generated_run_id(self) -> None:
        kind, value = resolve_run_grouping(
            conversation_id=None,
            session=None,
            group_id=None,
        )
        assert kind == "run"
        # uuid4().hex is a 32-char lowercase hex string.
        assert len(value) == 32
        assert all(c in "0123456789abcdef" for c in value)

    def test_generated_run_ids_are_unique(self) -> None:
        _, value_1 = resolve_run_grouping(conversation_id=None, session=None, group_id=None)
        _, value_2 = resolve_run_grouping(conversation_id=None, session=None, group_id=None)
        assert value_1 != value_2

    def test_empty_strings_all_round_fall_back_to_run(self) -> None:
        kind, value = resolve_run_grouping(
            conversation_id="   ",
            session=None,
            group_id="   ",
        )
        assert kind == "run"
        assert len(value) == 32


class TestResolveRunGroupingId:
    def test_conversation_id_returned_as_is(self) -> None:
        assert (
            resolve_run_grouping_id(conversation_id="conv-1", session=None, group_id=None)
            == "conv-1"
        )

    def test_session_id_returned_as_is(self) -> None:
        assert (
            resolve_run_grouping_id(
                conversation_id=None,
                session=_fake("sess-1"),
                group_id=None,
            )
            == "sess-1"
        )

    def test_group_id_returned_as_is(self) -> None:
        assert (
            resolve_run_grouping_id(conversation_id=None, session=None, group_id="group-1")
            == "group-1"
        )

    def test_run_kind_is_prefixed(self) -> None:
        # Generated run grouping is the only kind that gets the "run-" prefix.
        run_id = resolve_run_grouping_id(conversation_id=None, session=None, group_id=None)
        assert run_id.startswith("run-")
        assert len(run_id) == len("run-") + 32

    @pytest.mark.parametrize(
        ("conversation_id", "session_id", "group_id", "expected"),
        [
            ("conv", "sess", "grp", "conv"),
            (None, "sess", "grp", "sess"),
            (None, None, "grp", "grp"),
        ],
    )
    def test_priority_order(
        self,
        conversation_id: str | None,
        session_id: str | None,
        group_id: str | None,
        expected: str,
    ) -> None:
        session = _fake(session_id) if session_id is not None else None
        assert (
            resolve_run_grouping_id(
                conversation_id=conversation_id,
                session=session,
                group_id=group_id,
            )
            == expected
        )
