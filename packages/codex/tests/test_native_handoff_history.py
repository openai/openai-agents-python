import json
from dataclasses import replace

import pytest

from agents import Agent, HandoffInput, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


@pytest.mark.parametrize(("projected", "retained"), [(False, False), (True, False), (True, True)])
async def test_idle_replacement_preserves_active_destination_across_restore(
    responses, projected, retained
):
    prepared = []

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        prepared.append("prepared")
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "sampling summary"}],
                },
            ),
            previous=(),
            generated=(),
        )

    target = Agent(name="B", instructions="DESTINATION authority.")
    async with (
        timeout(30),
        NativeProvider(base_url=responses.url, api_key="synthetic-idle-replacement") as provider,
    ):
        source = Agent(
            name="A",
            instructions="SOURCE authority.",
            provider=provider,
            handoffs=[target],
            prepare_handoff_input=prepare if projected else None,
        )
        responses.respond(function_call("transfer_to_b", {}, "transfer-call"))
        responses.respond(assistant_output("first complete"))
        async with source.session() as session:
            assert (
                await (await session.submit("original-private")).get_result()
            ).final_output == "first complete"
            assert await session.get_active_agent() is target
            before = await session.history_snapshot()
            assert "original-private" in json.dumps(before.items)
            replacement = {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "canonical replacement"}],
            }
            revision = await session.transact_history(
                expected_revision=before.revision,
                keep=before.total if retained else 0,
                items=[replacement],
            )
            after = await session.history_snapshot()
            assert after.revision == revision
            assert after.items[-1] == replacement
            if not retained:
                assert after.items == (replacement,)
            checkpoint = await session.save_state()
        responses.respond(assistant_output("second complete"))
        async with source.session(state=checkpoint) as restored:
            assert await restored.get_active_agent() is target
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "second complete"
        assert prepared == (["prepared"] if projected else [])
        request = json.dumps(responses.requests[2]["input"])
        assert "canonical replacement" in request
        assert "original-private" not in request
        assert ("sampling summary" in request) is (projected and retained)
        assert "DESTINATION authority." in request
        assert len(responses.requests) == 3


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("change", ["model", "instructions"])
async def test_material_profile_update_publishes_or_rejects_stale_view(
    responses, projected, change
):
    from agents import NativeError

    def prepare(value):
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "sampling summary"}],
                },
            ),
            previous=(),
            generated=(),
        )

    target = Agent(name="B", instructions="DESTINATION authority.")
    async with (
        timeout(30),
        NativeProvider(base_url=responses.url, api_key="synthetic-material-update") as provider,
    ):
        source = Agent(
            name="A",
            instructions="SOURCE authority.",
            provider=provider,
            handoffs=[target],
            prepare_handoff_input=prepare if projected else None,
        )
        responses.respond(function_call("transfer_to_b", {}, "transfer-call"))
        responses.respond(assistant_output("first complete"))
        async with source.session() as session:
            assert (
                await (await session.submit("original-private")).get_result()
            ).final_output == "first complete"
            before = json.loads((await session.save_state()).to_json())

            def publications(state):
                return [
                    row
                    for row in state["history"]
                    if row.get("metadata", {}).get("sdk_agent_profile")
                ]

            previous = publications(before)[-1]
            kwargs = (
                {"model": "gpt-5.5"}
                if change == "model"
                else {"instructions": "CHANGED destination authority."}
            )
            if projected:
                with pytest.raises(
                    (NativeError, RuntimeError, ValueError),
                    match="Handoff presentation differs from committed destination",
                ):
                    await session.update(**kwargs)
                assert len(responses.requests) == 2
                with pytest.raises(
                    RuntimeError,
                    match="Session cannot continue after an unconfirmed native operation",
                ):
                    await session.save_state()
            else:
                await session.update(**kwargs)
                after = json.loads((await session.save_state()).to_json())
                latest = publications(after)[-1]
                assert len(publications(after)) == len(publications(before)) + 1
                assert (
                    latest["metadata"]["sdk_agent_profile"]["epoch"]
                    == previous["metadata"]["sdk_agent_profile"]["epoch"] + 1
                )
                assert latest["payload"]["id"] != previous["payload"]["id"]
                if change == "model":
                    assert latest["metadata"]["sdk_agent_profile"]["model"]["model"] == "gpt-5.5"
                    assert len(responses.requests) == 2
                else:
                    responses.respond(assistant_output("updated destination complete"))
                    assert (
                        await (await session.submit("continue")).get_result()
                    ).final_output == "updated destination complete"
                    assert "CHANGED destination authority." in json.dumps(
                        responses.requests[2]["input"]
                    )
                    assert "sampling summary" not in json.dumps(responses.requests[2]["input"])
