"""Manual restore checks current authority before connecting an executor."""

import json
from dataclasses import replace

import pytest

from agents import Agent, FunctionTool, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_child_agents import trust
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import next_request
from tests.test_native_provider_websockets import peer


@pytest.mark.parametrize("invalid", ["callback", "frame"])
async def test_manual_restore_validates_before_executor_connect(tmp_path, monkeypatch, invalid):
    effects = []

    @function_tool(description="Read a saved record")
    async def lookup(key: str) -> str:
        effects.append(key)
        return key

    manual = FunctionTool(
        name=lookup.name,
        description=lookup.description,
        input_type=lookup.input_type,
        output_type=lookup.output_type,
    )
    async with (
        timeout(30),
        peer() as model,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        # The suspended source had no Environment; keep its legacy write policy.
        agent = Agent(instructions="Read A.", tools=[manual], provider=model.provider())
        model.respond(function_call("lookup", {"key": "A"}, "call-A"))
        async with agent.session() as source:
            await source.submit("Read A")
            await next_request(source.interactions(), manual)
            good = await source.suspend()
        raw = json.loads(good.to_json())
        if invalid == "frame":
            raw["native_continuation"]["pending_manual"][0]["arguments"] = '{"key":"forged"}'
        state = SessionState.from_json(json.dumps(raw))
        current = replace(agent, tools=[lookup]) if invalid == "callback" else agent
        error = "not a manual tool" if invalid == "callback" else "differ from accepted history"
        with pytest.raises(RuntimeError, match=error):
            async with current.session(
                state=state, environment=declaration(executor, write_policy="legacy")
            ):
                pytest.fail("Invalid manual restore connected an executor")
        assert executor.attempts == 0 and not executor.requests
        assert len(model.requests) == 1 and not effects
        # Rejection must leave the valid portable boundary usable with current
        # manual bindings, without replaying its original model request.
        async with agent.session(
            state=good, environment=declaration(executor, write_policy="legacy")
        ) as restored:
            request = await next_request(restored.interactions(), manual)
            assert executor.attempts > 0
            assert request.call_id == "call-A" and len(model.requests) == 1
            model.respond(assistant_output("restored"))
            await request.respond("answer-A")
            assert (await restored.restored_turn.get_result()).final_output == "restored"
        assert len(model.requests) == 2 and not effects
