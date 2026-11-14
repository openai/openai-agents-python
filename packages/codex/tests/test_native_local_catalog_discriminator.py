import pytest
from agents import Agent, LocalStorage, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import peer


@pytest.mark.parametrize(
    "source_tool,target_tool",
    [(False, False), (True, True), (True, False), (False, True)],
)
async def test_local_resume_catalog_follows_the_active_destination(
    tmp_path, source_tool, target_tool
):
    effects = []

    @function_tool
    def unused() -> str:
        effects.append("unexpected")
        return "unused"

    async with timeout(20), peer() as wire:
        wire.respond(function_call("transfer_to_b", {}, "handoff"))
        wire.respond(assistant_output("done"))
        if target_tool:
            wire.respond(function_call("unused", {}, "resumed-call"))
        wire.respond(assistant_output("continued"))
        async with NativeProvider(
            base_url=wire.url, api_key="synthetic", supports_websockets=False
        ) as provider:
            target = Agent(
                name="B",
                instructions="destination",
                tools=[unused] if target_tool else [],
            )
            source = Agent(
                name="A",
                instructions="source",
                model="gpt-5.5",
                provider=provider,
                tools=[unused] if source_tool else [],
                handoffs=[target],
                prepare_handoff_input=lambda value: value,
            )
            storage = LocalStorage(tmp_path / "store")
            async with source.session(storage=storage) as session:
                assert (
                    await (await session.submit("transfer")).get_result()
                ).final_output == "done"
                saved = await session.save_state()
            async with source.session(
                storage=storage, resume=saved.source_session_id
            ) as restored:
                assert await restored.get_active_agent() is target
                assert (
                    await (await restored.submit("continue")).get_result()
                ).final_output == "continued"
            names = [tool.get("name") for tool in wire.requests[2].get("tools", [])]
            assert ("unused" in names) is target_tool
            assert "transfer_to_b" not in names
            assert effects == (["unexpected"] if target_tool else [])
            assert len(wire.requests) == (4 if target_tool else 3)


async def test_local_resume_refuses_removing_a_pinned_destination_tool(tmp_path):
    effects = []

    @function_tool
    def destination_only() -> str:
        effects.append("unexpected")
        return "unused"

    async with timeout(20), peer() as wire:
        wire.respond(function_call("transfer_to_b", {}, "handoff"))
        wire.respond(assistant_output("done"))
        async with NativeProvider(
            base_url=wire.url, api_key="synthetic", supports_websockets=False
        ) as provider:
            target = Agent(
                name="B", instructions="destination", tools=[destination_only]
            )
            source = Agent(
                name="A",
                instructions="source",
                model="gpt-5.5",
                provider=provider,
                handoffs=[target],
                prepare_handoff_input=lambda value: value,
            )
            storage = LocalStorage(tmp_path / "store")
            async with source.session(storage=storage) as session:
                assert (
                    await (await session.submit("transfer")).get_result()
                ).final_output == "done"
                saved = await session.save_state()
            removed = Agent(name="B", instructions="destination")
            changed = Agent(
                name="A",
                instructions="source",
                model="gpt-5.5",
                provider=provider,
                handoffs=[removed],
                prepare_handoff_input=lambda value: value,
            )
            with pytest.raises(
                RuntimeError, match="exact captured Agent declaration graph"
            ):
                async with changed.session(
                    storage=storage, resume=saved.source_session_id
                ):
                    pytest.fail("A missing admitted destination tool reached opening")
            assert effects == []
            assert len(wire.requests) == 2
