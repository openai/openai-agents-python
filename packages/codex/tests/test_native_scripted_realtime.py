"""Scripted canonical frames exercise the real native realtime owner."""

from agents import Agent, NativeProvider, function_tool
from agents.realtime import RealtimeConfig, RealtimeText
from agents.testing import RealtimeStep, ScriptedRealtimeAdapter
from tests._asyncio import timeout
from tests.test_native_realtime_adapter import translated_reply


@function_tool
def lookup() -> str:
    """Synthetic lookup."""
    return "found"


async def test_scripted_native_session_ack_history_and_completion():
    adapter = ScriptedRealtimeAdapter(
        [
            RealtimeStep(
                "session.update",
                lambda frame: [
                    {"type": "session.updated", "session": {**frame["session"], "id": "fixture"}}
                ],
            ),
            RealtimeStep("conversation.item.create"),
            RealtimeStep("response.create", translated_reply("Scripted native answer")),
        ]
    )
    async with (
        timeout(20),
        NativeProvider(
            base_url="http://127.0.0.1:1/v1", api_key="synthetic-key", supports_websockets=False
        ) as provider,
        Agent(instructions="Speak.", tools=[lookup], provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture", adapter=adapter)
        ) as voice:
            await voice.send_text("Synthetic question")
            async for event in voice.events():
                if isinstance(event, RealtimeText) and event.final and event.role == "assistant":
                    assert event.text == "Scripted native answer"
                    break
            assert "Scripted native answer" in repr(voice.history)
    adapter.assert_complete()
    assert adapter.connect_calls[0].model == "fixture"
    assert "synthetic-key" not in repr(adapter.connect_calls)
    snapshot = adapter.sent_events
    snapshot[0]["type"] = "changed"
    assert adapter.sent_events[0]["type"] == "session.update"


async def test_scripted_matching_snapshots_and_errors():
    import pytest

    from agents.realtime import RealtimeAdapterContext

    adapter = ScriptedRealtimeAdapter(
        [
            RealtimeStep({"type": "first", "nested": {"value": 1}}),
            RealtimeStep(lambda f: f["type"] == "last"),
        ]
    )
    await adapter.connect(RealtimeAdapterContext("wss://synthetic.invalid", {}))
    await adapter.send('{"nested":{"value":1},"type":"first"}')
    with pytest.raises(AssertionError, match="unconsumed"):
        adapter.assert_complete()
    await adapter.send('{"type":"last"}')
    await adapter.aclose()
    adapter.assert_complete()

    mismatch = ScriptedRealtimeAdapter([RealtimeStep("expected")])
    await mismatch.connect(RealtimeAdapterContext("wss://synthetic.invalid", {}))
    with pytest.raises(AssertionError, match="Unexpected"):
        await mismatch.send('{"type":"unexpected","private":"synthetic"}')
    with pytest.raises(AssertionError, match="Unexpected"):
        mismatch.assert_complete()
    await mismatch.aclose()


async def test_scripted_connection_error_survives_native_cleanup():
    import pytest

    failure = RuntimeError("Synthetic connection failure")
    adapter = ScriptedRealtimeAdapter(connect_error=failure)
    async with (
        timeout(20),
        NativeProvider(
            base_url="http://127.0.0.1:1/v1",
            api_key="synthetic-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Speak.", provider=provider).session() as session,
    ):
        with pytest.raises(RuntimeError, match="Synthetic connection failure"):
            async with session.realtime(config=RealtimeConfig(model="fixture", adapter=adapter)):
                raise AssertionError("Unexpected successful connection")
    assert len(adapter.connect_calls) == 1


async def test_scripted_predicate_cannot_change_emitted_acknowledgement():
    import json

    from agents.realtime import RealtimeAdapterContext

    def matches(frame):
        frame["session"]["instructions"] = "Matcher-modified instructions"
        return frame["type"] == "session.update"

    original = {"type": "session.update", "session": {"instructions": "Original instructions"}}
    adapter = ScriptedRealtimeAdapter(
        [
            RealtimeStep(
                matches,
                lambda frame: [{"type": "session.updated", "session": frame["session"]}],
            )
        ]
    )
    await adapter.connect(RealtimeAdapterContext("wss://synthetic.invalid", {}))
    try:
        await adapter.send(json.dumps(original))
        acknowledgement = json.loads(await anext(adapter.incoming))
        assert acknowledgement == {"type": "session.updated", "session": original["session"]}
        assert adapter.sent_events == (original,)
        adapter.assert_complete()
    finally:
        await adapter.aclose()
