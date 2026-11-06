"""Native manual checkpoints preserve the provider schema and exact pending Turn."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel

from agents import Agent, FunctionTool, InteractionExpiredError, InteractionRequired, NativeOutput
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_websockets import peer


class Answer(BaseModel):
    value: str


@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize("scalar", [False, True])
async def test_native_manual_checkpoint_preserves_schema_receipts_and_turn(
    websocket: bool, scalar: bool
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    effects = []
    output = NativeOutput(int if scalar else Answer)
    async with timeout(35), peer() as model:
        agent = Agent(
            instructions="Read both records and return the answer.",
            tools=[lookup],
            model="gpt-5.5",
            provider=model.provider(websocket=websocket),
            output_type=output,
        )
        model.respond(
            function_call("lookup", {"key": "A"}, "call-A"),
            function_call("lookup", {"key": "B"}, "call-B"),
        )
        async with agent.session() as source:
            turn = await source.submit("Read both")
            stream = source.interactions()
            requests = [await next_request(stream, lookup), await next_request(stream, lookup)]
            by_call = {request.call_id: request for request in requests}
            assert set(by_call) == {"call-A", "call-B"}
            first, second = by_call["call-A"], by_call["call-B"]
            effects.append(first.arguments.key)
            await first.respond("accepted-A")
            saved = SessionState.from_json((await source.suspend()).to_json())
            assert saved.native_continuation is not None
            assert saved.native_continuation.turn_id == turn.turn_id
            assert [item.call_id for item in saved.native_continuation.pending_manual] == ["call-B"]
            for stale in (first, second):
                with pytest.raises((RuntimeError, ValueError)):
                    await stale.respond("stale")
            with pytest.raises(InteractionRequired):
                await turn.get_result()
        assert len(model.requests) == 1
        expected_format = model.requests[0]["text"]["format"]
        assert expected_format["type"] == "json_schema" and expected_format["strict"] is True
        assert saved.native_continuation.output_schema == expected_format["schema"]
        # Resealing a restored boundary must retain its schema without inference.
        async with agent.session(state=saved) as intermediate:
            previous = await next_request(intermediate.interactions(), lookup)
            saved = SessionState.from_json((await intermediate.suspend()).to_json())
            assert saved.native_continuation.output_schema == expected_format["schema"]
        async with agent.session(state=saved) as restored:
            handle = restored.restored_turn
            assert handle is not None and handle.turn_id == turn.turn_id
            fresh = await next_request(restored.interactions(), lookup)
            assert (fresh.call_id, fresh.turn_id) == (second.call_id, second.turn_id)
            assert len(model.requests) == 1
            with pytest.raises((RuntimeError, ValueError)):
                await previous.respond("stale")
            model.respond(assistant_output('{"response":7}' if scalar else '{"value":"done"}'))
            effects.append(fresh.arguments.key)
            await fresh.respond("accepted-B")
            result = await handle.get_result()
            assert result.turn is handle and result.turn.turn_id == turn.turn_id
            assert result.final_output == (7 if scalar else Answer(value="done"))
            with pytest.raises(InteractionExpiredError):
                await fresh.respond("duplicate")
            idle = await restored.save_state()
        assert effects == ["A", "B"]
        assert len(model.requests) == 2
        assert model.requests[1]["text"]["format"] == expected_format
        assert tool_output(model.requests[1], "call-A") == "accepted-A"
        assert tool_output(model.requests[1], "call-B") == "accepted-B"
        assert (
            sum(
                item.get("type") == "function_call_output" and item.get("call_id") == "call-A"
                for item in model.requests[1]["input"]
            )
            == 1
        )
        assert idle.native_continuation is None


@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize("synchronous", [False, True])
async def test_native_one_shot_automatic_pause_preserves_schema(websocket, synchronous) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with timeout(30), peer() as model:
        agent = Agent(
            instructions="Read A.",
            tools=[lookup],
            model="gpt-5.5",
            provider=model.provider(websocket=websocket),
            output_type=Answer,
        )
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        with pytest.raises(InteractionRequired) as paused:
            if synchronous:
                await asyncio.to_thread(agent.run_sync, "Read A")
            else:
                await agent.run("Read A")
        saved = paused.value.state
        assert saved is not None and saved.native_continuation is not None
        assert len(model.requests) == 1
        schema = model.requests[0]["text"]["format"]
        assert saved.native_continuation.output_schema == schema["schema"]
        replied = []

        def answer(request):
            current = lookup.match(request)
            assert current is not None
            assert current.turn_id == saved.native_continuation.turn_id
            replied.append(current.call_id)
            return current.reply("accepted-A")

        model.respond(assistant_output('{"value":"done"}'))
        if synchronous:
            result = await asyncio.to_thread(
                agent.run_sync, None, state=saved, on_interaction=answer
            )
        else:
            result = await agent.run(None, state=saved, on_interaction=answer)
        assert result.final_output == Answer(value="done")
        assert result.turn.turn_id == saved.native_continuation.turn_id
        assert replied == ["call-A"] and len(model.requests) == 2
        assert model.requests[1]["text"]["format"] == schema
        assert schema["type"] == "json_schema" and schema["strict"] is True
        assert tool_output(model.requests[1], "call-A") == "accepted-A"


@pytest.mark.parametrize("current_typed", [False, True])
async def test_historical_typed_manual_frame_refuses_restore_before_provider(
    current_typed: bool,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with timeout(25), peer() as model:
        agent = Agent(
            instructions="Read A.", tools=[lookup], model="gpt-5.5", provider=model.provider()
        )
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        async with agent.session() as source:
            await source.submit("Read A")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        raw = json.loads(saved.to_json())
        # A schema inserted into an existing plain frame is not its captured contract.
        raw["native_continuation"]["output_schema"] = Answer.model_json_schema()
        saved = SessionState.from_json(json.dumps(raw))
        restored = Agent(
            instructions=agent.instructions,
            tools=[lookup],
            model=agent.model,
            provider=agent.provider,
            output_type=Answer if current_typed else str,
        )
        with pytest.raises(ValueError, match="output (schema|contract)"):
            async with restored.session(state=saved):
                pytest.fail("Opened a paused turn with a mismatched output contract")
        assert len(model.requests) == 1


@pytest.mark.parametrize("synchronous", [False, True])
async def test_native_manual_restore_one_shot_preserves_schema_and_turn(synchronous: bool) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with timeout(30), peer() as model:
        agent = Agent(
            instructions="Read the record.",
            tools=[lookup],
            model="gpt-5.5",
            provider=model.provider(),
            output_type=Answer,
        )
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        async with agent.session() as source:
            original = await source.submit("Read A")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        answered = []

        def answer(request):
            current = lookup.match(request)
            assert current is not None and current.turn_id == original.turn_id
            answered.append(current.call_id)
            return current.reply("accepted-A")

        model.respond(assistant_output('{"value":"done"}'))
        if synchronous:
            result = await asyncio.to_thread(
                agent.run_sync, None, state=saved, on_interaction=answer
            )
        else:
            result = await agent.run(None, state=saved, on_interaction=answer)
        assert result.final_output == Answer(value="done")
        assert result.turn.turn_id == original.turn_id
        assert answered == ["call-A"] and len(model.requests) == 2
        assert model.requests[1]["text"]["format"] == model.requests[0]["text"]["format"]
        assert model.requests[1]["text"]["format"]["strict"] is True
        assert tool_output(model.requests[1], "call-A") == "accepted-A"


@pytest.mark.parametrize("mismatch", ["schema", "missing-schema", "fingerprint", "declaration"])
async def test_native_manual_restore_rejects_changed_contract_before_opening(mismatch) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with timeout(25), peer() as model:
        agent = Agent(
            instructions="Read.",
            tools=[lookup],
            model="gpt-5.5",
            provider=model.provider(),
            output_type=Answer,
        )
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        async with agent.session() as source:
            await source.submit("Read A")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        raw = json.loads(saved.to_json())
        if mismatch == "schema":
            raw["native_continuation"]["output_schema"]["properties"]["value"]["type"] = "integer"
        elif mismatch == "missing-schema":
            raw["native_continuation"].pop("output_schema")
        elif mismatch == "fingerprint":
            raw["format_version"] = 3
            raw.pop("output_version", None)
            raw.pop("output_fingerprint", None)
            raw.pop("operation_tools", None)
            raw.get("agent_options", {}).pop("_sdk_default_tools", None)
        else:
            agent = agent.with_output_type(int)
        with pytest.raises(ValueError, match="output (schema|contract)"):
            async with agent.session(state=SessionState.from_json(json.dumps(raw))):
                pytest.fail("Opened a native checkpoint with a changed output contract")
        assert len(model.requests) == 1
