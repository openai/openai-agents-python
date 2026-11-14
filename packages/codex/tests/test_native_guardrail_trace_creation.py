"""The v1 tracing policy can control individual native Agent guardrail spans."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    GuardrailResult,
    GuardrailTripwire,
    InputGuardrail,
    NativeProvider,
    OutputGuardrail,
    TurnStoppedError,
    telemetry,
)
from tests.native_fixtures import ResponsesFixture, assistant_output
from tests.test_native_trace_exporter import collector, native_trace_child_env
from tests.test_tracing_native import Recorder


@pytest.mark.parametrize("mode", ["output_trip", "input_trip", "invalid", "raised", "cancel"])
def test_native_guardrail_trace_creation(mode):
    with tempfile.TemporaryDirectory() as home:
        env = native_trace_child_env()
        env["CODEX_HOME"] = home
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import asyncio,sys; "
                "from tests.test_native_guardrail_trace_creation import native_case; "
                "asyncio.run(native_case(sys.argv[1]))",
                mode,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=40,
        )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case(mode):

    fixture = ResponsesFixture()
    fixture.respond(assistant_output("Complete."))
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    policies, effects = [], []
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class PolicyRecorder(Recorder):
        def should_create_span(self, name, kind):
            if kind == "guardrail":
                policies.append(name)
                assert telemetry.current_context() is not None
                return name != "suppress this input"
            return True

    recorder = PolicyRecorder()

    async def input_check(*_args):
        await asyncio.sleep(0)
        effects.append("input")
        return GuardrailResult(mode == "input_trip", {"private": "guardrail input diagnostic"})

    async def output_check(*_args):
        await asyncio.sleep(0)
        effects.append("output")
        if mode == "cancel":
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        if mode == "invalid":
            return None
        if mode == "raised":
            raise RuntimeError("synthetic guardrail failure")
        return GuardrailResult(True, {"private": "guardrail output diagnostic"})

    input_name = "suppress this input" if mode == "output_trip" else "check this input"
    output_name = "check this output"
    expected_error = {"invalid": TypeError, "raised": RuntimeError}.get(mode, GuardrailTripwire)
    with collector() as (endpoint, received):
        exporter = telemetry.OpenAITraceExporter(api_key="synthetic-key", endpoint=endpoint)
        await telemetry.configure(processors=[recorder, exporter])
        try:
            async with TestServer(app) as server:
                async with NativeProvider(
                    base_url=str(server.make_url("/v1")),
                    api_key="synthetic-guardrail-trace",
                    supports_websockets=False,
                ) as provider:
                    with telemetry.trace("guardrail.workflow") as workflow:
                        agent = Agent(
                            model="gpt-5.5",
                            provider=provider,
                            instructions="Answer.",
                            input_guardrails=[
                                InputGuardrail(input_name, input_check, run_in_parallel=False)
                            ],
                            output_guardrails=[OutputGuardrail(output_name, output_check)],
                        )
                        if mode == "cancel":
                            async with agent.session() as session:
                                turn = await session.submit("Continue.")
                                await asyncio.wait_for(entered.wait(), 10)
                                await turn.cancel()
                                assert cancelled.is_set()
                                with pytest.raises((asyncio.CancelledError, TurnStoppedError)):
                                    await turn.get_result()
                        else:
                            with pytest.raises(expected_error):
                                await agent.run("Continue.")
            assert (await telemetry.force_flush()).status == "success"
            if mode == "input_trip":
                assert effects == ["input"] and not fixture.requests
                assert policies == [input_name]
            else:
                assert effects == ["input", "output"] and len(fixture.requests) == 1
                assert policies == [input_name, output_name]
            events = [
                event
                for event in recorder.events
                if event["trace_id"] == workflow.context.trace_id
                and event["attributes"].get("sdk.span.kind") == "guardrail"
            ]
            expected_names = {
                "output_trip": [output_name],
                "input_trip": [input_name],
            }.get(mode, [input_name, output_name])
            assert [(event["name"], event["lifecycle"]) for event in events] == [
                (name, lifecycle) for name in expected_names for lifecycle in ("start", "end")
            ]
            ends = [event for event in events if event["lifecycle"] == "end"]
            for event in ends:
                assert event["parent_span_id"]
                assert "diagnostic" not in repr(event)
            last = ends[-1]
            if mode in {"output_trip", "input_trip"}:
                assert last["attributes"]["sdk.outcome.category"] == "guardrail_triggered"
                assert last["outcome"] == "ok"
            else:
                assert last["outcome"] == "error"
                if mode == "cancel":
                    assert last["attributes"]["sdk.outcome.category"] == "cancelled"
            exported = [
                item["span_data"]
                for request in received
                for item in request["payload"]["data"]
                if item.get("span_data", {}).get("type") == "guardrail"
            ]
            assert exported == [
                {
                    "type": "guardrail",
                    "name": name,
                    "triggered": mode in {"output_trip", "input_trip"},
                }
                for name in expected_names
            ]
        finally:
            await telemetry.shutdown()
