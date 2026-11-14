"""Standard Responses retains explicit reasoning options through native state."""

from agents import Agent, ModelSettings
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_request_controls import MODEL, provider_for


async def test_reasoning_options_round_trip(responses: ResponsesFixture) -> None:
    for output in ("first", "restored", "override", "cleared"):
        responses.respond(assistant_output(output))
    extra = {"context": "all_turns", "mode": "pro", "vendor": {"budget": 123}}
    settings: ModelSettings = {"reasoning_effort": "medium", "reasoning_options": extra}
    async with timeout(30), provider_for(responses) as provider:
        agent = Agent(
            model=MODEL, instructions="Answer.", provider=provider, model_settings=settings
        )
        extra["mode"] = "mutated"
        async with agent.session() as session:
            assert (await (await session.submit("Begin.")).get_result()).final_output == "first"
            state = await session.save_state()
        async with agent.session(state=state) as restored:
            assert (
                await (await restored.submit("Continue.")).get_result()
            ).final_output == "restored"
            await restored.update(model_settings={"reasoning_options": {"context": None}})
            await (await restored.submit("Change the reasoning context.")).get_result()
            await restored.update(model_settings={"reasoning_options": None})
            await (await restored.submit("Reset options.")).get_result()

    first, restored, override, cleared = [request["reasoning"] for request in responses.requests]
    expected = {
        "context": "all_turns",
        "mode": "pro",
        "vendor": {"budget": 123},
        "effort": "medium",
        "summary": "auto",
    }
    assert first == expected
    assert restored == expected
    assert override == {"context": None, "effort": "medium", "summary": "auto"}
    assert cleared == {"effort": "medium", "summary": "auto"}
