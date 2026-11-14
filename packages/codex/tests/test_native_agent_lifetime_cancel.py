"""The native Agent lifetime owner joins cancellation and ends once."""

import asyncio
import subprocess
import sys

import pytest

from agents import Agent, NativeProvider, TurnStoppedError, telemetry
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_configured_agent_tool import MODEL, peer
from tests.test_native_trace_exporter import native_trace_child_env
from tests.test_tracing_native import Recorder


def test_agent_lifetime_cancellation_ends_the_original_entry_once():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from tests.test_native_agent_lifetime_cancel import native_case; "
            "asyncio.run(native_case())",
        ],
        env=native_trace_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def native_case():
    recorder = Recorder()
    await telemetry.configure(processors=[recorder])
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(event):
        if event.get("type") == "response.created":
            entered.set()
            await release.wait()

    try:
        async with timeout(20), peer() as model:
            model.respond(assistant_output("unobserved"))
            model.before_event = hold
            async with NativeProvider(
                base_url=model.url, api_key="synthetic", supports_websockets=False
            ) as provider:
                with telemetry.trace("agent-cancel-witness") as workflow:
                    trace_id = workflow.context.trace_id
                    async with Agent(
                        name="A", instructions="Reply", model=MODEL, provider=provider
                    ).session() as session:
                        turn = await session.submit("go")
                        await entered.wait()
                        try:
                            await turn.cancel()
                        finally:
                            release.set()
                        with pytest.raises(TurnStoppedError):
                            await turn.get_result()
            assert len(model.requests) == 1
        assert (await telemetry.force_flush()).status == "success"
        events = [
            row
            for row in recorder.events
            if row["trace_id"] == trace_id and row["attributes"].get("sdk.span.kind") == "agent"
        ]
        assert [row["lifecycle"] for row in events] == ["start", "end"]
        assert events[0]["span_id"] == events[1]["span_id"]
        assert events[1]["outcome"] != "ok"
    finally:
        release.set()
        await telemetry.shutdown()
