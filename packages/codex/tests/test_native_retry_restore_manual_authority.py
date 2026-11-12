"""Accepted manual model fields do not restore saved attempt authority."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    FunctionTool,
    ModelResponse,
    ModelRetrySettings,
    NativeProvider,
    SessionState,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call


class Lookup(BaseModel):
    key: str


async def next_request(session, tool):
    async for event in session.interactions():
        request = tool.match(event)
        if request is not None:
            return request
    raise AssertionError("Missing pending manual request")


@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("mode", ["retry", "timeout", "invocation", "invocation_clear"])
async def test_manual_restore_uses_current_attempt_authority(profile, mode):
    lookup = FunctionTool(name="lookup", description="Read", input_type=Lookup, output_type=str)
    calls = []
    retired = []
    initial_requests = 1

    class Backend:
        async def complete(self, request):
            calls.append(request)
            if len(calls) == initial_requests:
                return ModelResponse([function_call("lookup", {"key": "A"}, "manual")["item"]])
            if mode == "retry" and len(calls) == initial_requests + 1:
                raise ConnectionError("synthetic manual continuation failure")
            if mode in {"timeout", "invocation", "invocation_clear"}:
                try:
                    await asyncio.sleep(0.75 if mode == "invocation_clear" else 2.0)
                finally:
                    retired.append(True)
            return ModelResponse([assistant_output("unexpected replay")["item"]])

    invocation = mode in {"invocation", "invocation_clear"}
    deadline = "invocation_timeout_ms" if invocation else "attempt_timeout_ms"
    saved_timeout = 500 if mode == "invocation_clear" else 5000

    def declaration(provider, *, current):
        settings = {
            "retry": ModelRetrySettings(max_retries=0 if current else 3, policy=lambda _: True),
            deadline: (None if mode in {"retry", "invocation_clear"} else 1000)
            if current
            else saved_timeout,
        }
        worker = Agent(
            name="Worker",
            model="fixture",
            instructions="Read.",
            tools=[lookup],
            provider=None if profile else provider,
            model_settings=settings,
        )
        if not profile:
            return worker
        # A current active profile overrides its root's looser authority.
        return Agent(
            name="Parent",
            model="fixture",
            instructions="Transfer.",
            handoffs=[worker],
            provider=provider,
            model_settings={
                "retry": ModelRetrySettings(max_retries=7, policy=lambda _: True),
                deadline: 5000,
            },
        )

    async with timeout(20), NativeProvider(model_backend=Backend()) as provider:
        seed = declaration(provider, current=profile)
        async with seed.session() as source:
            if profile:
                await source.update(agent=seed.handoffs[0])
                assert (await source.get_active_agent()).name == "Worker"
            await source.submit("Read.")
            await next_request(source, lookup)
            saved = SessionState.from_json((await source.suspend()).to_json())
        if profile:
            document = json.loads(saved.to_json())
            changed = 0
            for row in document["history"]:
                payload = row.get("payload", {})
                if payload.get("type") == "thread_settings_applied":
                    settings = payload["thread_settings"]
                    controls = settings.setdefault("request_controls", {})
                    retry = controls.setdefault("retry", {})
                    retry["max_retries"] = 3
                    if invocation:
                        settings[deadline] = saved_timeout
                    else:
                        controls[deadline] = 5000
                    changed += 1
            assert changed
            saved = SessionState.from_json(json.dumps(document))
        current = seed if profile else declaration(provider, current=True)
        async with current.session(state=saved) as restored:
            request = await next_request(restored, lookup)
            expected = (
                "synthetic manual continuation failure"
                if mode == "retry"
                else "timed out|timeout|deadline"
            )
            if mode == "invocation_clear":
                await request.respond("answer")
                assert (
                    await restored.restored_turn.get_result()
                ).final_output == "unexpected replay"
            else:
                with pytest.raises(Exception, match=expected):
                    await request.respond("answer")
                    await restored.restored_turn.get_result()
        assert len(calls) == initial_requests + 1
        if mode in {"timeout", "invocation", "invocation_clear"}:
            assert retired == [True]
