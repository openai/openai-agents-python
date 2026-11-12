"""Managed Realtime decisions use the caller's native tool owner and history."""

import pytest

from agents import (
    Agent,
    Allow,
    ApprovalRequested,
    InteractionExpiredError,
    NativeProvider,
    function_tool,
)
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.native_fixtures import text_response
from tests.test_native_manual_tools import next_request
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("approve", [True, False])
@pytest.mark.parametrize("run_before", [True, False])
async def test_realtime_managed_decision_cache_and_guard_scheduling(
    realtime_peer: RealtimePeer, approve: bool, run_before: bool
) -> None:
    peer = realtime_peer
    effects, guards, predicates = [], [], []

    def before(context):
        guards.append(context.effective_arguments["key"])
        return Allow()

    def predicate(context):
        predicates.append(context.effective_arguments["key"])
        return True

    @function_tool(before=before, needs_approval=predicate, run_before_approval=run_before)
    def risky(key: str) -> str:
        effects.append(key)
        return "accepted-" + key

    peer.responses.respond(*text_response("followup", "after voice"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-voice-approval",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use risky.", tools=[risky], provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(risky,))
        ):
            await peer.function("risky", {"key": "first"}, "voice-first")
            request = await next_request(session.interactions(), risky)
            assert isinstance(request, ApprovalRequested)
            assert request.session_id == session.id and request.turn_id is not None
            assert effects == []
            assert guards == (["first"] if run_before else [])
            if approve:
                await request.approve(always=True)
            else:
                await request.reject("explicit denial", always=True)
            for key, call_id in [("first", "voice-first"), ("second", "voice-second")]:
                if key == "second":
                    await peer.function("risky", {"key": key}, call_id)
                output = await peer.frame(
                    lambda frame, call_id=call_id: (
                        frame["type"] == "conversation.item.create"
                        and frame.get("item", {}).get("call_id") == call_id
                    )
                )
                assert output["item"]["output"] == (
                    "accepted-" + key if approve else "explicit denial"
                )
            assert effects == (["first", "second"] if approve else [])
            assert predicates == ["first"]
            assert guards == (
                [key for key in ["first", "second"] for _ in range(2 if approve else 1)]
                if run_before
                else (["first", "second"] if approve else [])
            )
            assert await session.pending_interactions() == ()
            assert peer.responses.requests == []
        assert (
            await (await session.submit("Continue.")).get_result()
        ).final_output == "after voice"
        history = peer.responses.requests[0]["input"]
        calls = [
            item
            for item in history
            if item.get("type") == "function_call" and item.get("name") == "risky"
        ]
        assert len(calls) == 2
        assert all(
            any(
                item.get("type") == "function_call_output"
                and item.get("call_id") == call["call_id"]
                for item in history
            )
            for call in calls
        )


async def test_realtime_close_expires_pending_managed_decision(realtime_peer: RealtimePeer) -> None:
    peer = realtime_peer
    effects = []

    @function_tool(needs_approval=True)
    def risky(key: str) -> str:
        effects.append(key)
        return key

    peer.responses.respond(*text_response("followup", "root remains usable"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-voice-close", supports_websockets=False
        ) as provider,
        Agent(instructions="Use risky.", tools=[risky], provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(risky,))
        ):
            await peer.function("risky", {"key": "blocked"}, "voice-blocked")
            request = await next_request(session.interactions(), risky)
        with pytest.raises(InteractionExpiredError):
            await request.approve()
        assert effects == [] and peer.responses.requests == []
        assert not any(
            frame.get("item", {}).get("call_id") == "voice-blocked" for frame in peer.frames
        )
        assert (
            await (await session.submit("Continue.")).get_result()
        ).final_output == "root remains usable"


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("malformed", [False, True])
async def test_realtime_original_input_recovery_continues_once(realtime_peer, managed, malformed):
    peer = realtime_peer
    effects, policies, predicates, formatted = [], [], [], []

    def before(context):
        policies.append("before")
        return Allow()

    def after(context, output):
        policies.append("after")
        return output

    def predicate(context):
        predicates.append(True)
        return True

    def recover(context, error, original):
        assert original == (' {"key": ' if malformed else {"key": 7})
        if malformed:
            assert isinstance(error, ValueError)
            assert "EOF" in str(error)
        formatted.append(True)
        return "Invalid risky input"

    @function_tool(
        input_error_function=recover,
        needs_approval=predicate if managed else False,
        before=before,
        after=after,
    )
    def risky(key: str) -> str:
        effects.append(key)
        return "unexpected"

    async with (
        timeout(15),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-input-recovery",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use risky.", tools=[risky], provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(risky,))
        ) as voice:
            start = len(peer.frames)
            await peer.function(
                "risky", ' {"key": ' if malformed else {"key": 7}, "invalid-original"
            )
            output = await peer.frame(
                lambda frame: (
                    frame["type"] == "conversation.item.create"
                    and frame.get("item", {}).get("call_id") == "invalid-original"
                ),
                after=start,
            )
            assert output["item"]["output"] == "Invalid risky input"
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            await voice.update({"instructions": "Use risky."})
            assert (
                len([frame for frame in peer.frames[start:] if frame["type"] == "response.create"])
                == 1
            )
            assert (
                len(
                    [
                        frame
                        for frame in peer.frames[start:]
                        if frame["type"] == "conversation.item.create"
                        and frame.get("item", {}).get("call_id") == "invalid-original"
                    ]
                )
                == 1
            )
            assert formatted == [True]
            assert not effects and not policies and not predicates
            assert await session.pending_interactions() == ()
            assert not peer.responses.requests
