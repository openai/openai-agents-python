"""Admission and snapshots at the original-input recovery boundary."""

import asyncio
from types import SimpleNamespace

from agents import ChildInvocation, ToolCallInfo, ToolRef, function_tool
from agents.session import Session


def captured(callback):
    @function_tool(input_error_function=callback)
    async def lookup(key: str) -> str:
        return key

    return lookup._capture(None)


def owner():
    state = SimpleNamespace(
        handle=ChildInvocation(session_id="s", child_id="c", instance_id="1", turn_id="t")
    )
    call = ToolCallInfo(
        tool=ToolRef("lookup"),
        session_id="s",
        turn_id="t",
        call_id="call",
        child_id="c",
        child_incarnation="1",
        role=None,
        model=None,
        route="direct",
        cell_id=None,
        nested_call_id=None,
    )
    return SimpleNamespace(realtime_usage=None, realtime_usage_accounting=None), state, call


async def test_recovery_rechecks_revoked_admission_before_callback():
    seen = []
    selected = captured(lambda *args: seen.append(True))
    session, state, call = owner()
    assert (
        await Session._recover_tool_input(
            session,
            state,
            selected,
            {"callId": "call"},
            ValueError(),
            {"key": 7},
            lambda: False,
            call,
        )
        is None
    )
    assert not seen


async def test_recovery_discards_late_formatter_after_revocation():
    started = asyncio.Event()
    release = asyncio.Event()
    live = True

    async def recover(*args):
        started.set()
        await release.wait()
        return "late"

    selected = captured(recover)
    session, state, call = owner()
    task = asyncio.create_task(
        Session._recover_tool_input(
            session,
            state,
            selected,
            {"callId": "call"},
            ValueError(),
            {"key": 7},
            lambda: live,
            call,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    live = False
    release.set()
    assert await task is None


async def test_recovery_handler_receives_a_detached_original_snapshot():
    original = {"nested": {"key": 7}}

    def recover(context, error, raw):
        assert raw == original
        raw["nested"]["key"] = 9
        return "retry"

    selected = captured(recover)
    session, state, call = owner()
    assert (
        await Session._recover_tool_input(
            session, state, selected, {"callId": "call"}, ValueError(), original, lambda: True, call
        )
        == "retry"
    )
    assert original == {"nested": {"key": 7}}
