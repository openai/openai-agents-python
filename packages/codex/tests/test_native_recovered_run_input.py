from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from agents import (
    Agent,
    FunctionTool,
    InteractionRequired,
    NativeProvider,
    OutputGuardrail,
    RunDiagnosticError,
    SessionState,
)
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs


@pytest.mark.parametrize(
    "kind,capture,legacy,expose",
    [
        ("string", True, False, True),
        ("message", True, False, True),
        ("string", False, False, True),
        ("string", True, True, True),
        ("string", True, False, False),
    ],
)
async def test_original_input_after_manual_recovery(responses, kind, capture, legacy, expose):
    original = (
        "original caller request"
        if kind == "string"
        else {
            "role": "user",
            "type": "message",
            "content": [{"type": "input_text", "text": "original caller request"}],
        }
    )
    expected = deepcopy(original)
    lookup = FunctionTool(
        name="lookup", description="Ask lookup.", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "recover-input"))

    def fail(*args):
        raise ValueError("synthetic recovered failure")

    def answer(event):
        request = lookup.match(event)
        assert request is not None
        return request.reply("accepted lookup")

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-recovered-input"
    ) as provider:
        agent = Agent(
            provider=provider,
            instructions="Ask lookup, then answer.",
            tools=[lookup],
            output_guardrails=[OutputGuardrail("fail-after-recovery", fail)],
        )
        pending = asyncio.create_task(agent.run(original, capture_error_details=capture))
        await asyncio.sleep(0)
        if isinstance(original, dict):
            original["content"][0]["text"] = "caller mutation after admission"
        with pytest.raises(RunDiagnosticError if capture else InteractionRequired) as caught:
            await pending
        interrupted = (
            caught.value.__cause__ if isinstance(caught.value, RunDiagnosticError) else caught.value
        )
        assert isinstance(interrupted, InteractionRequired)
        assert interrupted.state is not None
        payload = json.loads(interrupted.state.to_json())
        if legacy:
            payload.pop("original_input", None)
        else:
            assert ("original_input" in payload) is capture
        state = SessionState.from_json(json.dumps(payload))
        responses.respond(assistant_output("resumed answer"))
        with pytest.raises(RunDiagnosticError if expose else ValueError) as failed:
            await agent.run(None, state=state, capture_error_details=expose, on_interaction=answer)
        if expose:
            assert isinstance(failed.value, RunDiagnosticError)
            assert failed.value.details.input == (expected if capture and not legacy else None)
            assert failed.value.details.incomplete is (not capture or legacy)
        else:
            assert not isinstance(failed.value, RunDiagnosticError)
            assert str(failed.value) == "synthetic recovered failure"
    assert len(responses.requests) == 2
