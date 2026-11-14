"""Observe the native managed connection's physical MCP catalog discovery."""

import asyncio
import json
import subprocess
import sys
from itertools import count

import pytest

from agents import Agent, HttpMCP, telemetry
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_provider_lineage import history_peer
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


@pytest.mark.parametrize("mode", ["allowed", "policy-suppressed"])
def test_native_managed_mcp_discovery_trace(mode):
    env = native_trace_child_env()
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; "
            "from tests.test_native_managed_named_mcp_discovery import native_case; "
            "asyncio.run(native_case(sys.argv[1]))",
            mode,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case(mode):
    admitted = []
    generated = []
    sequence = count(1)
    loop = asyncio.get_running_loop()

    class NamedPolicy(Recorder):
        def should_create_span(self, name, kind):
            assert asyncio.get_running_loop() is loop
            if kind == "mcp_tools":
                admitted.append((name, kind))
            return mode != "policy-suppressed" or kind != "mcp_tools"

    def generate_span_id():
        assert asyncio.get_running_loop() is loop
        value = f"{next(sequence):016x}"
        generated.append(value)
        return value

    recorder = NamedPolicy()
    await telemetry.configure(processors=[recorder])
    telemetry.set_tracing_id_generator(
        telemetry.TracingIdGenerator(generate_span_id=generate_span_id)
    )
    peer = Peer(names=("search",))
    try:
        async with (
            timeout(25),
            history_peer() as (provider, model, _, _url, _wire),
            peer.serve() as endpoint,
        ):
            model.respond(mcp_call("search", "mcp__records", "native-mcp"))
            model.respond(assistant_output("Done."))
            with telemetry.trace(
                "caller",
                options=telemetry.TraceOptions(include_sensitive_data=True),
            ) as caller:
                agent = Agent(
                    name="Reader",
                    instructions="Read.",
                    model="gpt-5.5",
                    provider=provider,
                    tools=[
                        HttpMCP(
                            name="records",
                            url=endpoint,
                            headers={"Authorization": "Bearer private-fixture"},
                            tools=["search"],
                            approval_mode="preapproved",
                        )
                    ],
                )
                async with agent.session() as session:
                    assert (
                        await (await session.submit("Read.")).get_result()
                    ).final_output == "Done."
                trace_id = caller.context.trace_id
        assert sum(row["method"] == "tools/list" for row in peer.requests) == 1
        assert len(peer.calls) == 1
        assert (await telemetry.force_flush()).status == "success"
        events = [
            event
            for event in recorder.events
            if event["trace_id"] == trace_id and event["name"] == "records"
        ]
        assert admitted == [("records", "mcp_tools")], admitted
        if mode == "policy-suppressed":
            assert events == []
            return
        assert [event["lifecycle"] for event in events] == ["start", "end"]
        assert events[0]["span_id"] in generated
        assert events[0]["span_id"] == events[-1]["span_id"]
        assert events[0]["parent_span_id"] is not None
        parents = {
            event["span_id"]
            for event in recorder.events
            if event["trace_id"] == trace_id and event["lifecycle"] == "start"
        }
        assert events[0]["parent_span_id"] in parents
        assert "private-fixture" not in json.dumps(events)
        attributes = events[-1]["attributes"]
        assert attributes["sdk.span.kind"] == "mcp_tools"
        assert attributes["sdk.outcome"] == "ok"
        data = attributes["sdk.outcome.data"]
        assert (json.loads(data) if isinstance(data, str) else data) == {
            "tools": ["search"],
            "omitted_tool_count": 0,
        }
    finally:
        telemetry.set_tracing_id_generator()
        await telemetry.shutdown()
