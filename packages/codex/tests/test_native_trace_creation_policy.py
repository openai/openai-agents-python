"""Optional processor policy suppresses logical responses before provider I/O."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from _agents_codex.telemetry import _native_provider_response_span_id
from agents import Agent, NativeProvider, function_tool, telemetry
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


@pytest.mark.parametrize(
    "case", ["suppress", "allow", "legacy", "recursive", "replace", "disabled", "invalid", "raises"]
)
def test_native_response_creation_policy(case):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; "
            "from tests.test_native_trace_creation_policy import native_case; "
            "asyncio.run(native_case(sys.argv[1]))",
            case,
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=40,
    )
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case(case):
    fixture = ResponsesFixture()
    fixture.respond(function_call("echo", {}, "policy-call"))
    fixture.respond(assistant_output("Complete."))
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    calls, effects, tool_ids = [], [], []
    trace_id, root_id = "21" * 16, "a" * 16
    loop = asyncio.get_running_loop()

    class PolicyRecorder(Recorder):
        def should_create_span(self, name, kind):
            assert asyncio.get_running_loop() is loop
            parent = telemetry.current_context()
            assert parent is not None and parent.trace_id == trace_id
            calls.append((name, kind, parent.span_id))
            if case == "recursive":
                with telemetry.span("creation-policy.recursive") as recursive:
                    assert recursive.context.disabled
                assert _native_provider_response_span_id(parent._handle) is None
            if case == "invalid":
                return None
            if case == "raises":
                raise ValueError("Synthetic creation policy failure")
            return case == "allow"

    recorder = Recorder() if case == "legacy" else PolicyRecorder()
    await telemetry.configure(processors=[recorder])

    @function_tool
    def echo() -> str:
        context = telemetry.current_context()
        assert context is not None
        effects.append("echo")
        tool_ids.append(context.span_id)
        return "done"

    try:
        async with TestServer(app) as server:
            async with NativeProvider(
                base_url=str(server.make_url("/v1")),
                api_key="synthetic-creation-policy",
                supports_websockets=False,
            ) as provider:
                with telemetry.trace(
                    "creation-policy.application",
                    options=telemetry.TraceOptions(
                        trace_id=trace_id, span_id=root_id, disabled=case == "disabled"
                    ),
                ):
                    if case == "replace":
                        recorder = PolicyRecorder()
                        telemetry.set_trace_processors([recorder])
                    agent = Agent(
                        model="gpt-5.5", provider=provider, instructions="Answer.", tools=[echo]
                    )
                    if case in {"invalid", "raises"}:
                        expected = TypeError if case == "invalid" else ValueError
                        with pytest.raises(expected, match="creation polic"):
                            await agent.run("Continue.")
                    else:
                        assert (await agent.run("Continue.")).final_output == "Complete."
        assert (await telemetry.force_flush()).status == "success"
        events = [row for row in recorder.events if row["trace_id"] == trace_id]
        responses = [row for row in events if row["name"] == "response"]
        print(
            json.dumps(
                {
                    "case": case,
                    "requests": fixture.requests,
                    "policy_calls": calls,
                    "response_ids": sorted({row["span_id"] for row in responses}),
                    "effects": effects,
                    "span_names": sorted({row["name"] for row in events}),
                }
            )
        )
        if case in {"invalid", "raises"}:
            assert len(calls) == 1 and not fixture.requests and not effects and not responses
            return
        assert len(fixture.requests) == 2 and effects == ["echo"]
        if case == "disabled":
            assert not calls and not events
            return
        if case == "legacy":
            assert not calls
        else:
            assert [(name, kind) for name, kind, _ in calls] == [("response", "model")] * 2
        expected = 2 if case in {"allow", "legacy"} else 0
        response_ids = {row["span_id"] for row in responses}
        assert len(response_ids) == expected
        assert "receiving" in {row["name"] for row in events}
        assert "creation-policy.recursive" not in {row["name"] for row in events}
        parents = {row["span_id"]: row["parent_span_id"] for row in events}
        for tool in tool_ids:
            visited = set()
            while tool != root_id:
                assert tool not in visited and tool not in response_ids
                visited.add(tool)
                tool = parents[tool]
    finally:
        await telemetry.shutdown()
