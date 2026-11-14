"""Observe the native migration helper's physical MCP catalog discovery."""

import json
import subprocess
import sys

import pytest

from agents import Agent, telemetry
from agents.classic.mcp import MCPServerStreamableHttp
from agents.migration.mcp import MCPNamespace, select_mcp_tools
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_provider_lineage import history_peer
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


@pytest.mark.parametrize("mode", ["names", "redacted", "suppressed"])
def test_native_borrowed_mcp_discovery_trace(mode):
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
            "from tests.test_native_mcp_discovery_trace import native_case; "
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
    decisions = []

    class Policy(Recorder):
        def should_create_span(self, name, kind):
            decisions.append((name, kind))
            return not (mode == "suppressed" and kind == "mcp_tools")

    recorder = Policy()
    await telemetry.configure(processors=[recorder])
    peer = Peer(names=("search",))
    try:
        async with (
            timeout(25),
            history_peer() as (provider, model, _, _url, _wire),
            peer.serve() as endpoint,
        ):
            model.respond(mcp_call("search", "records", "native-mcp"))
            model.respond(assistant_output("Done."))
            with telemetry.trace(
                "caller", options=telemetry.TraceOptions(include_sensitive_data=mode == "names")
            ) as caller:
                async with MCPServerStreamableHttp(
                    params={
                        "url": endpoint,
                        "headers": {"Authorization": "Bearer private-fixture"},
                    },
                    name="non-exported-server",
                ) as server:
                    tools = await select_mcp_tools(MCPNamespace("records", server), context=None)
                    agent = Agent(
                        name="Reader",
                        instructions="Read.",
                        model="gpt-5.5",
                        provider=provider,
                        tools=tools,
                    )
                    assert (await agent.run("Read.")).final_output == "Done."
                trace_id, caller_id = caller.context.trace_id, caller.context.span_id
        assert decisions.count(("records", "mcp_tools")) == 1
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
        assert events[0]["parent_span_id"] == caller_id
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
