"""Observe the native managed connection's physical MCP catalog discovery."""

import json
import subprocess
import sys

import pytest

from agents import Agent, HttpMCP, telemetry
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_provider_lineage import history_peer
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


@pytest.mark.parametrize("mode", ["names", "redacted", "suppressed"])
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
            "from tests.test_native_managed_mcp_discovery_trace import native_case; "
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
    recorder = Recorder()
    await telemetry.configure(processors=[recorder])
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
                options=telemetry.TraceOptions(
                    include_sensitive_data=mode == "names", disabled=mode == "suppressed"
                ),
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
        if mode == "suppressed":
            assert events == []
            return
        assert [event["lifecycle"] for event in events] == ["start", "end"]
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
        if mode == "names":
            data = attributes["sdk.outcome.data"]
            assert (json.loads(data) if isinstance(data, str) else data) == {
                "tools": ["search"],
                "omitted_tool_count": 0,
            }
        else:
            assert "sdk.outcome.data" not in attributes
    finally:
        await telemetry.shutdown()
