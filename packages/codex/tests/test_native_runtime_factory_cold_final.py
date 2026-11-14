import pytest

from agents import Agent, DecisionHandoff, NativeProvider, function_tool
from agents.session import Session
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import peer
from tests.test_native_decision_handoff import Decisions


async def test_runtime_callback_permission_stays_with_current_thread_opening(monkeypatch):
    effects = []
    targets = []

    @function_tool
    def fresh_lookup() -> str:
        effects.append("once")
        return "lookup"

    def factory(_context, _input):
        target = Agent(
            name="Runtime target",
            instructions="Use the new callback.",
            model="gpt-5.5",
            tools=[fresh_lookup],
        )
        targets.append(target)
        return target

    async with (
        timeout(30),
        peer() as model,
        NativeProvider(
            base_url=model.url, api_key="synthetic", supports_websockets=False
        ) as backend,
    ):
        root = Agent(
            name="Source",
            instructions="Delegate.",
            model="gpt-5.5",
            provider=backend,
            decisions=Decisions("fresh"),
            handoffs=[
                DecisionHandoff(
                    name="delegate",
                    description="Delegate.",
                    question="Choose.",
                    targets={"fresh": factory},
                )
            ],
        )
        model.respond(function_call("delegate", {"input": "fresh"}, "initial-route"))
        model.respond(function_call("fresh_lookup", {}, "initial-effect"))
        model.respond(assistant_output("initial done"))
        async with root.session() as session:
            before_publication = await session.save_state()
            assert (await (await session.submit("Go")).get_result()).final_output == "initial done"
        assert effects == ["once"] and len(targets) == 1

        # A separate native thread has no runtime callback attachment.
        model.respond(function_call("fresh_lookup", {}, "foreign-effect"))
        model.respond(assistant_output("foreign done"))
        foreign = Agent(instructions="Answer.", model="gpt-5.5", provider=backend)
        assert (await foreign.run("Try the old name")).final_output == "foreign done"
        assert effects == ["once"] and len(model.requests) == 5

        session_globals = Session._open.__func__.__globals__
        original = session_globals["_install_profile_options"]

        def remove_current_permission(options, *args, **kwargs):
            original(options, *args, **kwargs)
            options["tool_policy"]["allowed_tools"] = [
                name
                for name in options["tool_policy"]["allowed_tools"]
                if name != {"namespace": "_agents_sdk", "name": "runtime_handoff_callbacks"}
            ]

        monkeypatch.setitem(session_globals, "_install_profile_options", remove_current_permission)
        model.respond(function_call("delegate", {"input": "fresh"}, "cold-route"))
        async with root.session(state=before_publication) as restored:
            assert await restored.get_active_agent() is root
            turn = await restored.submit("Reenter")
            with pytest.raises(RuntimeError, match="excluded by ToolPolicy"):
                await turn.get_result()
            assert await restored.get_active_agent() is root
        assert effects == ["once"] and len(targets) == 2
        assert len(model.requests) == 6
