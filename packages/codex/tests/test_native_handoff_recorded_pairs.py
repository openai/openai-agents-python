import json
from dataclasses import replace
from hashlib import sha256

import pytest

from agents import (
    Agent,
    HandoffInput,
    LocalStorage,
    NativeError,
    NativeProvider,
    SessionState,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)


@pytest.mark.parametrize(
    "selection",
    ["pair", "large-pair", "output-only", "call-only", "model-pair", "transfer-pair"],
)
async def test_handoff_recorded_generated_can_omit_a_settled_tool_pair(
    responses, tmp_path, selection
):
    calls = []
    accepted_turn = []
    hidden = "PRIVATE_CURRENT_HANDOFF_TOOL_OUTPUT"
    head = "PRIVATE_CURRENT_HANDOFF_TOOL_HEAD"
    body = hidden
    if selection == "large-pair":
        body = (
            head
            + ("head-0123456789\n" * 20_000)
            + hidden
            + ("tail-9876543210\n" * 20_000)
        )

    @function_tool
    def read_record() -> str:
        calls.append("read")
        return body

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        current_pair = [
            item for item in value.generated if item.get("call_id") == "source-read"
        ]
        assert {item["type"] for item in current_pair} == {
            "function_call",
            "function_call_output",
        }
        if selection == "large-pair":
            selected_output = next(
                item["output"]
                for item in current_pair
                if item["type"] == "function_call_output"
            )
            assert head in selected_output and hidden not in selected_output
        accepted_turn.append(value.target)
        omitted = {
            "pair": {"source-read"},
            "large-pair": {"source-read"},
            "output-only": {"source-read"},
            "call-only": {"source-read"},
            "model-pair": {"source-read"},
            "transfer-pair": {"source-handoff"},
        }[selection]
        recorded = tuple(
            item
            for item in value.generated
            if item.get("call_id") not in omitted
            or (
                selection in {"output-only", "model-pair"}
                and item.get("type") == "function_call"
            )
            or (selection == "call-only" and item.get("type") == "function_call_output")
        )
        model_view = value.generated if selection == "model-pair" else recorded
        return replace(value, generated=model_view, recorded_generated=recorded)

    responses.respond(
        function_call("read_record", {}, "source-read"),
        function_call("transfer_to_destination", {}, "source-handoff"),
    )
    responses.respond(assistant_output("destination"))
    responses.respond(assistant_output("cold continued"))
    async with (
        timeout(30),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-handoff-pair"
        ) as provider,
    ):
        destination = Agent(name="destination", instructions="Handle the transfer.")
        root = Agent(
            name="source",
            instructions="Read and transfer.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
            tools=[read_record],
            prepare_handoff_input=prepare,
        )
        storage = LocalStorage(tmp_path / "storage")
        async with root.session(storage=storage) as session:
            if selection == "transfer-pair":
                with pytest.raises(NativeError, match="Canonical handoff history"):
                    await (await session.submit("Read.")).get_result()
                assert await session.get_active_agent() is root
                assert hidden in json.dumps((await session.history_snapshot()).items)
                assert accepted_turn == [destination] and calls == ["read"]
                assert len(responses.requests) == 1
                return
            assert (
                await (await session.submit("Read.")).get_result()
            ).final_output == "destination"
            assert await session.get_active_agent() is destination
            assert all(
                row.get("call_id") != "source-read"
                for row in (await session.history_snapshot()).items
            )
            saved = (await session.save_state()).to_json()
        detached = SessionState.from_json(
            saved, expected_sha256=sha256(saved.encode()).hexdigest()
        )
        async with root.session(state=detached) as session:
            assert await session.get_active_agent() is destination
            assert all(
                row.get("call_id") != "source-read"
                for row in (await session.history_snapshot()).items
            )
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "cold continued"
    assert accepted_turn == [destination] and calls == ["read"]
    assert len(responses.requests) == 3
    if selection == "model-pair":
        for request in responses.requests[1:]:
            assert [
                row["type"] for row in request["input"] if row.get("call_id") == "source-read"
            ] == ["function_call", "function_call_output"]
    else:
        assert all(
            row.get("call_id") != "source-read"
            for request in responses.requests[1:]
            for row in request["input"]
        )
        assert all(
            hidden not in json.dumps(request["input"])
            and head not in json.dumps(request["input"])
            for request in responses.requests[1:]
        )
