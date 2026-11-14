"""Native Agent entries retain a lifetime span across successive model/tool work."""

import subprocess
import sys

import pytest

from agents import Agent, NativeProvider, function_tool, telemetry
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import MODEL, peer
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


@pytest.mark.parametrize("handoff", [False, True])
def test_one_agent_lifetime_span_owns_both_model_steps_and_function(handoff):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from tests.test_native_agent_lifetime import native_case; "
            "asyncio.run(native_case(sys.argv[1] == 'True'))",
            str(handoff),
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case(handoff):
    recorder = Recorder()
    await telemetry.configure(processors=[recorder])
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "accepted"

    try:
        async with timeout(20), peer() as model:
            model.respond(function_call("lookup", {}, "call"))
            if handoff:
                model.respond(function_call("transfer_to_b", {}, "handoff-call"))
            model.respond(assistant_output("done"))
            async with NativeProvider(
                base_url=model.url, api_key="synthetic", supports_websockets=False
            ) as provider:
                with telemetry.trace("agent-lifetime-witness") as workflow:
                    trace_id = workflow.context.trace_id
                    target = Agent(name="B", instructions="Finish", model=MODEL)
                    source = Agent(
                        name="A",
                        instructions="Use lookup",
                        model=MODEL,
                        provider=provider,
                        tools=[lookup],
                        handoffs=[target] if handoff else [],
                    )
                    result = await source.run("go")
                    assert result.final_output == "done"
            assert len(model.requests) == (3 if handoff else 2) and effects == ["lookup"]
        assert (await telemetry.force_flush()).status == "success"
        events = [row for row in recorder.events if row["trace_id"] == trace_id]
        agents = [row for row in events if row["attributes"].get("sdk.span.kind") == "agent"]
        assert len(agents) == (4 if handoff else 2)
        assert [row["lifecycle"] for row in agents] == (
            ["start", "end", "start", "end"] if handoff else ["start", "end"]
        )
        starts = {row["span_id"]: row for row in events if row["lifecycle"] == "start"}

        def agent_ancestor(row):
            while row["attributes"].get("sdk.span.kind") != "agent":
                row = starts[row["parent_span_id"]]
            return row["name"]

        models = [
            row for row in starts.values() if row["attributes"].get("sdk.span.kind") == "model"
        ]
        assert [agent_ancestor(row) for row in models] == (
            ["A", "A", "B"] if handoff else ["A", "A"]
        )
        lookup = next(row for row in starts.values() if row["name"] == "lookup")
        assert agent_ancestor(lookup) == "A"
        if handoff:
            transfer = next(
                row
                for row in starts.values()
                if row["attributes"].get("sdk.span.kind") == "handoff"
            )
            assert agent_ancestor(transfer) == "A"

    finally:
        await telemetry.shutdown()
