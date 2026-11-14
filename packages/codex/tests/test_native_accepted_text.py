"""Application-approved prefixes have been reaching callers during native generation."""

import asyncio
from contextlib import aclosing

import pytest

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses, text_response


@pytest.mark.parametrize("cancel_successor", [False, True])
async def test_late_reader_skips_retired_step_before_live_automatic_successor(
    responses, monkeypatch, cancel_successor
):
    from agents import TurnStoppedError
    from agents.observation import OutputTextDelta
    from tests.test_review_ii_steering_tail import ProjectContinuation

    successor_entered = asyncio.Event()
    release = asyncio.Event()
    responses.respond(*text_response("earlier", "Retired private text."))
    responses.respond(*text_response("successor", "Live prefix."))

    async def hold(event):
        if len(responses.requests) == 2 and event["type"] == "response.completed":
            successor_entered.set()
            await release.wait()

    responses.before_event = hold
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        async with Agent(instructions="Answer.", provider=provider).session() as session:
            # Project only the existing controlled unsampled receipt; both native turns run.
            monkeypatch.setattr(session, "_native", ProjectContinuation(session._native))
            turn = await session.submit("Answer.")
            calls = []
            reader = turn.accepted_text(lambda text: calls.append(text) or text)
            try:
                await successor_entered.wait()
                async for event in turn.events():
                    if isinstance(event, OutputTextDelta) and event.item_id == "successor":
                        successor_id = event.turn_id
                        break
                else:
                    pytest.fail("The native successor has not been streaming")
                assert successor_id != turn.turn_id
                if cancel_successor:
                    await turn.cancel()
                    assert [chunk async for chunk in reader] == []
                    assert calls == []
                    with pytest.raises(TurnStoppedError):
                        await turn.get_result()
                else:
                    chunk = await anext(reader)
                    assert (chunk.text, chunk.turn_id) == ("Live prefix.", successor_id)
                    assert calls == ["Live prefix."]
            finally:
                release.set()
                await reader.aclose()
            if not cancel_successor:
                assert (await turn.get_result()).final_output == "Live prefix."


async def test_approved_prefix_arrives_before_provider_finishes_and_rejected_tail_stays_private(
    responses,
):
    approved = asyncio.Event()
    responses.delta_gate = approved
    responses.respond(*text_response("independent", "Approved.", " private rejected tail"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        async with Agent(
            instructions="Answer.", model="fixture", provider=provider
        ).session() as session:
            turn = await session.submit("Answer.")
            chunks = []
            async for chunk in turn.accepted_text(
                lambda pending: "Approved." if pending.startswith("Approved.") else None
            ):
                assert not approved.is_set()
                assert session.active_turn is turn
                chunks.append(chunk)
                approved.set()
            assert [chunk.text for chunk in chunks] == ["Approved."]
            assert chunks[0].submission_id == turn.submission_id
            assert chunks[0].turn_id == turn.turn_id
            assert chunks[0].item_id == "independent"
            assert (await turn.get_result()).final_output == "Approved. private rejected tail"


async def test_retired_reader_never_approves_old_text_during_a_new_root(responses):
    responses.respond(*text_response("old", "Old private text"))
    responses.respond(assistant_output("New answer"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        async with Agent(
            instructions="Answer.", model="fixture", provider=provider
        ).session() as session:
            old = await session.submit("Old.")
            await old.get_result()
            newer = await session.submit("New.")
            calls = []
            assert [
                chunk async for chunk in old.accepted_text(lambda text: calls.append(text) or text)
            ] == []
            assert not calls
            assert (await newer.get_result()).final_output == "New answer"


async def test_rejected_candidate_stays_private_while_later_item_streams(responses):
    from agents import ContinueTurn

    approved = asyncio.Event()
    responses.respond(*text_response("rejected", "Unaccepted candidate."))
    responses.respond(*text_response("approved", "Independent accepted sentence."))

    async def hold_final(event):
        if len(responses.requests) == 2 and event["type"] == "response.completed":
            await approved.wait()

    responses.before_event = hold_final
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            instructions="Answer.",
            model="fixture",
            provider=provider,
            before_complete=lambda request: (
                ContinueTurn("Revise.") if request.text == "Unaccepted candidate." else None
            ),
        )
        async with agent.session() as session:
            turn = await session.submit("Answer.")
            received = []
            async for chunk in turn.accepted_text(
                lambda pending: pending if pending == "Independent accepted sentence." else None
            ):
                assert len(responses.requests) == 2
                assert not approved.is_set()
                received.append(chunk.text)
                approved.set()
            assert received == ["Independent accepted sentence."]
            assert (await turn.get_result()).final_output == received[0]


async def test_cancel_while_application_gate_waits_never_publishes_late_approval(responses):
    entered = asyncio.Event()
    release = asyncio.Event()
    finish_provider = asyncio.Event()
    responses.delta_gate = finish_provider
    responses.respond(*text_response("cancelled", "Private candidate."))

    async def accept(pending):
        entered.set()
        await release.wait()
        return pending

    try:
        async with (
            timeout(30),
            NativeProvider(
                base_url=responses.url, api_key="synthetic", supports_websockets=False
            ) as provider,
        ):
            async with Agent(
                instructions="Answer.", model="fixture", provider=provider
            ).session() as session:
                turn = await session.submit("Answer.")
                reader = turn.accepted_text(accept)
                pending = asyncio.create_task(anext(reader))
                try:
                    await entered.wait()
                    await turn.cancel()
                    release.set()
                    with pytest.raises(StopAsyncIteration):
                        await pending
                finally:
                    release.set()
                    await reader.aclose()
    finally:
        finish_provider.set()


async def test_native_terminal_during_approval_withholds_original_text_while_repair_finishes(
    responses,
):
    from agents import PromptedOutput

    approval_entered = asyncio.Event()
    repair_entered = asyncio.Event()
    release_repair = asyncio.Event()
    responses.respond(*text_response("original", "invalid"))
    responses.respond(assistant_output("7"))

    async def hold(event):
        if len(responses.requests) == 1 and event["type"] == "response.completed":
            await approval_entered.wait()
        if len(responses.requests) == 2:
            repair_entered.set()
            await release_repair.wait()

    responses.before_event = hold
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        async with Agent(
            instructions="Return a number.", model="fixture", provider=provider
        ).session() as session:
            turn = await session.submit("Answer.", output_type=PromptedOutput(int, max_retries=1))

            async def accept(pending):
                approval_entered.set()
                await repair_entered.wait()
                return pending

            stream = turn.accepted_text(accept)
            try:
                with pytest.raises(StopAsyncIteration):
                    await anext(stream)
            finally:
                release_repair.set()
                await stream.aclose()
            assert (await turn.get_result()).final_output == 7
            assert len(responses.requests) == 2


@pytest.mark.parametrize("queued", [False, True])
async def test_reader_skips_only_eviction_before_its_own_admission(responses, queued):
    responses.respond(*text_response("prior", *(["x"] * 300)))
    approved = asyncio.Event()
    blocker_entered = asyncio.Event()
    blocker_release = asyncio.Event()

    async def hold_completion(event):
        if event["type"] != "response.completed":
            return
        if queued and len(responses.requests) == 2:
            blocker_entered.set()
            await blocker_release.wait()
        else:
            await approved.wait()

    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        async with Agent(
            instructions="Answer.", model="fixture", provider=provider
        ).session() as session:
            await (await session.submit("Earlier.")).get_result()
            responses.before_event = hold_completion
            if queued:
                responses.respond(*text_response("blocker", "Blocking."))
                blocker = await session.submit("Block dispatch.")
                await blocker_entered.wait()
            responses.respond(*text_response("current", "Approved."))
            turn = await session.submit("Current.", if_busy="queue" if queued else "reject")
            read = asyncio.create_task(anext(turn.accepted_text(lambda text: text)))
            try:
                if queued:
                    await asyncio.sleep(0)
                    blocker_release.set()
                chunk = await read
                assert chunk.text == "Approved."
                assert chunk.submission_id == turn.submission_id
                assert not approved.is_set()
            finally:
                blocker_release.set()
                approved.set()
            assert (await turn.get_result()).final_output == "Approved."
            if queued:
                assert (await blocker.get_result()).final_output == "Blocking."


async def test_reader_still_fails_closed_when_current_turn_observations_are_evicted(responses):
    ready = asyncio.Event()
    release = asyncio.Event()

    async def hold_completion(event):
        if event["type"] == "response.completed":
            ready.set()
            await release.wait()

    responses.before_event = hold_completion
    responses.respond(*text_response("current", *(["x"] * 300), "last"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        async with (
            Agent(instructions="Answer.", model="fixture", provider=provider).session() as session,
            aclosing(session.events()),
        ):
            turn = await session.submit("Current.")
            await ready.wait()
            from agents.observation import OutputTextDelta

            async for event in session.events():
                if isinstance(event, OutputTextDelta) and event.delta.endswith("last"):
                    break
            calls = []
            try:
                with pytest.raises(RuntimeError, match="observation gap"):
                    await anext(turn.accepted_text(lambda text: calls.append(text) or text))
                assert not calls
            finally:
                release.set()
            assert (await turn.get_result()).final_output == "x" * 300 + "last"
