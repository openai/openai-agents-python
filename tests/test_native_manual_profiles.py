"""Manual checkpoints retain supported transport and accepted output values."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from agents import Agent, FunctionTool, ToolOutput, ToolOutputImage, ToolOutputText, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_code_mode import code_call, declared_names
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_provider_websockets import peer
from tests.test_native_session_input import IMAGE


async def save_at_manual_boundary(session) -> SessionState:
    """Use the public settlement boundary without probing native state."""
    return await session.suspend()


@pytest.mark.parametrize(
    ("first_websocket", "restored_websocket"),
    [
        (True, True),
        (True, False),
        (False, True),
        (False, False),
    ],
)
async def test_manual_checkpoint_preserves_current_transport(
    first_websocket: bool, restored_websocket: bool
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with timeout(25), peer() as model:
        agent = Agent(
            instructions="Use lookup.",
            tools=[lookup],
            model="gpt-5.5",
            provider=model.provider(websocket=first_websocket),
        )
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        async with agent.session() as session:
            await session.submit("Look up A")
            original = await next_request(session.interactions(), lookup)
            saved = await session.suspend()
            assert saved.format_version == 5
            assert len(model.requests) == 1
        if first_websocket:
            # The peer observes client retirement on its own event-loop turn.
            await model.disconnected.wait()
        assert not model.sockets

        agent = replace(
            agent,
            provider=model.provider(
                websocket=restored_websocket, key="synthetic-current-checkpoint-key"
            ),
        )
        async with agent.session(state=SessionState.from_json(saved.to_json())) as restored:
            current = await next_request(restored.interactions(), lookup)
            assert (current.call_id, current.turn_id) == (original.call_id, original.turn_id)
            assert len(model.requests) == 1
            with pytest.raises((ValueError, RuntimeError), match="foreign|expired|closed"):
                await original.respond("stale")
            model.respond(assistant_output("continued"))
            await current.respond("answer-A")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
            finished = await restored.save_state()
            assert finished.format_version == 4
        assert len(model.requests) == 2
        assert tool_output(model.requests[1], "call-A") == "answer-A"
        assert model.request_headers[-1]["authorization"] == (
            "Bearer synthetic-current-checkpoint-key"
        )
        if first_websocket and restored_websocket:
            assert model.connections >= 2
            assert not model.http_requests
        else:
            assert len(model.http_requests) == 2 - first_websocket - restored_websocket


@pytest.mark.parametrize("picture_first", [False, True])
async def test_manual_checkpoint_preserves_completed_images_without_replay(
    responses: ResponsesFixture, picture_first: bool
) -> None:
    effects: list[str] = []
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )

    @function_tool
    async def picture(key: str) -> ToolOutput:
        effects.append(key)
        return ToolOutput(
            [
                ToolOutputText("accepted picture"),
                ToolOutputImage(IMAGE),
            ]
        )

    calls = [
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("picture", {"key": "B"}, "call-B"),
    ]
    responses.respond(*(reversed(calls) if picture_first else calls))
    agent = Agent(
        instructions="Use both tools.", tools=[lookup, picture], provider=provider(responses)
    )
    async with timeout(20):
        async with agent.session() as session:
            await session.submit("Read the record and picture")
            original = await next_request(session.interactions(), lookup)
            saved = await session.suspend()
            assert effects == ["B"]
            assert IMAGE in saved.to_json()
        responses.respond(assistant_output("continued"))
        async with agent.session(state=SessionState.from_json(saved.to_json())) as restored:
            resealed = await restored.suspend()
            assert IMAGE in resealed.to_json()
        async with agent.session(state=SessionState.from_json(resealed.to_json())) as restored:
            current = await next_request(restored.interactions(), lookup)
            assert current.turn_id == original.turn_id
            await current.respond("answer-A")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
        assert effects == ["B"]
        assert len(responses.requests) == 2
        output = next(
            item
            for item in responses.requests[1]["input"]
            if item.get("type") == "function_call_output" and item.get("call_id") == "call-B"
        )
        assert output["output"] == [
            {"type": "input_text", "text": "accepted picture"},
            {"type": "input_image", "image_url": IMAGE},
        ]


async def test_code_mode_manual_checkpoint_refuses_compacted_history(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(25), agent.session() as session:
        responses.respond(assistant_output("ready"))
        first = await session.submit("Prepare")
        await (first).get_result()
        responses.respond(assistant_output("Compacted summary: ready to look up A."))
        await session.compact()
        responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
        handle = await session.submit("Look up A")
        request = await next_request(session.interactions(), lookup)
        assert len(responses.requests) == 3
        with pytest.raises(RuntimeError, match="unused code state"):
            await save_at_manual_boundary(session)
        responses.respond(assistant_output("continued"))
        assert await request.respond("answer-A") is None
        assert (await (handle).get_result()).final_output == "continued"


@pytest.mark.parametrize("used_code,stale_probe", [(False, False), (True, False), (True, True)])
async def test_code_mode_manual_checkpoint_transfers_settled_values_or_refuses_active_cells(
    responses: ResponsesFixture, used_code: bool, stale_probe: bool
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(25):
        async with agent.session() as session:
            if used_code:
                code = 'store("remembered", 17); text("stored");'
                responses.respond(code_call(code, "code-A"))
                responses.respond(assistant_output("remembered"))
                first = await session.submit("Remember a value")
                assert (await (first).get_result()).final_output == "remembered"
                if stale_probe:
                    # Stored values commit only when a cell completes. Keep the
                    # settled seed separate from the active cell that blocks capture.
                    responses.respond(
                        code_call("yield_control(); await new Promise(() => {});", "live-cell")
                    )
                    responses.respond(assistant_output("cell still active"))
                    live = await session.submit("Keep another cell active")
                    assert (await live.get_result()).final_output == "cell still active"
            responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
            handle = await session.submit("Look up A directly")
            request = await next_request(session.interactions(), lookup)
            assert "exec" in declared_names(responses.requests[-1])
            if stale_probe:
                native = session._native

                class StaleReadiness:
                    async def _manual_ready(self) -> bool:
                        return True

                    def __getattr__(self, name: str):
                        return getattr(native, name)

                # Force only the stale readiness result. Real native capture
                # must still refuse without sealing the live turn.
                while True:
                    try:
                        await native._manual_ready()
                    except RuntimeError as error:
                        assert "active cells" in str(error)
                        break
                    await asyncio.sleep(0)
                session._native = StaleReadiness()
                try:
                    with pytest.raises(RuntimeError, match="settled manual boundary"):
                        await session.suspend()
                finally:
                    session._native = native
                responses.respond(code_call('text(load("remembered"));', "code-B"))
                responses.respond(assistant_output("continued"))
                assert await request.respond("answer-A") is None
                assert (await (handle).get_result()).final_output == "continued"
                assert "17" in tool_output(
                    responses.requests[-1], "code-B", "custom_tool_call_output"
                )
                return
            while not await session._native._manual_ready():
                await asyncio.sleep(0)
            saved = await session.suspend()
            if used_code:
                assert saved.root_code_mode["stored_values"]["remembered"] == 17
        if used_code:
            responses.respond(code_call('text(load("remembered"));', "code-B"))
        responses.respond(assistant_output("continued"))
        async with agent.session(state=saved) as session:
            resealed = await session.suspend()
        async with agent.session(state=resealed) as session:
            current = await next_request(session.interactions(), lookup)
            await current.respond("answer-A")
            assert (await session.restored_turn.get_result()).final_output == "continued"
            if used_code:
                assert "17" in tool_output(
                    responses.requests[-1], "code-B", "custom_tool_call_output"
                )
        # A seeded namespace adds its exec/assistant pair and restored read.
        assert len(responses.requests) == (5 if used_code else 2)
