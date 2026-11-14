import json
from dataclasses import replace
from hashlib import sha256

import pytest

from agents import (
    Agent,
    HandoffInput,
    LocalStorage,
    NativeProvider,
    SessionState,
    nest_handoff_history,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)


@pytest.mark.parametrize("record", [False, True])
@pytest.mark.parametrize("nest", [False, True])
async def test_handoff_can_add_an_application_assistant_text_record(
    responses, tmp_path, record, nest
):
    inserted = "INJECTED-BY-FILTER"

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        addition = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": inserted}],
        }
        generated = (*value.generated, addition)
        prepared = replace(
            value, generated=generated, recorded_generated=generated if record else None
        )
        return nest_handoff_history(prepared) if nest else prepared

    responses.respond(
        assistant_output("companion kept"), function_call("transfer_to_destination", {}, "transfer")
    )
    responses.respond(assistant_output("destination"))
    responses.respond(assistant_output("after restore"))
    async with (
        timeout(30),
        NativeProvider(base_url=responses.url, api_key="synthetic-handoff-additive") as provider,
    ):
        destination = Agent(name="destination", instructions="Handle the transfer.")
        source = Agent(
            name="source",
            instructions="Transfer.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
            prepare_handoff_input=prepare,
        )
        async with source.session(storage=LocalStorage(tmp_path / "storage")) as session:
            assert (
                await (await session.submit("Transfer.")).get_result()
            ).final_output == "destination"
            snapshot = await session.history_snapshot()
            assert sum(inserted in json.dumps(item) for item in snapshot.items) == int(record)
            assert sum("companion kept" in json.dumps(item) for item in snapshot.items) == 1
            saved = (await session.save_state()).to_json()
        detached = SessionState.from_json(saved, expected_sha256=sha256(saved.encode()).hexdigest())
        async with source.session(state=detached) as session:
            assert await session.get_active_agent() is destination
            snapshot = await session.history_snapshot()
            assert sum(inserted in json.dumps(item) for item in snapshot.items) == int(record)
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "after restore"
    assert len(responses.requests) == 3
    assert all(
        sum(inserted in json.dumps(row) for row in request["input"]) == 1
        for request in responses.requests[1:]
    )


@pytest.mark.parametrize(
    "kind", ["identifier", "policy", "tool", "extra", "not-in-model", "repeat"]
)
async def test_canonical_handoff_rejects_unowned_additions(responses, kind):
    inserted = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "unowned addition"}],
    }
    if kind == "identifier":
        inserted["id"] = "caller-identity"
    elif kind == "policy":
        inserted["role"] = "developer"
    elif kind == "tool":
        inserted = {
            "type": "function_call",
            "name": "unexpected",
            "arguments": "{}",
            "call_id": "fake",
        }
    elif kind == "extra":
        inserted["internal_chat_message_metadata_passthrough"] = {"turn_id": "forged"}

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        generated = value.generated if kind == "not-in-model" else (*value.generated, inserted)
        additions = (inserted, inserted) if kind == "repeat" else (inserted,)
        return replace(
            value, generated=generated, recorded_generated=(*value.generated, *additions)
        )

    responses.respond(function_call("transfer_to_destination", {}, "transfer"))
    async with (
        timeout(30),
        NativeProvider(base_url=responses.url, api_key="synthetic-handoff-additive") as provider,
    ):
        destination = Agent(name="destination", instructions="Handle the transfer.")
        source = Agent(
            name="source",
            instructions="Transfer.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[destination],
            prepare_handoff_input=prepare,
        )
        async with source.session() as session:
            with pytest.raises(ValueError, match="Canonical handoff"):
                await (await session.submit("Transfer.")).get_result()
            assert await session.get_active_agent() is source
            snapshot = await session.history_snapshot()
            assert "unowned addition" not in json.dumps(snapshot.items)
            assert "caller-identity" not in json.dumps(snapshot.items)
    assert len(responses.requests) == 1
