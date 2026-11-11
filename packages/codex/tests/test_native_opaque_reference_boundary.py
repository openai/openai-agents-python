"""Opaque service history IDs must not become fabricated native replay authority."""

import pytest

from agents import Agent, NativeError
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_named_control_paths import provider_for


async def test_opaque_item_reference_public_input_rejects_before_dispatch(responses):
    async with timeout(15), provider_for(responses) as provider:
        agent = Agent(model="gpt-5.5", instructions="Answer.", provider=provider)
        with pytest.raises(ValueError, match="^Unsupported user content type$"):
            await agent.run(
                {
                    "role": "user",
                    "content": [{"type": "item_reference", "id": "service-owned-opaque-reference"}],
                }
            )
    assert responses.requests == []


async def test_malformed_item_reference_preparation_rejects_before_dispatch(
    responses,
):
    calls = []

    async def edit(turn, request, **kwargs):
        calls.append(kwargs)
        request["input"].append({"type": "item_reference", "id": 17})
        return request

    async with timeout(15), provider_for(responses) as provider:
        agent = Agent(model="gpt-5.5", instructions="Answer.", provider=provider)
        async with agent.session() as session:
            with pytest.raises((NativeError, RuntimeError, ValueError)):
                await (await session.submit("answer", prepare_request=edit)).get_result()
            assert "item_reference" not in (await session.save_state()).to_json()
    assert len(calls) == 1
    assert responses.requests == []


async def test_typed_item_reference_preparation_preserves_provider_id_on_wire(responses):
    reference = {"type": "item_reference", "id": "service-owned-opaque-reference"}
    calls = []
    responses.respond(assistant_output("reference accepted"))

    async def edit(turn, request, **kwargs):
        calls.append(kwargs)
        request["input"].append(reference)
        return request

    async with timeout(15), provider_for(responses) as provider:
        agent = Agent(model="gpt-5.5", instructions="Answer.", provider=provider)
        async with agent.session() as session:
            result = await (await session.submit("answer", prepare_request=edit)).get_result()
    assert result.final_output == "reference accepted"
    assert len(calls) == 1
    assert len(responses.requests) == 1
    assert [
        item for item in responses.requests[0]["input"] if item["type"] == "item_reference"
    ] == [reference]
