"""Exercise observation demand through real callbacks, transport and closed Turns."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents import Agent, NativeError, NativeProvider, ToolContext, Turn, function_tool
from agents.observation import OutputTextDelta
from tests._asyncio import timeout
from tests.native_fixtures import function_call, responses as responses, tool_output
from tests.test_native_agent_observation import text_response


@pytest.mark.parametrize("finish", [True, False], ids=["completion", "idle-deadline"])
async def test_public_observer_wakes_quiet_native_text_and_preserves_idle_deadline(
    responses, finish
):
    published: asyncio.Future[Turn[Any]] = asyncio.get_running_loop().create_future()
    quiet, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def remember(context: ToolContext[None]) -> str:
        published.set_result(context.submission)
        return "tool-result"

    async def before(event):
        if event["type"] == "response.output_item.done" and event["item"].get("id") == "out":
            quiet.set()
            await release.wait()

    responses.before_event = before
    responses.respond(function_call("remember", {}, "call"))
    responses.respond(*text_response("out", "first", "second"))
    provider = NativeProvider(
        base_url=responses.url,
        api_key="synthetic-text-demand",
        supports_websockets=False,
        stream_idle_timeout_ms=3000,
        stream_max_retries=0,
        request_max_retries=0,
    )
    running = None
    async with timeout(20), provider:
        try:
            running = asyncio.create_task(
                Agent(instructions="Use the tool.", tools=[remember], provider=provider).run(
                    "input"
                )
            )
            turn = await published
            await quiet.wait()
            if not finish:
                # Activate well into the existing native idle window. This is a
                # deadline contract check, not a latency measurement.
                await asyncio.sleep(2)
            reader = turn.events()  # Demand at construction, before iterator polling.
            prefix = ""
            async with timeout(2):
                async for event in reader:
                    if isinstance(event, OutputTextDelta):
                        prefix += event.delta
                        if prefix == "firstsecond":
                            break
            assert prefix == "firstsecond"
            await reader.aclose()
            if finish:
                release.set()
                completed = await running
                assert completed.final_output == "firstsecond"
            else:
                # Original timeout has about one second left; a demand-triggered
                # restart would require three seconds and violate this bound.
                with pytest.raises(NativeError, match="(?i)idle timeout"):
                    async with timeout(2):
                        await asyncio.shield(running)
        finally:
            release.set()
            if running is not None and not running.done():
                running.cancel()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
    late = [event async for event in turn.events()]
    assert (
        "".join(event.delta for event in late if isinstance(event, OutputTextDelta))
        == "firstsecond"
    )
    assert tool_output(responses.requests[1], "call") == "tool-result"


async def test_observation_demand_stays_with_fresh_session_and_late_replay_stays_closed(responses):
    published: asyncio.Future[Turn[Any]] = asyncio.get_running_loop().create_future()
    ready, release = asyncio.Event(), asyncio.Event()
    seen: list[Turn[Any]] = []

    @function_tool
    async def remember(context: ToolContext[None]) -> str:
        seen.append(context.submission)
        if not published.done():
            published.set_result(context.submission)
        return f"result-{len(seen)}"

    async def before(event):
        if event["type"] == "response.output_item.added" and event["item"].get("id") == "observed":
            ready.set()
            await release.wait()

    responses.before_event = before
    chunks_a = tuple(f"a{i};" for i in range(40))
    chunks_b = tuple(f"b{i};" for i in range(40))
    for name, chunks in [("observed", chunks_a), ("private", chunks_b)]:
        responses.respond(function_call("remember", {}, name + "-call"))
        responses.respond(*text_response(name, *chunks))
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-session-demand", supports_websockets=False
    )
    async with timeout(20), provider:
        agent = Agent(instructions="Use the tool.", tools=[remember], provider=provider)
        running = asyncio.create_task(agent.run("first-input", return_history=True))
        try:
            turn = await published
            await ready.wait()
            reader = turn.events()
            release.set()
            first = await running
            observed = [event async for event in reader]
            second = await agent.run("second-input", return_history=True)
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert first.turn.session_id != second.turn.session_id
    assert [event.delta for event in observed if isinstance(event, OutputTextDelta)] == list(
        chunks_a
    )
    late = [event async for event in second.turn.events()]
    private = [event.delta for event in late if isinstance(event, OutputTextDelta)]
    assert private[0] == chunks_b[0]
    assert "".join(private) == "".join(chunks_b)
    assert len(private) < len(chunks_b)
    assert first.final_output == "".join(chunks_a)
    assert second.final_output == "".join(chunks_b)
    assert "first-input" in json.dumps(first.history) and "second-input" not in json.dumps(
        first.history
    )
    assert "second-input" in json.dumps(second.history) and "first-input" not in json.dumps(
        second.history
    )
    assert first.final_output in json.dumps(first.history)
    assert second.final_output in json.dumps(second.history)
    assert tool_output(responses.requests[1], "observed-call") == "result-1"
    assert tool_output(responses.requests[3], "private-call") == "result-2"
