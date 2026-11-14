from __future__ import annotations

import asyncio

import pytest

from agents import Agent, ModelCompletionContext, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_provider_websockets import peer as websocket_peer


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["accept", "reject", "cancel"])
async def test_lite_websocket_model_hook_gates_native_tool(behavior):
    seen = []
    effects = []
    entered, release = asyncio.Event(), asyncio.Event()
    application = object()
    rejected = ValueError("application rejected completion")

    @function_tool
    def lookup() -> str:
        effects.append("executed")
        return "value"

    async def after_model(event: ModelCompletionContext[object]):
        assert event.context is application
        seen.append(event)
        if len(seen) == 1:
            entered.set()
            await release.wait()
            if behavior == "reject":
                raise rejected

    async with timeout(15), websocket_peer() as peer:
        peer.respond(function_call("lookup", {}, "one-call"))
        if behavior == "accept":
            peer.respond(assistant_output("Complete."))
        agent = Agent(
            instructions="Use lookup.",
            model="gpt-6.1-sol",
            provider=peer.provider(),
            tools=[lookup],
            model_settings={"request_transport": "websocket"},
            after_model=after_model,
        )
        async with agent.session(context=application) as session:
            turn = await session.submit("Question.")
            waiting = asyncio.create_task(turn.get_result())
            entering = asyncio.create_task(entered.wait())
            try:
                completed, _ = await asyncio.wait(
                    [waiting, entering], return_when=asyncio.FIRST_COMPLETED
                )
                if waiting in completed:
                    await waiting
                    pytest.fail("Turn completed without awaiting the model callback")
                assert not effects
                assert len(peer.requests) == 1
                assert seen[0].items[0]["call_id"] == "one-call"
                if behavior == "cancel":
                    await turn.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
                elif behavior == "reject":
                    release.set()
                    with pytest.raises(ValueError) as error:
                        await waiting
                    assert error.value is rejected
                else:
                    release.set()
                    assert (await waiting).final_output == "Complete."
            finally:
                release.set()
                entering.cancel()
                waiting.cancel()
                await asyncio.gather(entering, waiting, return_exceptions=True)
    assert not peer.errors
    assert not peer.http_requests
    metadata = peer.frames[0]["client_metadata"]
    assert metadata["ws_request_header_x_openai_internal_codex_responses_lite"] == "true"
    assert peer.requests[0]["reasoning"]["context"] == "all_turns"
    assert peer.requests[0]["input"][0]["type"] == "additional_tools"
    assert len(peer.requests) == (2 if behavior == "accept" else 1)
    assert len(seen) == (2 if behavior == "accept" else 1)
    assert effects == (["executed"] if behavior == "accept" else [])
