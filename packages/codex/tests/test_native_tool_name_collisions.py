"""A captured native catalog dispatches the opted-in winner exactly once."""

import pytest

from agents import Agent, NativeProvider, function_tool
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)


def _provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(
        base_url=responses.url,
        api_key="synthetic-collision",
        supports_websockets=False,
    )


async def test_duplicate_eager_functions_are_strict_by_default(responses):
    def declaration(text: str):
        @function_tool
        def lookup() -> str:
            return text

        return lookup

    async with _provider(responses) as provider:
        agent = Agent(
            instructions="Use lookup.",
            provider=provider,
            model="gpt-5.5",
            tools=[declaration("first"), declaration("last")],
        )
        with pytest.raises((ValueError, RuntimeError), match="[Dd]uplicate|[Cc]ollision"):
            await agent.run("Start.")
    assert responses.requests == []


async def test_last_bare_tool_wins_and_survives_restore(responses, caplog):
    observed: list[str] = []

    def declaration(text: str):
        @function_tool
        def lookup() -> str:
            observed.append(text)
            return text

        return lookup

    for index in range(2):
        responses.respond(function_call("lookup", {}, f"call-{index}"))
        responses.respond(assistant_output(f"Done {index}."))
    async with _provider(responses) as provider:
        agent = Agent(
            instructions="Use lookup.",
            provider=provider,
            model="gpt-5.5",
            tools=[declaration("first"), declaration("last")],
            tool_name_collision_policy="warn",
        )
        async with agent.session() as session:
            assert (await (await session.submit("Start.")).get_result()).final_output == "Done 0."
            saved = await session.save_state()
        async with agent.session(state=saved) as session:
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "Done 1."

    assert observed == ["last", "last"]
    assert "Duplicate function or handoff name" in caplog.text
    for request in (responses.requests[0], responses.requests[2]):
        assert sum(tool.get("name") == "lookup" for tool in request["tools"]) == 1


async def test_final_handoff_takes_precedence_over_bare_functions(responses):
    observed: list[str] = []

    @function_tool
    def transfer_to_target() -> str:
        observed.append("wrong callback")
        return "wrong"

    def target(instructions: str) -> Agent[None, str]:
        return Agent(name="target", instructions=instructions)

    responses.respond(function_call("transfer_to_target", {}, "move"))
    responses.respond(assistant_output("Final."))
    async with _provider(responses) as provider:
        agent = Agent(
            instructions="Delegate.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[target("First target."), target("Last target.")],
            tools=[transfer_to_target],
            tool_name_collision_policy="warn",
        )
        assert (await agent.run("Start.")).final_output == "Final."
    assert observed == []
    assert "Last target." in str(responses.requests[-1]["input"])
    assert "First target." not in str(responses.requests[-1]["input"])


@pytest.mark.parametrize("options", [{"namespace": "functions"}, {"defer_loading": True}])
async def test_non_bare_collisions_remain_strict(responses, options):
    def declaration(text: str):
        def lookup() -> str:
            return text

        return function_tool(lookup, **options)

    async with _provider(responses) as provider:
        agent = Agent(
            instructions="Use lookup.",
            provider=provider,
            model="gpt-5.5",
            tools=[declaration("first"), declaration("last")],
            tool_name_collision_policy="warn",
        )
        with pytest.raises((ValueError, RuntimeError), match="require unique identities"):
            await agent.run("Start.")
    assert responses.requests == []
