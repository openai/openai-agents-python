"""Public native voice policy behavior against the independent provider peer."""

import asyncio

import pytest

from agents import Agent, Deny, NativeProvider, function_tool
from agents.observation import ToolFinished, ToolStarted
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.test_native_manual_tools import next_request
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


async def test_native_voice_policies_follow_active_profile(realtime_peer: RealtimePeer) -> None:
    peer = realtime_peer
    context = {"marker": "policy-owner"}
    seen = []

    @function_tool
    def hidden() -> str:
        return "forbidden"

    @function_tool
    def visible() -> str:
        return "accepted"

    async def instructions(run):
        assert run.context is context
        return f"Voice for {run.agent.name}"

    async def enabled(run, declaration):
        assert run.context is context
        seen.append((run.agent.name, declaration.type, declaration.name))
        return declaration.name == "visible"

    target = Agent(name="B", instructions="B", tools=[visible, hidden])
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-policy", supports_websockets=False
        ) as provider,
        Agent(
            name="A",
            instructions="A",
            tools=[visible, hidden],
            handoffs=[target],
            provider=provider,
        ).session(context=context) as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime", instructions=instructions, is_tool_enabled=enabled
            )
        ) as owner,
    ):
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        assert "Voice for A" in initial["session"]["instructions"]
        assert [
            item["name"]
            for item in initial["session"]["tools"]
            if item["name"] in {"visible", "hidden", "transfer_to_b"}
        ] == ["visible"]
        assert ("A", "handoff", "transfer_to_b") in seen
        start = len(peer.frames)
        await session.update(agent=target)
        updated = await peer.frame(lambda frame: frame["type"] == "session.update", after=start)
        assert "Voice for B" in updated["session"]["instructions"]
        assert [
            item["name"]
            for item in updated["session"]["tools"]
            if item["name"] in {"visible", "hidden", "transfer_to_b"}
        ] == ["visible"]
        await peer.function("visible", {}, "profile-call")
        output = await peer.frame(
            lambda frame: frame.get("item", {}).get("call_id") == "profile-call"
        )
        assert output["item"]["output"] == "accepted"
        assert ("B", "function", "visible") in seen
        assert peer.responses.requests == []
        await owner.update({"instructions": "Completed."})


@pytest.mark.parametrize("mixed", [False, True])
async def test_unknown_native_calls_do_not_poison_response_batches(
    realtime_peer: RealtimePeer, mixed: bool
) -> None:
    peer = realtime_peer
    effects = []

    @function_tool
    def known() -> str:
        effects.append("accepted")
        return "accepted"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-policy", supports_websockets=False
        ) as provider,
        Agent(instructions="Use known.", tools=[known], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        calls = [("unknown", {}, "rejected-call")]
        if mixed:
            calls.append(("known", {}, "mixed-call"))
        await peer.functions(calls, response_id="rejected-response")
        if mixed:
            output = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "mixed-call"
            )
            assert output["item"]["output"] == "accepted"
            await peer.frame(lambda frame: frame["type"] == "response.create")
        await owner.update({"instructions": "Still listening."})
        assert sum(frame["type"] == "response.create" for frame in peer.frames) == int(mixed)
        start = len(peer.frames)
        await peer.function("known", {}, "recovery-call")
        output = await peer.frame(
            lambda frame: frame.get("item", {}).get("call_id") == "recovery-call"
        )
        assert output["item"]["output"] == "accepted"
        await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
        await owner.update({"instructions": "Completed."})
        assert sum(frame["type"] == "response.create" for frame in peer.frames) == 1 + int(mixed)
        assert effects == ["accepted"] * (1 + int(mixed))
        rejected = [
            frame["item"]
            for frame in peer.frames
            if frame.get("item", {}).get("call_id") == "rejected-call"
        ]
        assert len(rejected) == 1
        assert rejected[0]["output"] == "Tool unknown not found"
        assert peer.responses.requests == []


@pytest.mark.parametrize("explicit", [False, True])
async def test_native_approval_formatter_preserves_explicit_reason(
    realtime_peer: RealtimePeer, explicit: bool
) -> None:
    peer = realtime_peer
    effects, formatted = [], []

    @function_tool(needs_approval=True)
    def risky() -> str:
        effects.append("forbidden")
        return "forbidden"

    async def formatter(error):
        formatted.append(error)
        return "Application declined"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-policy", supports_websockets=False
        ) as provider,
        Agent(instructions="Use risky.", tools=[risky], provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tool_error_formatter=formatter)
        ),
    ):
        await peer.function("risky", {}, "approval-call")
        request = await next_request(session.interactions(), risky)
        if explicit:
            await request.reject("Explicit denial")
        else:
            await request.reject()
        output = await peer.frame(
            lambda frame: frame.get("item", {}).get("call_id") == "approval-call"
        )
        assert output["item"]["output"] == (
            "Explicit denial" if explicit else "Application declined"
        )
        assert len(formatted) == (0 if explicit else 1)
        assert effects == []
        assert await session.pending_interactions() == ()
        assert peer.responses.requests == []


@pytest.mark.parametrize("outcome", ["invalid", "none", "exception"])
async def test_native_approval_formatter_validates_returns_and_preserves_pending_owner(
    realtime_peer: RealtimePeer, outcome: str
) -> None:
    peer = realtime_peer
    effects, formatted = [], []

    @function_tool(needs_approval=True)
    def risky() -> str:
        effects.append("forbidden")
        return "forbidden"

    async def formatter(error):
        formatted.append(error)
        if outcome == "exception":
            raise RuntimeError("Application formatter failure")
        return None if outcome == "none" else 42

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-policy", supports_websockets=False
        ) as provider,
        Agent(instructions="Use risky.", tools=[risky], provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tool_error_formatter=formatter)
        ),
    ):
        await peer.function("risky", {}, "formatter-return")
        request = await next_request(session.interactions(), risky)
        if outcome == "invalid":
            with pytest.raises(TypeError, match="formatter must return a string or None"):
                await request.reject()
            assert len(await session.pending_interactions()) == 1
            assert not any(
                frame.get("item", {}).get("call_id") == "formatter-return" for frame in peer.frames
            )
            await request.reject("Explicit recovery")
        else:
            await request.reject()
        output = await peer.frame(
            lambda frame: frame.get("item", {}).get("call_id") == "formatter-return"
        )
        assert output["item"]["output"] == (
            "Explicit recovery" if outcome == "invalid" else "Tool execution was rejected"
        )
        assert len(formatted) == 1
        assert effects == []
        assert await session.pending_interactions() == ()
        assert peer.responses.requests == []


async def test_native_voice_close_discards_late_availability(realtime_peer: RealtimePeer) -> None:
    peer = realtime_peer
    entered = asyncio.Event()
    effects = []
    checks = 0

    @function_tool
    def known() -> str:
        effects.append("forbidden")
        return "forbidden"

    async def enabled(run, declaration):
        nonlocal checks
        checks += 1
        if checks > 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The application suppresses cancellation and returns a stale allowance.
                return True
        return True

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-policy", supports_websockets=False
        ) as provider,
        Agent(instructions="Use known.", tools=[known], provider=provider).session() as session,
    ):
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", is_tool_enabled=enabled)
        ):
            await peer.function("known", {}, "retired-call")
            await entered.wait()
        assert effects == []
        assert not any(
            frame.get("item", {}).get("call_id") == "retired-call" for frame in peer.frames
        )
        assert peer.responses.requests == []


async def test_native_managed_availability_rejection_has_no_unfinished_tool_notice(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    allowed = True
    effects = []

    @function_tool(needs_approval=True)
    def risky() -> str:
        effects.append("forbidden")
        return "forbidden"

    def enabled(_context, _tool):
        return allowed

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-policy", supports_websockets=False
        ) as provider,
        Agent(instructions="Use risky.", tools=[risky], provider=provider).session() as session,
    ):
        reader = session.events(include_execution_details=True)
        async with session.realtime(
            config=RealtimeConfig(model="fixture-realtime", is_tool_enabled=enabled)
        ) as owner:
            await peer.function("risky", {}, "approval-disabled")
            request = await next_request(session.interactions(), risky)
            allowed = False
            await request.approve()
            output = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "approval-disabled"
            )
            assert output["item"]["output"] == "Tool risky not found"
            await owner.update({"instructions": "Rejection acknowledged."})
            assert effects == []
            assert await session.pending_interactions() == ()
    notices = [
        event
        async for event in reader
        if isinstance(event, (ToolStarted, ToolFinished)) and event.call_id == "approval-disabled"
    ]
    # Python checks execution availability before publishing ToolStarted.
    assert notices == []
    assert peer.responses.requests == []


@pytest.mark.parametrize("reason", ["implicit", "explicit_default", "input_policy"])
async def test_native_remembered_rejection_preserves_formatter_omission(
    realtime_peer: RealtimePeer, reason: str
) -> None:
    peer = realtime_peer
    effects, formatted = [], []

    def before(_context):
        return Deny("Tool execution was rejected")

    @function_tool(
        needs_approval=True,
        before=before if reason == "input_policy" else None,
        run_before_approval=True,
    )
    def risky() -> str:
        effects.append("forbidden")
        return "forbidden"

    def formatter(error):
        formatted.append(error.call_id)
        return "Application declined " + error.call_id

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-policy", supports_websockets=False
        ) as provider,
        Agent(instructions="Use risky.", tools=[risky], provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tool_error_formatter=formatter)
        ) as owner,
    ):
        for index in range(2):
            call_id = f"remembered-{index}"
            await peer.function("risky", {}, call_id)
            if index == 0 and reason != "input_policy":
                request = await next_request(session.interactions(), risky)
                if reason == "implicit":
                    await request.reject(always=True)
                else:
                    await request.reject("Tool execution was rejected", always=True)
            output = await peer.frame(
                lambda frame, call_id=call_id: frame.get("item", {}).get("call_id") == call_id
            )
            assert output["item"]["output"] == (
                "Application declined " + call_id
                if reason == "implicit"
                else "Tool execution was rejected"
            )
        await owner.update({"instructions": "Completed remembered calls."})
        assert formatted == (["remembered-0", "remembered-1"] if reason == "implicit" else [])
        assert effects == []
        assert await session.pending_interactions() == ()
        assert peer.responses.requests == []
