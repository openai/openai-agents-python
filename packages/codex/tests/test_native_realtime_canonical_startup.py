"""The actual V2 startup frame and readiness share one settings contract."""

from agents import Agent, NativeProvider
from agents.realtime import RealtimeConfig
from agents.testing import RealtimeStep, ScriptedRealtimeAdapter
from tests._asyncio import timeout


async def test_canonical_startup_without_functions_or_optional_settings():
    adapter = ScriptedRealtimeAdapter(
        [
            RealtimeStep(
                "session.update",
                lambda frame: [
                    {"type": "session.updated", "session": {**frame["session"], "id": "fixture"}}
                ],
            ),
        ]
    )
    async with (
        timeout(20),
        NativeProvider(
            base_url="http://127.0.0.1:1/v1", api_key="synthetic-key", supports_websockets=False
        ) as provider,
        Agent(instructions="Speak.", provider=provider).session() as session,
    ):
        async with session.realtime(config=RealtimeConfig(model="fixture", adapter=adapter)):
            pass
    adapter.assert_complete()
    assert adapter.sent_events[0]["session"]["model"] == "fixture"
