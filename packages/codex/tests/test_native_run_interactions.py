"""One-shot interaction handlers advance real native tools and child calls."""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from agents import (
    ActionRequested,
    Agent,
    FunctionTool,
    HttpMCP,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_child_manual import ManualChildren, declaration_for
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs
from tests.test_native_mcp_tools import Peer, mcp_call


class Record(BaseModel):
    value: str
    revision: int


@pytest.mark.parametrize(
    ("entry", "async_handler"),
    [("agent", False), ("agent", True), ("agent_sync", True), ("agent_sync", False)],
)
async def test_typed_manual_run_forwarding_preserves_reply(
    responses: ResponsesFixture, entry: str, async_handler: bool
) -> None:
    manual = FunctionTool(
        name="lookup", description="Read one record", input_type=LookupArgs, output_type=Record
    )
    responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
    responses.respond(assistant_output("record accepted"))
    agent = Agent(instructions="Read the record.", tools=[manual], provider=provider(responses))
    received: list[str] = []

    def reply(event: ActionRequested):
        request = manual.match(event)
        assert request is not None
        assert request.arguments == LookupArgs(key="A")
        received.append(request.call_id)
        return request.reply(Record(value="record-A", revision=7))

    async def reply_async(event: ActionRequested):
        await asyncio.sleep(0)
        return reply(event)

    handler = reply_async if async_handler else reply
    kwargs = {"on_interaction": handler}

    async def run():
        if entry == "agent":
            return await agent.run("Read A", **kwargs)
        return await asyncio.to_thread(agent.run_sync, "Read A", **kwargs)

    async with timeout(20):
        assert (await run()).final_output == "record accepted"
        assert len(responses.requests) == 2
        assert json.loads(tool_output(responses.requests[-1], "manual-A")) == {
            "value": "record-A",
            "revision": 7,
        }
    assert received == ["manual-A"]


async def test_manual_run_callbacks_advance_concurrent_code_consumers(
    responses: ResponsesFixture,
) -> None:
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=Record
    )
    responses.respond(
        code_call(
            "const a=tools.lookup({key:'a'}).then(x=>"
            "JSON.stringify(x).includes('record-a')?'MATCH:a':'wrong-a');"
            "const b=tools.lookup({key:'b'}).then(x=>"
            "JSON.stringify(x).includes('record-b')?'MATCH:b':'wrong-b');"
            "text(JSON.stringify(await Promise.all([a,b])));",
            "code",
        )
    )
    responses.respond(assistant_output("code complete"))
    agent = Agent(
        instructions="Read both records.",
        tools=[manual],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    started: set[str] = set()
    both = asyncio.Event()

    async def handle(event: ActionRequested):
        request = manual.match(event)
        assert request is not None
        key = request.arguments.key
        started.add(key)
        if started == {"a", "b"}:
            both.set()
        await both.wait()
        return request.reply(Record(value="record-" + key, revision=3))

    async with timeout(20):
        result = await agent.run("Read both records", on_interaction=handle)
    assert result.final_output == "code complete"
    assert started == {"a", "b"}
    rendered = code_output(responses.requests[1], "code")
    assert "MATCH:a" in rendered and "MATCH:b" in rendered
    assert "wrong-a" not in rendered and "wrong-b" not in rendered


async def test_manual_run_releases_following_serial_mcp_call(
    responses: ResponsesFixture,
) -> None:
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    peer = Peer(names=("search",), annotations={})
    responses.respond(
        function_call("lookup", {"key": "first"}, "manual"),
        mcp_call("search", "mcp__docs", "native"),
    )
    responses.respond(assistant_output("both tools complete"))
    manual_started = asyncio.Event()
    calls_sent = asyncio.Event()

    async def offer_calls_in_order(event):
        if event.get("item", {}).get("call_id") == "native":
            # Response order alone does not order Core's independently scheduled tasks.
            await manual_started.wait()
        elif event["type"] == "response.completed":
            calls_sent.set()

    responses.before_event = offer_calls_in_order
    async with timeout(25), peer.serve() as url:
        agent = Agent(
            instructions="Read both records.",
            provider=provider(responses),
            tools=[
                manual,
                HttpMCP(name="docs", url=url, tools=["search"], approval_mode="preapproved"),
            ],
        )

        async def handle(event: ActionRequested):
            request = manual.match(event)
            assert request is not None
            manual_started.set()
            await calls_sent.wait()
            assert peer.calls == []
            return request.reply("first-record")

        try:
            result = await agent.run("Read both", on_interaction=handle)
        finally:
            manual_started.set()
            calls_sent.set()
    assert result.final_output == "both tools complete"
    assert tool_output(responses.requests[1], "manual") == "first-record"
    assert peer.label in tool_output(responses.requests[1], "native")
    assert [(row["name"], row["_meta"]["callId"]) for row in peer.calls] == [("search", "native")]


@pytest.mark.requires_native_linux_sandbox
async def test_manual_run_with_executor_preserves_single_remote_effect(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        responses.respond(
            function_call("lookup", {"key": "A"}, "manual"),
            function_call(
                "exec_command",
                {"cmd": "printf x >> effect-count; printf receipt", "yield_time_ms": 1000},
                "command",
            ),
        )
        command_outputs: list[str] = []

        class UntilCommandDone(deque[list[dict[str, Any]]]):
            def __bool__(self) -> bool:
                return True

            def popleft(self) -> list[dict[str, Any]]:
                if len(self):
                    return super().popleft()
                body = responses.requests[-1]
                calls = [
                    item["call_id"]
                    for item in body["input"]
                    if item.get("type") == "function_call_output"
                    and str(item.get("call_id", "")).startswith("command")
                ]
                output = tool_output(body, calls[-1])
                command_outputs.append(output)
                running = re.search(r"Process running with session ID (\d+)", output)
                if running:
                    return [
                        function_call(
                            "write_stdin",
                            {"session_id": int(running[1]), "chars": "", "yield_time_ms": 1000},
                            f"command-poll-{len(responses.requests)}",
                        )
                    ]
                assert re.search(r"Process exited with code 0\b", output), output
                return [assistant_output("recorded")]

        responses.replies = UntilCommandDone(responses.replies)
        agent = Agent(
            instructions="Read and record.",
            provider=provider(responses),
            tools=[manual],
        )

        async def handle(event: ActionRequested):
            request = manual.match(event)
            assert request is not None
            return request.reply("record-A")

        result = await agent.run(
            "Read and record", on_interaction=handle, environment=declaration(executor)
        )
        assert result.final_output == "recorded"
        assert (executor.root / "effect-count").read_text() == "x"
        assert sum(row.get("method") == "process/start" for row in executor.requests) == 1
        assert tool_output(responses.requests[1], "manual") == "record-A"
        assert "receipt" in "".join(command_outputs)


async def test_child_manual_calls_progress_while_root_run_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ManualChildren()
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    completed_children: set[str] = set()
    actions: dict[str, str] = {}
    children_replied = asyncio.Event()

    async def hold_root(event):
        item = event.get("item", {})
        for part in item.get("content", []):
            text = part.get("text")
            if text in {"alpha complete", "beta complete"}:
                completed_children.add(text)
                if completed_children == {"alpha complete", "beta complete"}:
                    children_replied.set()
            elif text == "root complete":
                await children_replied.wait()

    peer.before_event = hold_root
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, manual)

        async def handle(event: ActionRequested):
            request = manual.match(event)
            assert request is not None and request.subagent_id is not None
            actions[request.arguments.key] = request.subagent_id
            return request.reply("record-" + request.arguments.key)

        try:
            result = await agent.run(
                "Start both children", on_interaction=handle, environment=declaration(executor)
            )
        finally:
            children_replied.set()
            await agent.provider.aclose()
        assert result.final_output == "root complete"
        assert set(actions) == {"alpha", "beta"} and len(set(actions.values())) == 2
        assert completed_children == {"alpha complete", "beta complete"}
        for name in ("alpha", "beta"):
            assert tool_output(peer.child_requests[name][1], "same-manual-id") == "record-" + name


async def test_root_run_completion_joins_pending_child_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = ManualChildren()
    peer.roles = ("default",)
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    entered = asyncio.Event()
    draining = asyncio.Event()
    finish_cleanup = asyncio.Event()

    async def hold_root(event):
        item = event.get("item", {})
        if any(part.get("text") == "root complete" for part in item.get("content", [])):
            await entered.wait()

    peer.before_event = hold_root
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, manual)

        async def handle(event: ActionRequested):
            request = manual.match(event)
            assert request is not None and request.subagent_id is not None
            assert request.arguments.key == "alpha"
            entered.set()
            try:
                await asyncio.Event().wait()
                return request.reply("late-result")
            finally:
                draining.set()
                await finish_cleanup.wait()

        run = asyncio.create_task(
            agent.run(
                "Start a child",
                on_interaction=handle,
                environment=declaration(executor),
            )
        )
        try:
            await draining.wait()
            await asyncio.sleep(0)
            assert not run.done(), "Run returned before its child's handler cleanup finished"
            assert len(peer.child_requests["alpha"]) == 1
            finish_cleanup.set()
            assert (await run).final_output == "root complete"
            assert len(peer.child_requests["alpha"]) == 1
        finally:
            entered.set()
            finish_cleanup.set()
            if not run.done():
                run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            await agent.provider.aclose()


async def test_host_child_reply_wins_during_sdk_delivery(tmp_path, monkeypatch):
    from pydantic import model_validator

    from agents import InteractionExpiredError, Session

    validated = []

    class Answer(BaseModel):
        value: str

        @model_validator(mode="after")
        def record(self):
            validated.append(self.value)
            return self

    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=Answer
    )
    peer = ManualChildren()
    at_lookup, release_lookup = asyncio.Event(), asyncio.Event()
    allow_alpha, allow_beta, children_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    completed = set()
    events, tasks = {}, {}
    run = Session._run_with_interactions

    async def hold_root(event):
        for part in event.get("item", {}).get("content", []):
            text = part.get("text")
            if text == "alpha complete":
                await allow_alpha.wait()
            if text in {"alpha complete", "beta complete"}:
                completed.add(text)
                if len(completed) == 2:
                    children_done.set()
            if text == "root complete":
                await children_done.wait()

    async def instrument(session, submission, handler, *, return_state=False, pause_manual=()):
        native = session._native

        class HeldChild:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _child_turn(self, child_id):
                if asyncio.current_task() is tasks.get("alpha"):
                    at_lookup.set()
                    await release_lookup.wait()
                    receipt = await native._child_turn(child_id)
                    # Keep alpha's terminal pending until the SDK rechecks delivery,
                    # otherwise terminal shutdown could retire the handler first.
                    asyncio.get_running_loop().call_soon(allow_alpha.set)
                    return receipt
                return await native._child_turn(child_id)

        monkeypatch.setattr(session, "_native", HeldChild())
        return await run(
            session, submission, handler, return_state=return_state, pause_manual=pause_manual
        )

    monkeypatch.setattr(Session, "_run_with_interactions", instrument)
    peer.before_event = hold_root
    async with (
        timeout(35),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, manual)

        async def handle(event):
            request = manual.match(event)
            assert request is not None and request.subagent_id is not None
            name = request.arguments.key
            events[name] = request
            tasks[name] = asyncio.current_task()
            if name == "beta":
                await allow_beta.wait()
            return request.reply({"value": "sdk-" + name})

        work = asyncio.create_task(
            agent.run(
                "Start both children", on_interaction=handle, environment=declaration(executor)
            )
        )
        reached_lookup = asyncio.create_task(at_lookup.wait())
        try:
            done, _ = await asyncio.wait(
                (work, reached_lookup), return_when=asyncio.FIRST_COMPLETED
            )
            if work in done:
                await work
                pytest.fail("Run completed before the SDK reached the child delivery lookup")
            # Outside callback authority, a host can answer the exact same grant.
            alpha = events["alpha"]
            await alpha.respond({"value": "host-alpha"})
            with pytest.raises(InteractionExpiredError):
                await alpha.respond({"value": "duplicate"})
            assert validated == ["host-alpha"]
            release_lookup.set()
            allow_beta.set()
            result = await work
            assert result.final_output == "root complete"
            assert completed == {"alpha complete", "beta complete"}
            assert sorted(validated) == ["host-alpha", "sdk-beta"]
            assert len(peer.child_requests["alpha"]) == len(peer.child_requests["beta"]) == 2
            assert json.loads(tool_output(peer.child_requests["alpha"][1], "same-manual-id")) == {
                "value": "host-alpha"
            }
            assert json.loads(tool_output(peer.child_requests["beta"][1], "same-manual-id")) == {
                "value": "sdk-beta"
            }
            await executor.wait_disconnected()
        finally:
            release_lookup.set()
            allow_alpha.set()
            allow_beta.set()
            children_done.set()
            if not work.done():
                work.cancel()
            if not reached_lookup.done():
                reached_lookup.cancel()
            await asyncio.gather(work, reached_lookup, return_exceptions=True)
            await agent.provider.aclose()
