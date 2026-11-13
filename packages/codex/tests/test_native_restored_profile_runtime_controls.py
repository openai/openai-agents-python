"""Restored profile settings remain current through independent idle updates."""

from dataclasses import replace

from agents import Agent, ModelRetrySettings, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_model_invocation_deadline import latest_settings


async def test_restored_handoff_rebinds_native_runtime_controls_before_update(responses):
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        target = Agent(
            name="B",
            instructions="Destination.",
            model="gpt-5.5",
            model_settings={
                "invocation_timeout_ms": 40_000,
                "attempt_timeout_ms": 20_000,
                "retry": ModelRetrySettings(max_retries=1),
            },
        )
        source = Agent(
            name="A",
            instructions="Source.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[target],
        )
        responses.respond(function_call("transfer_to_b", {}, "route_controls"))
        responses.respond(assistant_output("Destination ready."))
        async with source.session() as session:
            assert (await (await session.submit("Route.")).get_result()).final_output == (
                "Destination ready."
            )
            await session.update(
                model_settings={
                    "invocation_timeout_ms": 7_000,
                    "attempt_timeout_ms": 5_000,
                    "retry": ModelRetrySettings(max_retries=3),
                }
            )
            saved = await session.save_state()
        assert latest_settings(saved)["invocation_timeout_ms"] == 7_000

        async with replace(source, handoffs=[target]).session(state=saved) as restored:
            assert await restored.get_active_agent() is target
            await restored.update(instructions="Destination updated.")
            settings = latest_settings(await restored.save_state())
            assert settings["invocation_timeout_ms"] == 40_000
            assert settings["request_controls"]["attempt_timeout_ms"] == 20_000
            assert settings["request_controls"]["retry"]["max_retries"] == 1
