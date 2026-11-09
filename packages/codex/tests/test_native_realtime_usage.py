"""Provider speech usage passes through Core's accepted response completions."""

from agents import Agent, NativeProvider
from agents.realtime import RealtimeConfig, RealtimeResponse, RealtimeUsage
from agents.testing import RealtimeStep, ScriptedRealtimeAdapter
from tests._asyncio import timeout


async def test_missing_zero_detailed_usage_and_duplicate_completion():
    values = [
        None,
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_token_details": {
                "text_tokens": 1,
                "audio_tokens": 9,
                "cached_tokens": 3,
                "cached_tokens_details": {"audio_tokens": 3},
            },
            "output_token_details": {"audio_tokens": 5},
        },
    ]
    steps = [
        RealtimeStep(
            "session.update",
            lambda frame: [
                {"type": "session.updated", "session": {**frame["session"], "id": "fixture"}}
            ],
        )
    ]
    for index, usage in enumerate(values):
        done = {
            "type": "response.done",
            "response": {
                "id": f"r{index}",
                **({} if usage is None else {"usage": {**usage, "vendor_counter": index}}),
            },
        }
        steps.extend(
            [
                RealtimeStep("conversation.item.create"),
                RealtimeStep(
                    "response.create",
                    [
                        {"type": "response.created", "response": {"id": f"r{index}"}},
                        done,
                        done,
                    ],
                ),
            ]
        )
    adapter = ScriptedRealtimeAdapter(steps)
    async with (
        timeout(20),
        NativeProvider(
            base_url="http://127.0.0.1:1/v1", api_key="synthetic-key", supports_websockets=False
        ) as provider,
        Agent(instructions="Speak.", provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture", adapter=adapter)
        ) as voice:
            assert voice.usage is None
            for index, usage in enumerate(values):
                await voice.send_text("Synthetic usage turn")
                async for event in voice.events():
                    if (
                        isinstance(event, RealtimeResponse)
                        and event.status == "done"
                        and event.response_id == f"r{index}"
                    ):
                        assert (None if event.usage is None else event.usage.total_tokens) == (
                            None if usage is None else usage["total_tokens"]
                        )
                        break
                assert (None if voice.usage is None else voice.usage.total_tokens) == (
                    None if usage is None else usage["total_tokens"]
                )
        assert voice.usage is not None
        assert voice.usage.total_tokens == 15
        assert voice.usage.input_token_details == values[2]["input_token_details"]
        detached = voice.usage.input_token_details
        assert isinstance(detached, dict)
        detached["audio_tokens"] = 999
        assert voice.usage.input_token_details["audio_tokens"] == 9
    adapter.assert_complete()


def test_usage_public_import():
    usage = RealtimeUsage(input_tokens=0)
    assert usage.input_tokens == 0
    assert usage.__class__.__module__ == "agents.realtime"


async def test_rejected_usage_public_error_has_no_provider_payload_after_cleanup():
    import pytest

    marker = "N05_SYNTHETIC_PRIVATE_USAGE"
    adapter = ScriptedRealtimeAdapter(
        [
            RealtimeStep(
                "session.update",
                lambda frame: [
                    {"type": "session.updated", "session": {**frame["session"], "id": "fixture"}}
                ],
            ),
            RealtimeStep("conversation.item.create"),
            RealtimeStep(
                "response.create",
                [
                    {"type": "response.created", "response": {"id": "invalid-usage"}},
                    {
                        "type": "response.done",
                        "response": {
                            "id": "invalid-usage",
                            "usage": {"input_tokens": -1, "vendor_private": marker},
                        },
                    },
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
        voice = session.realtime(config=RealtimeConfig(model="fixture", adapter=adapter))
        async with voice:
            await voice.send_text("Synthetic malformed usage turn")
            with pytest.raises(ValueError, match="nonnegative safe integers") as caught:
                async for _event in voice.events():
                    pass
            with pytest.raises(ValueError) as repeated:
                await voice.send_text("After failure")
            assert repeated.value is caught.value
            await voice.aclose()
            retained = caught.value
        pending: list[BaseException] = [retained]
        seen: set[int] = set()
        while pending:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            assert marker not in str(error)
            assert marker not in repr(vars(error))
            current = error.__traceback__
            while current is not None:
                frame = current.tb_frame
                if frame.f_globals.get("__name__", "").startswith("_agents_codex"):
                    assert marker not in repr(frame.f_locals)
                current = current.tb_next
            pending.extend(e for e in (error.__cause__, error.__context__) if e is not None)
        assert voice.usage is None
    adapter.assert_complete()
