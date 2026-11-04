"""Provider item identity under overlapping output items."""

import json

import pytest

from agents import Agent, ReplaceArguments
from agents.observation import ExecutionItemCompleted
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_child_agents import trust
from tests.test_native_executor_tools import declaration
from tests.test_native_provider_websockets import peer as websocket_peer


@pytest.mark.parametrize("replace", [False, True], ids=["allow", "edit"])
async def test_environment_interleaved_message_and_call_ids(tmp_path, monkeypatch, replace):
    from agents import Allow

    def before(ctx, value):
        assert ctx.call_id == "call-overlap"
        return ReplaceArguments({"cmd": "printf accepted-overlap"}) if replace else Allow()

    async with (
        timeout(35),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        trust(executor, tmp_path, monkeypatch)
        message = assistant_output("I will check the workspace.")
        message["item"]["phase"] = "commentary"
        call = function_call("exec_command", {"cmd": "printf original-overlap"}, "call-overlap")
        call["item"]["id"] = "fc_overlapProvider"
        # The native Responses stream can carry separate output indexes.
        # Complete the tool while the non-tool item has not yet completed.
        model.respond(
            {"type": "response.output_item.added", "output_index": 0, "item": message["item"]},
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {**call["item"], "arguments": ""},
            },
            {**call, "output_index": 1},
            {**message, "output_index": 0},
        )
        model.respond(assistant_output("finished"))
        agent = Agent(instructions="Use environment tools.", tools=[], provider=model.provider())
        async with agent.session(
            environment=declaration(executor, transform_arguments=before)
        ) as session:
            turn = await session.submit("Check")
            assert (await turn.get_result()).final_output == "finished"
            received = model.requests[-1]["input"]
            received_call = next(i for i in received if i.get("call_id") == "call-overlap")
            expected = "printf accepted-overlap" if replace else "printf original-overlap"
            assert json.loads(received_call["arguments"]) == {"cmd": expected}
            state = await session.save_state()
            calls = [
                row["payload"]
                for row in state.history
                if row["type"] == "response_item"
                and row["payload"].get("type") == "function_call"
                and row["payload"].get("call_id") == "call-overlap"
            ]
            assert len(calls) == 1
            assert calls[0]["id"] != message["item"]["id"]
            public = [i async for i in session.items()]
            public_calls = [
                i
                for i in public
                if i.get("call_id") == "call-overlap" and i.get("type") == "function_call"
            ]
            assert len(public_calls) == 1
            assert public_calls[0]["id"] == calls[0]["id"] == received_call["id"]
            assert any(i.get("id") == message["item"]["id"] for i in public)
            assert model.connections == 1 and not model.http_requests and not model.errors


async def test_environment_provider_id_can_equal_native_execution_id(tmp_path, monkeypatch):
    async with (
        timeout(35),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        trust(executor, tmp_path, monkeypatch)
        call_id = "fc_sharedIdentity"
        call = function_call("exec_command", {"cmd": "printf executed"}, call_id)
        call["item"]["id"] = call_id
        model.respond(call)
        model.respond(assistant_output("finished"))
        agent = Agent(instructions="Use environment tools.", tools=[], provider=model.provider())
        async with agent.session(environment=declaration(executor)) as session:
            turn = await session.submit("Check")
            assert (await turn.get_result()).final_output == "finished"
            [reference] = [
                event.item
                async for event in turn.events()
                if isinstance(event, ExecutionItemCompleted)
            ]
            assert reference["id"] == reference["call_id"] == call_id
            assert "output" not in reference
            [completion] = [
                event.item
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, ExecutionItemCompleted)
            ]
            assert completion["id"] == completion["call_id"] == call_id

            history = [item async for item in session.items()]
            [original] = [item for item in history if item["type"] == "function_call"]
            [execution] = [item for item in history if item["type"] == "command_execution"]
            assert original["id"] == original["call_id"] == call_id
            assert original["arguments"] == call["item"]["arguments"]
            assert len({item["id"] for item in history}) == len(history)
            assert execution["call_id"] == reference["call_id"]
            assert {**execution, "id": completion["id"]} == completion
            assert [item async for item in session.items()] == history
