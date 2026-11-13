"""Provider speech usage passes through Core's accepted response completions."""

from contextlib import aclosing
from typing import Any

import pytest

from agents import Agent, NativeProvider, ToolContext, function_tool
from agents.realtime import RealtimeConfig, RealtimeResponse, RealtimeUsage
from agents.testing import RealtimeStep, ScriptedRealtimeAdapter
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


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
        {"total_tokens": 7},
        None,
    ]
    totals = [None, 0, 15, 22, 22]
    snapshots = []
    accounting: list[tuple[int, int]] = []
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
                "status": "cancelled" if index == 3 else "completed",
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
        assert session.realtime_usage is None
        async with session.realtime(
            config=RealtimeConfig(model="fixture", adapter=adapter)
        ) as voice:
            assert voice.usage is None
            for index, usage in enumerate(values):
                await voice.send_text("Synthetic usage turn")
                async with aclosing(voice.events()) as events:
                    async for event in events:
                        if (
                            isinstance(event, RealtimeResponse)
                            and event.status == "done"
                            and event.response_id == f"r{index}"
                        ):
                            assert (None if event.usage is None else event.usage.total_tokens) == (
                                None if usage is None else usage["total_tokens"]
                            )
                            assert event.cumulative_usage == session.realtime_usage
                            snapshots.append(event.cumulative_usage)
                            assert event.usage_accounting is not None
                            assert event.usage_accounting == voice.usage_accounting
                            assert session.realtime_usage_accounting == voice.usage_accounting
                            accounting.append(
                                (
                                    event.usage_accounting.finished_responses,
                                    event.usage_accounting.usage_reports,
                                )
                            )
                            break
                assert (None if voice.usage is None else voice.usage.total_tokens) == (
                    totals[index]
                )
                assert session.realtime_usage == voice.usage
        assert session.realtime_usage is None
        assert [None if value is None else value.total_tokens for value in snapshots] == totals
        assert voice.usage is not None
        assert voice.usage.total_tokens == 22
        assert session.realtime_usage_accounting is None
        assert voice.usage_accounting.finished_responses == 5
        assert voice.usage_accounting.usage_reports == 3
        assert accounting == [(1, 0), (2, 1), (3, 2), (4, 3), (5, 3)]
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


@pytest.mark.parametrize("managed", [False, True])
async def test_tool_usage_snapshot_preserves_application_context_and_connection_lifetime(
    realtime_peer: RealtimePeer,
    managed: bool,
):
    application = {"usage": object()}
    contexts: list[ToolContext[dict[str, Any]]] = []

    policy_contexts: list[ToolContext[dict[str, Any]]] = []

    def policy(context: ToolContext[dict[str, Any]]) -> bool:
        assert context.context is application
        policy_contexts.append(context)
        return False

    @function_tool(needs_approval=policy if managed else False)
    async def inspect_usage(context: ToolContext[dict[str, Any]]) -> str:
        assert context.context is application
        contexts.append(context)
        return "observed"

    async with (
        timeout(20),
        NativeProvider(
            base_url=realtime_peer.responses.url,
            api_key="synthetic-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Inspect usage.", provider=provider, tools=[inspect_usage]).session(
            context=application
        ) as session,
    ):
        marker = application["usage"]
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(inspect_usage,))
        ) as voice:
            assert realtime_peer.socket is not None
            await realtime_peer.socket.send_json(
                {"type": "response.created", "response": {"id": "usage-before-tool"}}
            )
            await realtime_peer.socket.send_json(
                {
                    "type": "response.done",
                    "response": {
                        "id": "usage-before-tool",
                        "usage": {
                            "total_tokens": 7,
                            "input_token_details": {"audio_tokens": 7},
                        },
                    },
                }
            )
            async with aclosing(voice.events()) as events:
                async for event in events:
                    if isinstance(event, RealtimeResponse) and event.status == "done":
                        assert event.cumulative_usage is not None
                        assert event.cumulative_usage.total_tokens == 7
                        break
            await realtime_peer.function("inspect_usage", {}, "usage-tool")
            await realtime_peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "usage-tool"
            )
            assert len(contexts) == 1
            if managed:
                assert len(policy_contexts) == 1
                assert policy_contexts[0].realtime_usage == session.realtime_usage
                assert (
                    policy_contexts[0].realtime_usage_accounting
                    == session.realtime_usage_accounting
                )
            snapshot = contexts[0].realtime_usage
            assert snapshot is not None
            assert snapshot.total_tokens == 7
            assert contexts[0].realtime_usage_accounting == voice.usage_accounting
            assert contexts[0].realtime_usage_accounting is not None
            assert contexts[0].realtime_usage_accounting.usage_reports == 1
            assert isinstance(snapshot.input_token_details, dict)
            snapshot.input_token_details["audio_tokens"] = 999
            assert session.realtime_usage is not None
            assert session.realtime_usage.input_token_details == {"audio_tokens": 7}
            assert event.cumulative_usage is not None
            assert event.cumulative_usage.input_token_details == {"audio_tokens": 7}
            assert application["usage"] is marker
        assert session.realtime_usage is None
        assert voice.usage is not None and voice.usage.total_tokens == 7
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(inspect_usage,))
        ):
            assert session.realtime_usage is None
            await realtime_peer.function("inspect_usage", {}, "usage-tool-next")
            await realtime_peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "usage-tool-next"
            )
            assert len(contexts) == 2
            assert contexts[1].realtime_usage is None
            assert contexts[1].realtime_usage_accounting is not None
            assert contexts[1].realtime_usage_accounting.usage_reports == 0
            assert len(policy_contexts) == (2 if managed else 0)
            if managed:
                assert policy_contexts[1].realtime_usage is None
            assert snapshot.total_tokens == 7
            assert application["usage"] is marker


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
