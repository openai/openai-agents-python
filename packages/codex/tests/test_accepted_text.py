"""Accepted text readers have been publishing only approved independent prefixes."""

import asyncio
from types import SimpleNamespace

import pytest

from agents import AcceptedTextChunk, Turn
from agents.observation import EventGap, OutputTextDelta
from agents.turn import _root_turn


def delta(text, *, item="item", submission="submission", parent=None):
    return OutputTextDelta(
        session_id="session",
        submission_id=submission,
        turn_id="native-turn",
        item_id=item,
        delta=text,
        parent_submission_id=parent,
    )


async def reader(monkeypatch, events, accept):
    completion = asyncio.Future()
    session = SimpleNamespace(id="session", _owner=object())
    turn = _root_turn(session, "native-turn", "submission", completion)
    session._active = SimpleNamespace(
        handle=turn, stopping=False, terminal=asyncio.Future(), failure=asyncio.Future()
    )

    class Stream:
        async def __aiter__(self):
            for event in events:
                yield event

    monkeypatch.setattr(Turn, "events", lambda _: Stream())
    return [chunk async for chunk in turn.accepted_text(accept)]


async def test_contiguous_prefixes_discard_unapproved_tail_on_item_change(monkeypatch):
    calls = []

    def accept(text):
        calls.append(text)
        return text[: text.index(".") + 1] if "." in text else None

    chunks = await reader(
        monkeypatch, [delta("Ready"), delta(". private"), delta("Next.", item="next")], accept
    )
    assert [chunk.text for chunk in chunks] == ["Ready.", "Next."]
    assert calls == ["Ready", "Ready. private", "Next."]
    assert chunks[0] == AcceptedTextChunk("session", "submission", "native-turn", "item", "Ready.")


async def test_private_repairs_and_other_roots_stay_outside_acceptance(monkeypatch):
    calls = []
    chunks = await reader(
        monkeypatch,
        [
            delta("repair", submission="repair", parent="submission"),
            delta("other", submission="other"),
            delta("own"),
        ],
        lambda text: calls.append(text) or text,
    )
    assert calls == ["own"]
    assert [chunk.text for chunk in chunks] == ["own"]


@pytest.mark.parametrize("approved", [False, 1, "unrelated"])
async def test_invalid_approval_fails_closed(monkeypatch, approved):
    with pytest.raises(ValueError, match="prefix"):
        await reader(monkeypatch, [delta("private")], lambda _: approved)


async def test_gap_fails_without_exposing_later_text(monkeypatch):
    calls = []
    with pytest.raises(RuntimeError, match="gap"):
        await reader(
            monkeypatch,
            [EventGap(session_id="session", first_sequence=1), delta("private")],
            lambda text: calls.append(text) or text,
        )
    assert not calls


async def test_utf8_pending_limit_is_bounded(monkeypatch):
    with pytest.raises(RuntimeError, match="limit"):
        await reader(monkeypatch, [delta("é" * 32769)], lambda _: None)


async def test_async_acceptance_rechecks_exact_native_owner(monkeypatch):
    async def accept(text):
        await asyncio.sleep(0)
        return text

    chunks = await reader(monkeypatch, [delta("ready")], accept)
    assert [chunk.text for chunk in chunks] == ["ready"]
