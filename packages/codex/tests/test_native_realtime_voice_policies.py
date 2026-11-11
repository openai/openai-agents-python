"""Public native voice policy behavior against the independent provider peer."""

import asyncio
import json

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
        if isinstance(event, (ToolStarted, ToolFinished)) and event.call_id == request.call_id
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
        return "Application declined " + str(len(formatted))

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
                "Application declined " + str(index + 1)
                if reason == "implicit"
                else "Tool execution was rejected"
            )
        await owner.update({"instructions": "Completed remembered calls."})
        if reason == "implicit":
            assert len(formatted) == 2 and len(set(formatted)) == 2
            assert formatted[0] == request.call_id
        else:
            assert formatted == []
        assert effects == []
        assert await session.pending_interactions() == ()
        assert peer.responses.requests == []


@pytest.mark.parametrize("entry", ["sip", "profile"])
async def test_explicit_voice_capture_rejects_source_changed_between_policy_reads(
    realtime_peer: RealtimePeer, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    from contextlib import AsyncExitStack

    peer = realtime_peer
    effects = []

    @function_tool(name="probe", description="Identical wire definition")
    def original() -> str:
        effects.append("original")
        return "original"

    @function_tool(name="probe", description="Identical wire definition")
    def replacement() -> str:
        effects.append("replacement")
        return "replacement"

    config = RealtimeConfig(
        model="fixture-realtime", tools=(original,), instructions=lambda run: "Captured policy"
    )
    async with (
        timeout(25),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-selection-race",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="Root", instructions="Use probe", tools=[original], provider=provider
        ).session() as session,
        AsyncExitStack() as stack,
    ):
        owner = None
        if entry == "profile":
            owner = await stack.enter_async_context(session.realtime(config=config))
            await peer.frame(lambda frame: frame["type"] == "session.update")
        captured = asyncio.Event()
        captured_generation = None
        resume = asyncio.Event()
        wire = []
        select = type(session)._realtime_function_tools
        stale = None

        async def capture(self, tools, **kwargs):
            nonlocal captured_generation
            value = await select(self, tools, **kwargs)
            wire.append(value[0])
            if asyncio.current_task() is stale:
                captured_generation = kwargs["snapshot"]["catalog_generation"]
                captured.set()
                await resume.wait()
            return value

        monkeypatch.setattr(type(session), "_realtime_function_tools", capture)
        # _sync_active_profile is the native completed-delegation/profile-event
        # entry boundary; the competing replacement uses public Session.update.
        stale = asyncio.create_task(
            session.build_realtime_sip_config(config=config)
            if owner is None
            else owner._sync_active_profile(include_mono=True)
        )
        captured_waiter = asyncio.create_task(captured.wait())
        updating = None
        try:
            done, _ = await asyncio.wait(
                (captured_waiter, stale), return_when=asyncio.FIRST_COMPLETED
            )
            if stale in done:
                await stale
                pytest.fail("The stale selection finished before its capture barrier")
            assert captured_generation is not None
            updating = asyncio.create_task(session.update(tools=[replacement]))
            # The public update joins profile synchronization, whose lock the
            # stale capture still holds. Wait only for the native catalog change.
            while True:
                update_was_done = updating.done()
                try:
                    current_snapshot = json.loads(
                        await session._native.call("realtime_tool_selection_sdk", "{}")
                    )
                except RuntimeError as error:
                    if (
                        update_was_done
                        or str(error) != "Realtime source changed during selection snapshot"
                    ):
                        raise
                    await asyncio.sleep(0)
                    continue
                if current_snapshot["catalog_generation"] != captured_generation:
                    break
                if updating.done():
                    await updating
                    current_snapshot = json.loads(
                        await session._native.call("realtime_tool_selection_sdk", "{}")
                    )
                    if current_snapshot["catalog_generation"] == captured_generation:
                        pytest.fail("The tool update did not change its native catalog")
                    break
                await asyncio.sleep(0)
            resume.set()
            with pytest.raises(
                (RuntimeError, ValueError), match="another captured catalog/profile"
            ):
                await stale
            await updating
            current, _ = await select(session, (original,))
            assert wire[0] == current
        finally:
            resume.set()
            captured_waiter.cancel()
            pending = [captured_waiter, stale]
            if updating is not None:
                pending.append(updating)
            await asyncio.gather(*pending, return_exceptions=True)
        assert effects == []
        if owner is None:
            # A fresh capture of the same admitted selector resolves the accepted binding.
            assert await session.build_realtime_sip_config(config=config)
        else:
            assert sum(frame["type"] == "session.update" for frame in peer.frames) == 2
            await peer.function("probe", {}, "captured-replacement")
            output = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "captured-replacement"
            )
            assert output["item"]["output"] == "replacement"
            assert effects == ["replacement"]
