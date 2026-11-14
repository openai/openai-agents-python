from hashlib import sha256

from agents import Agent, LocalStorage, NativeProvider, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import peer


async def test_selected_empty_destination_catalog_survives_owned_json_restore(tmp_path):
    effects = []

    @function_tool
    def unused() -> str:
        effects.append("unexpected")
        return "unused"

    async with timeout(20), peer() as wire:
        wire.respond(function_call("transfer_to_b", {}, "handoff"))
        wire.respond(assistant_output("done"))
        wire.respond(assistant_output("continued"))
        async with NativeProvider(
            base_url=wire.url, api_key="synthetic", supports_websockets=False
        ) as provider:
            target = Agent(name="B", instructions="destination", tools=[unused])
            source = Agent(
                name="A",
                instructions="source",
                model="gpt-5.5",
                provider=provider,
                tools=[unused],
                handoffs=[target],
                prepare_handoff_input=lambda value: value,
            )
            storage = LocalStorage(tmp_path / "store")
            async with source.session(storage=storage) as session:
                assert (
                    await (await session.submit("transfer")).get_result()
                ).final_output == "done"
                saved = await session.save_state()
            async with source.session(storage=storage, resume=saved.source_session_id) as resumed:
                assert await resumed.get_active_agent() is target
                await resumed.update(tools=[])
                saved = await resumed.save_state()
            raw = saved.to_json()
            saved = SessionState.from_json(raw, expected_sha256=sha256(raw.encode()).hexdigest())
            async with source.session(state=saved) as restored:
                assert await restored.get_active_agent() is target
                assert (
                    await (await restored.submit("continue")).get_result()
                ).final_output == "continued"
            assert "unused" not in [tool.get("name") for tool in wire.requests[2].get("tools", [])]
            assert effects == []
            assert len(wire.requests) == 3
