"""An exact child turn distinguishes supported blank text from missing content."""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any

import pytest

from agents import NativeError
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_response_integrity import assert_phased_answer_in_history, phased_answer


@pytest.mark.parametrize(
    "kind",
    ["blank", "whitespace", "input_text", "empty_content", "blank_then_empty_content"],
)
async def test_child_text_presence_belongs_to_the_exact_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    text = " \n\t " if kind == "whitespace" else ""
    first = assistant_output(text)
    first["item"]["status"] = "completed"
    if kind == "input_text":
        first["item"]["content"] = [{"type": "input_text", "text": text}]
    if kind == "empty_content":
        first["item"]["content"] = []
    first_reply = [first]
    if kind == "blank_then_empty_content":
        missing = assistant_output("")
        missing["item"]["status"] = "completed"
        missing["item"]["content"] = []
        first_reply.append(missing)
    replies = deque([first_reply, [assistant_output("later child answer")]])
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        # Keep the real native spawn, while the local provider supplies text
        # directly to the child. This test does not need to run a shell tool.
        ordinary_reply = peer.response

        def scripted_reply() -> list[dict[str, Any]]:
            result = ordinary_reply()
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] != "/root":
                return replies.popleft()
            return result

        monkeypatch.setattr(peer, "response", scripted_reply)
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            root = await session.submit("Start one child")
            assert (await root.get_result()).final_output == "root complete"
            await handles(session, 1)
            [child] = [entry async for entry in session.subagents()]
            first_turn = child.turn
            assert first_turn is not None
            unsupported = kind in {"empty_content", "blank_then_empty_content"}
            failure_message = None
            if unsupported:
                with pytest.raises(NativeError) as failed:
                    await first_turn.get_result()
                failure_message = str(failed.value)
                assert failure_message in {
                    "Native child turn failed",
                    "Native model response ended without complete supported output",
                }
            else:
                result = await first_turn.get_result()
                assert result.turn is first_turn
                assert result.final_output == ""
            events = [event async for event in first_turn.events()]
            assert isinstance(events[-1], TurnFinished)
            assert events[-1].status == ("failed" if unsupported else "completed")

            later = await child.start("Respond again")
            assert later.turn_id != first_turn.turn_id
            assert (await later.get_result()).final_output == "later child answer"
            if unsupported:
                with pytest.raises(NativeError) as repeated:
                    await first_turn.get_result()
                assert str(repeated.value) == failure_message
            else:
                captured = await first_turn.get_result()
                assert captured.turn is first_turn
                assert captured.final_output == result.final_output == ""
                assert captured.thread_usage == result.thread_usage
            assert not replies


@pytest.mark.parametrize(
    ("text", "phase", "trailing_untagged_blank", "expected"),
    [
        ("", "final_answer", False, ""),
        (" \n\t ", "final_answer", False, ""),
        ("supported final", "final_answer", False, "supported final"),
        ("\x1c", "final_answer", False, "\x1c"),
        (" \n\t ", None, False, "Synthetic intermediate commentary"),
        ("", "final_answer", True, ""),
    ],
    ids=[
        "blank",
        "whitespace",
        "nonblank",
        "rust-nonblank-separator",
        "untagged-whitespace",
        "untagged-after-final-blank",
    ],
)
async def test_canonical_text_phase_belongs_to_exact_child_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    phase: str | None,
    trailing_untagged_blank: bool,
    expected: str,
) -> None:
    answer = phased_answer(text, final_phase=phase, trailing_untagged_blank=trailing_untagged_blank)
    replies = deque([answer, [assistant_output("fresh child answer")]])
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        ordinary_reply = peer.response

        def scripted_reply() -> list[dict[str, Any]]:
            result = ordinary_reply()
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] != "/root":
                return replies.popleft()
            return result

        monkeypatch.setattr(peer, "response", scripted_reply)
        agent = agent_for(peer, executor)
        try:
            async with agent.session(environment=declaration(executor)) as session:
                root = await session.submit("Start one native child")
                assert (await root.get_result()).final_output == "root complete"
                await handles(session, 1)
                [child] = [entry async for entry in session.subagents()]
                first = child.turn
                assert first is not None
                result = await first.get_result()
                assert result.turn is first and first.child_id == child.id
                observations = [event async for event in first.events()]
                assert isinstance(observations[-1], TurnFinished)
                assert (observations[-1].turn_id, observations[-1].status) == (
                    first.turn_id,
                    "completed",
                )
                saved = await session.save_state()
                [stored] = [row for row in saved.descendants if row.thread_id == child.id]
                assert_phased_answer_in_history(stored.history, answer)
                later = await child.start("Answer a fresh child question")
                assert later.turn_id != first.turn_id and later.child_id == first.child_id
                assert (await later.get_result()).final_output == "fresh child answer"
                captured = await first.get_result()
                assert captured.turn is first and captured.thread_usage == result.thread_usage
                assert captured.final_output == result.final_output
                assert not replies and len(peer.child_requests["alpha"]) == 2
                assert result.final_output == expected
        finally:
            await agent.provider.aclose()
