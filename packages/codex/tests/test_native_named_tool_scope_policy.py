"""Observe native creation-policy coverage for agent, handoff and tool spans."""

import subprocess
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, Handoff, NativeProvider, ToolContext, function_tool, telemetry
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


@pytest.mark.parametrize("rejected", ["None", "route", "lookup", "raise-route", "observer-only"])
def test_native_named_tool_scope_policy(rejected):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; "
            "from tests.test_native_named_tool_scope_policy import native_case; "
            "asyncio.run(native_case(sys.argv[1]))",
            rejected,
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case(rejected):
    rejected = None if rejected == "None" else rejected
    fixture = ResponsesFixture()
    fixture.respond(function_call("route", {}, "route-a"))
    fixture.respond(function_call("lookup", {}, "lookup-b"))
    fixture.respond(assistant_output("Complete."))
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    checks = []
    invoked = []

    class Policy(Recorder):
        def should_create_span(self, name, kind):
            checks.append((name, kind))
            if rejected == "raise-route" and (name, kind) == ("route", "handoff"):
                raise ValueError("Synthetic handoff creation policy failure")
            return name != rejected or kind not in ("tool", "handoff")

    @function_tool
    def route() -> str:
        span = telemetry.current_context()
        assert span is not None
        invoked.append(("route", span.span_id))
        return "routed"

    @function_tool
    def lookup(context: ToolContext) -> str:
        span = telemetry.current_context()
        assert span is not None and context.call.call_id == "lookup-b"
        invoked.append(("lookup", span.span_id))
        return "record"

    recorder = Recorder() if rejected == "observer-only" else Policy()
    await telemetry.configure(processors=[recorder])
    try:
        async with TestServer(app) as server:
            async with NativeProvider(
                base_url=str(server.make_url("/v1")),
                api_key="synthetic-trace-boundary",
                supports_websockets=False,
            ) as provider:
                target = Agent(name="Target", instructions="Read.", tools=[lookup])
                source = Agent(
                    name="Source",
                    model="gpt-5.5",
                    provider=provider,
                    instructions="Route.",
                    handoffs=[Handoff(target, route)],
                )
                with telemetry.trace("application") as root:
                    async with source.session() as session:
                        turn = await session.submit("Read a record.")
                        if rejected == "raise-route":
                            with pytest.raises(ValueError, match="handoff creation policy"):
                                await turn.get_result()
                            assert await session.get_active_agent() is source
                        else:
                            assert (await turn.get_result()).final_output == "Complete."
                            assert await session.get_active_agent() is target
                    trace_id = root.context.trace_id
        if rejected == "raise-route":
            assert checks.count(("route", "handoff")) == 1
            assert len(fixture.requests) == 1 and invoked == []
            return
        assert len(fixture.requests) == 3
        assert [name for name, _ in invoked] == ["route", "lookup"]
        assert (await telemetry.force_flush()).status == "success"
        events = [
            (row["name"], row["kind"], row["span_id"], row["parent_span_id"])
            for row in recorder.events
            if row["trace_id"] == trace_id and row["lifecycle"] == "start"
        ]
        if rejected == "observer-only":
            assert checks == []
            ended = [
                event
                for event in recorder.events
                if event["trace_id"] == trace_id and event["lifecycle"] == "end"
            ]
            for name in ("route", "lookup"):
                [dispatch] = [
                    event
                    for event in ended
                    if event["name"] == name and "aborted" in event["attributes"]
                ]
                assert "sdk.span.kind" not in dispatch["attributes"]
                assert dispatch["attributes"]["aborted"] is False
                [semantic] = [
                    event
                    for event in ended
                    if event["name"] == name and "sdk.span.kind" in event["attributes"]
                ]
                assert semantic["attributes"]["sdk.span.kind"] == (
                    "handoff" if name == "route" else "tool"
                )
                assert semantic["span_id"] != dispatch["span_id"]
        else:
            assert checks.count(("response", "model")) == 3
            assert checks.count(("route", "handoff")) == 1
            assert checks.count(("lookup", "tool")) == 1
        named = {name: identity for name, _, identity, _ in events if name in ("route", "lookup")}
        assert set(named) == {"route", "lookup"} - {rejected}
        for _, identity in invoked:
            assert any(identity == row[2] for row in events)
    finally:
        await telemetry.shutdown()
