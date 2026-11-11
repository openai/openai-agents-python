"""SDK correlation ordering, isolated from Core's source/ACK validation."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession, _Delegation
from _agents_codex.voice.workflow import NativeSessionVoiceWorkflow
from agents import Session
from tests._asyncio import timeout


class AcceptedWorkflow:
    def __init__(self) -> None:
        self.cancelled = False

    async def run(self, text: str):
        assert text == "prepared native input"
        yield "accepted final text"

    async def cancel(self) -> None:
        self.cancelled = True


def owner(target: dict[str, Any] | None) -> RealtimeSession:
    async def active_profile() -> str:
        return json.dumps(target)

    async def tools(selected, *, allow_missing):
        assert selected is None and allow_missing
        return None, None

    # These two calls are the SDK/native profile seam under test. Native source
    # authorization and provider ACK acceptance require the real-wire suite.
    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _native=SimpleNamespace(_active_agent_profile=active_profile),
            _realtime_function_tools=tools,
        ),
    )
    voice = RealtimeSession(session, config=RealtimeConfig())
    session._realtime = voice
    voice._live = True
    voice._ready.set_result(None)
    return voice


@pytest.mark.parametrize("target", [None, {"profile_id": "agent-0", "epoch": 0}])
async def test_delegation_binds_exact_receipt_before_completion_even_without_profile_change(
    target, monkeypatch
):
    voice = owner(target)
    workflow = AcceptedWorkflow()
    owned = _Delegation(cast(NativeSessionVoiceWorkflow, workflow))
    voice._delegations["delegation-1"] = owned
    calls: list[tuple[str, dict[str, Any]]] = []
    updating = asyncio.Event()
    acknowledge = asyncio.Event()

    async def call(kind: str, **payload):
        calls.append((kind, payload))
        if kind == "update":
            updating.set()
            await acknowledge.wait()

    monkeypatch.setattr(voice, "_call", call)
    async with timeout(5):
        task = asyncio.create_task(voice._delegate("delegation-1", "prepared native input", owned))
        started = asyncio.create_task(updating.wait())
        try:
            await asyncio.wait((task, started), return_when=asyncio.FIRST_COMPLETED)
            assert updating.is_set(), "Delegation completed without binding its native source"
            assert not task.done()
            assert calls == [
                (
                    "update",
                    {
                        "update": {},
                        "profile": {
                            "response_id": None,
                            "delegation_id": "delegation-1",
                            "target": target,
                            "function_tools": None,
                            "expected_selection_id": None,
                            "disabled_tool_names": None,
                        },
                    },
                )
            ]
        finally:
            acknowledge.set()
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
            await task
    assert calls[-1] == ("complete", {"handoff_id": "delegation-1", "text": "accepted final text"})
    assert workflow.cancelled
    assert "delegation-1" not in voice._delegations


async def test_ordinary_mono_sync_still_skips_profile_update_unless_requested(monkeypatch):
    voice = owner(None)
    calls: list[dict[str, Any]] = []

    async def call(kind: str, **payload):
        assert kind == "update"
        calls.append(payload)

    monkeypatch.setattr(voice, "_call", call)
    await voice._sync_active_profile()
    assert calls == []
    await voice._sync_active_profile(include_mono=True)
    assert len(calls) == 1
    assert calls[0]["profile"]["response_id"] is None
    assert calls[0]["profile"]["delegation_id"] is None
