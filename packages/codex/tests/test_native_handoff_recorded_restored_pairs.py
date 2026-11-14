import json
from dataclasses import replace
from hashlib import sha256

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    FunctionTool,
    HandoffInput,
    NativeProvider,
    PendingRequests,
    SessionState,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import next_request


class EmptyInput(BaseModel):
    pass


@pytest.mark.parametrize("mode", ["manual", "approval"])
async def test_recorded_handoff_can_omit_a_pair_completed_after_restore(responses, mode):
    output = "private current result after restore"
    effects = []
    preparations = []

    @function_tool(needs_approval=True)
    def managed() -> str:
        effects.append("ran")
        return output

    gate = (
        FunctionTool(
            name="read", description="Read a value.", input_type=EmptyInput, output_type=str
        )
        if mode == "manual"
        else managed
    )
    destination = Agent(name="destination", instructions="Handle the transfer.")

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        assert value.target is destination
        assert sum(item.get("call_id") == "held-read" for item in value.generated) == 2
        assert output in json.dumps(value.generated)
        preparations.append(value.target)
        visible = tuple(item for item in value.generated if item.get("call_id") != "held-read")
        return replace(value, generated=visible, recorded_generated=visible)

    responses.respond(
        function_call("transfer_to_destination", {}, "accepted-handoff"),
        function_call(gate.name, {}, "held-read"),
    )
    responses.respond(assistant_output("destination complete"))
    async with (
        timeout(30),
        NativeProvider(base_url=responses.url, api_key="synthetic-handoff-manual") as provider,
    ):
        source = Agent(
            name="source",
            instructions="Read and transfer.",
            model="gpt-5.5",
            provider=provider,
            tools=[gate],
            handoffs=[destination],
            prepare_handoff_input=prepare,
        )
        async with source.session() as session:
            turn = await session.submit("Read.")
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            saved = (await session.suspend()).to_json()
        assert effects == [] and preparations == [] and len(responses.requests) == 1
        detached = SessionState.from_json(saved, expected_sha256=sha256(saved.encode()).hexdigest())
        async with source.session(state=detached) as session:
            assert session.restored_turn is not None
            request = await next_request(session.interactions(), gate)
            assert request.call_id == "held-read"
            if mode == "approval":
                await request.approve()
            else:
                await request.respond(output)
            assert (await session.restored_turn.get_result()).final_output == "destination complete"
            assert await session.get_active_agent() is destination
            snapshot = await session.history_snapshot()
            assert all(item.get("call_id") != "held-read" for item in snapshot.items)
            assert output not in json.dumps(snapshot.items)
    assert effects == (["ran"] if mode == "approval" else [])
    assert preparations == [destination]
    assert len(responses.requests) == 2
    assert output not in json.dumps(responses.requests[-1]["input"])
