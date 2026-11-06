"""A dormant Python ceiling must pass native admission before it can select effects."""

from __future__ import annotations

import re
from typing import Any

import pytest

from agents import Agent, ToolContext, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent_declarations import _observe_implicit
from tests.test_native_manual_tools import provider


@pytest.mark.parametrize("bad_ceiling", ["duplicate", "reserved"])
async def test_dormant_ceiling_validation_uses_opening_admission(
    responses: ResponsesFixture, monkeypatch: Any, bad_ceiling: str
) -> None:
    created = _observe_implicit(monkeypatch, responses.url)
    effects: list[str] = []

    @function_tool(name="lookup")
    def first(key: str) -> str:
        effects.append("first")
        return key

    @function_tool(name="lookup" if bad_ceiling == "duplicate" else "apply_patch")
    def other(key: str) -> str:
        effects.append("other")
        return key

    # The all-active Agent exercises the existing native admission rule.
    expected = (
        "Duplicate Python tool: lookup"
        if bad_ceiling == "duplicate"
        else "Python tool uses a reserved name: apply_patch"
    )
    dormant = Agent(instructions="Read.", tools=[first, other])
    with pytest.raises(ValueError, match=re.escape(expected)):
        dormant.validate()
    assert not effects and not created and not responses.requests


@pytest.mark.parametrize("bad_ceiling", ["duplicate", "reserved"])
async def test_dormant_ceiling_rejected_before_implicit_provider_or_effects(
    responses: ResponsesFixture, monkeypatch: Any, bad_ceiling: str
) -> None:
    created = _observe_implicit(monkeypatch, responses.url)
    effects: list[str] = []

    @function_tool(name="lookup")
    def first(key: str) -> str:
        effects.append("first")
        return key

    @function_tool(name="lookup" if bad_ceiling == "duplicate" else "apply_patch")
    def other(key: str) -> str:
        effects.append("other")
        return key

    with pytest.raises(ValueError) as offline:
        Agent(instructions="Read.", tools=[first, other]).validate()
    roster = [first, other]
    agent = Agent(instructions="Read.", tools=roster)
    with pytest.raises(ValueError, match=re.escape(str(offline.value))):
        async with timeout(15), agent.session() as session:
            # The reported failure must never get as far as choosing the
            # later callback for the first declaration's identity.
            responses.respond(function_call("lookup", {"key": "value"}, "chosen"))
            responses.respond(assistant_output("done"))
            assert (
                await (await session.submit("Read.", tools=[first.ref])).get_result()
            ).final_output == "done"
            assert effects != ["other"], "selecting the first identity ran the later effect"
    assert not effects and not created and not responses.requests


async def test_dormant_context_tool_needs_no_binding_until_it_is_selected(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    def dormant(ctx: ToolContext[list[str]], key: str) -> str:
        ctx.context.append(key)
        return key

    roster = [dormant]
    assert Agent(instructions="Read.", tools=roster).validate() is None
    async with (
        timeout(15),
        Agent(
            instructions="Read.", tools=roster, provider=provider(responses)
        ).session() as session,
    ):
        with pytest.raises(TypeError, match="context"):
            await session.submit("Read", tools=[dormant.ref])
        responses.respond(assistant_output("still usable"))
        assert (
            await (await session.submit("Read.", tools=[])).get_result()
        ).final_output == "still usable"
