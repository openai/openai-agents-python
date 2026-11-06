"""Application summaries use Core compaction and its original callback owner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from threading import Event

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import agents
from agents import Agent, CompactionContext, PromptedOutput, function_tool
from agents.compaction import CompactionContext as PublicCompactionContext
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import (
    ChildPeer,
    agent_for,
    child_usage,
    handles,
    metadata,
    trust,
)
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration


def test_public_declaration_preserves_callback() -> None:
    assert agents.CompactionContext is PublicCompactionContext is CompactionContext
    assert "CompactionContext" in agents.__all__
    assert CompactionContext.__module__ == "agents.compaction"

    def compact(request):
        return "Keep the pending task."

    agent = Agent(instructions="Answer.", before_compact=compact)
    assert replace(agent, name="Other").before_compact is compact
    assert agent.with_output_type(PromptedOutput(int)).before_compact is compact


@pytest.mark.parametrize("remote_compaction", [False, True])
async def test_custom_summary_skips_model_and_preserves_current_instructions(
    responses, monkeypatch, remote_compaction
):
    if remote_compaction:
        from agents import _native

        start = _native.Engine.start_replaceable_thread
        create = _native.create_engine

        def select_remote(config):
            config["model_providers.native_agent"]["capabilities"] = {"remote_compaction": "v2"}

        def create_remote(config_json, *args, **kwargs):
            config = json.loads(config_json)
            select_remote(config)
            return create(json.dumps(config), *args, **kwargs)

        async def start_remote(engine, options_json, specs_json):
            # Select the same Core compactor used by OpenAI/Codex-login providers
            # while retaining the local synthetic server and normal SDK owner.
            options = json.loads(options_json)
            select_remote(options["config"])
            return await start(engine, json.dumps(options), specs_json)

        monkeypatch.setattr(_native, "create_engine", create_remote)
        monkeypatch.setattr(_native.Engine, "start_replaceable_thread", start_remote)
    application = {"pending": "Verify reference atlas-17."}
    calls = []

    async def compact(request: CompactionContext[dict[str, str]]):
        assert request.context is application
        calls.append(request)
        return request.context["pending"]

    responses.respond(assistant_output("Remembered."))
    responses.respond(assistant_output("Continued."))
    agent = Agent(
        instructions="Original instruction.",
        provider=provider(responses),
        before_compact=compact,
    )
    async with timeout(20), agent.session(context=application) as session:
        await (await session.submit("Remember the source.")).get_result()
        await session.update(instructions="Current instruction.")
        original = await session.save_state()
        await session.compact()
        assert len(responses.requests) == 1
        saved = await session.save_state()
        assert saved.history[: len(original.history)] == original.history
        summaries = [row["payload"] for row in saved.history if row["type"] == "compacted"]
        assert len(summaries) == 1
        assert "Verify reference atlas-17." in summaries[0]["message"]
        assert summaries[0].get("compaction_response_id") is None
        assert saved.instructions == "Current instruction."
        assert len(calls) == 1
        assert calls[0].session_id == session.id
        assert calls[0].subagent_id is None
        assert calls[0].trigger == "manual"
        assert "Remember the source." in json.dumps(calls[0].items)
        assert "atlas-17" not in repr(calls[0])
        result = await (await session.submit("Continue.")).get_result()
        assert result.final_output == "Continued."
    assert "Verify reference atlas-17." in json.dumps(responses.requests[-1]["input"])
    assert "Current instruction." in json.dumps(responses.requests[-1])


async def test_none_keeps_native_summary_and_runs_context_transform(responses):
    order = []

    def compact(request):
        order.append("before_compact")
        for item in request.items:
            if item.get("role") == "user":
                item["content"] = [{"type": "input_text", "text": "discarded edit"}]
        return None

    def transform(request):
        order.append(request.purpose)
        return None

    responses.respond(assistant_output("Remembered."))
    responses.respond(assistant_output("Native summary."))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        before_compact=compact,
        transform_context=transform,
    )
    async with timeout(20), agent.session() as session:
        await (await session.submit("Original input.")).get_result()
        await session.compact()
        saved = await session.save_state()
    assert order == ["turn", "before_compact", "compaction"]
    assert len(responses.requests) == 2
    assert "Original input." in json.dumps(responses.requests[-1])
    assert "discarded edit" not in json.dumps(responses.requests)
    assert "Native summary." in saved.to_json()


async def test_automatic_compaction_uses_current_task_state(responses):
    application = {"pending": "Run lookup."}
    calls = []

    @function_tool
    def lookup() -> str:
        application["pending"] = "Cite reference atlas-17."
        return "Lookup finished."

    def compact(request):
        calls.append(request)
        assert "Lookup finished." in json.dumps(request.items)
        return request.context["pending"]

    async def high_usage(event):
        if event["type"] == "response.completed" and event["response"]["id"] == "resp_1":
            event["response"]["usage"].update(
                input_tokens=260_000, output_tokens=1_000, total_tokens=261_000
            )

    responses.before_event = high_usage
    responses.respond(function_call("lookup", {}, "lookup-once"))
    responses.respond(assistant_output("Cited."))
    agent = Agent(
        instructions="Use lookup.",
        model="gpt-5.5",
        tools=[lookup],
        provider=provider(responses),
        before_compact=compact,
    )
    async with timeout(20):
        result = await asyncio.to_thread(
            agent.run_sync, "Look up the reference.", context=application
        )
    assert result.final_output == "Cited."
    assert len(calls) == 1
    assert calls[0].trigger == "auto"
    assert calls[0].turn_id == result.turn.turn_id
    assert len(responses.requests) == 2
    assert "Cite reference atlas-17." in json.dumps(responses.requests[-1]["input"])


async def test_restore_rebinds_callback_and_explicit_summary_bypasses_it(responses):
    calls = []

    def first(request):
        calls.append("first")
        return "Original summary."

    async def current(request):
        calls.append(request.context)
        return request.context

    agent = Agent(instructions="Answer.", provider=provider(responses), before_compact=first)
    async with timeout(20):
        async with agent.session() as session:
            await session.compact()
            saved = await session.save_state()
        async with replace(agent, before_compact=current).session(
            context="Current summary.", state=saved
        ) as restored:
            await restored.compact(summary="Explicit summary.")
            assert calls == ["first"]
            await restored.compact()
            final = await restored.save_state()
        responses.respond(assistant_output("Default after restore."))
        async with replace(agent, before_compact=None).session(state=saved) as plain:
            await plain.compact()
    assert calls == ["first", "Current summary."]
    assert "Current summary." in final.to_json()
    assert len(responses.requests) == 1


async def test_retained_summary_bypasses_hook_and_preserves_it_for_later_compaction(responses):
    calls = []

    def compact(request):
        calls.append(request.trigger)
        return "Callback summary."

    responses.respond(assistant_output("Original reference: atlas-17."))
    responses.respond(assistant_output("Continued."))
    agent = Agent(instructions="Answer.", provider=provider(responses), before_compact=compact)
    async with timeout(20), agent.session() as session:
        await (await session.submit("Read the reference.")).get_result()
        snapshot = await session.model_context()
        retain = [i for i, item in enumerate(snapshot.items) if item.get("role") == "assistant"]
        assert len(retain) == 1
        await session.compact(summary="Application summary.", snapshot=snapshot, retain=retain)
        assert calls == []
        await (await session.submit("Continue.")).get_result()
        await session.compact()
        saved = await session.save_state()
    assert calls == ["manual"]
    assert len(responses.requests) == 2
    assert "Original reference: atlas-17." in json.dumps(responses.requests[-1]["input"])
    assert "Application summary." in json.dumps(responses.requests[-1]["input"])
    assert "Callback summary." in saved.to_json()


@pytest.mark.parametrize("summary", ["", " \n\t"])
async def test_empty_summary_cannot_replace_history(responses, summary):
    agent = Agent(
        instructions="Answer.", provider=provider(responses), before_compact=lambda request: summary
    )
    async with timeout(15), agent.session() as session:
        with pytest.raises(ValueError, match="summary must not be empty"):
            await session.compact()
        saved = await session.save_state()
    assert not responses.requests
    assert not any(row["type"] == "compacted" for row in saved.history)


async def test_callback_failure_preserves_error_without_native_fallback(responses):
    problem = LookupError("synthetic-private-task-state")

    async def compact(request):
        raise problem

    agent = Agent(instructions="Answer.", provider=provider(responses), before_compact=compact)
    async with timeout(15), agent.session() as session:
        events = session.events()
        with pytest.raises(LookupError) as caught:
            await session.compact()
        assert caught.value is problem
        saved = await session.save_state()
    observed = [event async for event in events]
    assert not responses.requests
    assert not any(row["type"] == "compacted" for row in saved.history)
    assert "synthetic-private-task-state" not in repr(observed)


async def test_cancel_joins_sync_callback_and_discards_late_summary(responses):
    entered, release, exited = Event(), Event(), Event()

    def compact(request):
        entered.set()
        assert release.wait(15)
        exited.set()
        return "Late summary must not survive."

    agent = Agent(instructions="Answer.", provider=provider(responses), before_compact=compact)
    responses.respond(assistant_output("Successor."))
    async with timeout(20), agent.session() as session:
        pending = asyncio.create_task(session.compact())
        await asyncio.to_thread(entered.wait, 10)
        assert entered.is_set()
        pending.cancel()
        try:
            while not session._active.stopping:
                await asyncio.sleep(0)
            assert not pending.done()
            assert not exited.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert exited.is_set()
        result = await (await session.submit("Continue normally.")).get_result()
        assert result.final_output == "Successor."
        saved = await session.save_state()
    assert len(responses.requests) == 1
    assert "Late summary" not in saved.to_json()
    assert "Late summary" not in json.dumps(responses.requests)


async def test_cancel_joins_async_cleanup_and_discards_suppressed_cancellation(responses):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def compact(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            return "Late async summary."

    agent = Agent(instructions="Answer.", provider=provider(responses), before_compact=compact)
    responses.respond(assistant_output("Successor."))
    async with timeout(20), agent.session() as session:
        pending = asyncio.create_task(session.compact())
        await entered.wait()
        pending.cancel()
        try:
            await cleaning.wait()
            assert not pending.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        result = await (await session.submit("Continue normally.")).get_result()
        assert result.final_output == "Successor."
        saved = await session.save_state()
    assert len(responses.requests) == 1
    assert "Late async summary" not in saved.to_json()


async def test_custom_compaction_keeps_live_programmatic_state(responses):
    agent = Agent(
        instructions="Use only the requested code.",
        provider=provider(responses),
        programmatic_tool_calling=True,
        before_compact=lambda request: "Read the stored reference next.",
    )
    responses.respond(code_call('store("reference", "atlas-17"); text("stored");', "store"))
    responses.respond(assistant_output("Stored."))
    responses.respond(code_call('text(load("reference"));', "read"))
    responses.respond(assistant_output("Read."))
    async with timeout(30), agent.session() as session:
        await (await session.submit("Store the reference.")).get_result()
        await session.compact()
        result = await (await session.submit("Read the reference.")).get_result()
        assert result.final_output == "Read."
    assert len(responses.requests) == 4
    assert "atlas-17" in code_output(responses.requests[-1], "read")


async def test_native_transport_retry_does_not_repeat_callback():
    attempts, calls = [], []
    fixture = ResponsesFixture()
    fixture.respond(assistant_output("Retried native summary."))

    async def handle(request):
        attempts.append(await request.json())
        if len(attempts) == 1:
            return web.json_response(
                {"error": {"message": "temporarily unavailable"}},
                status=503,
                headers={"Retry-After": "0"},
            )
        return await fixture.handle(request)

    def compact(request):
        calls.append(request)
        return None

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with timeout(20), TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        agent = Agent(instructions="Answer.", provider=provider(fixture), before_compact=compact)
        async with agent.session() as session:
            await session.compact()
            saved = await session.save_state()
    assert len(attempts) == 2 and len(calls) == 1
    assert "Retried native summary." in saved.to_json()


class CompactionChildPeer(ChildPeer):
    def response(self):
        identity = metadata(self.requests[-1], self.request_headers[-1])
        if identity["agent_name"] == "/root" or not self.child_requests:
            return super().response()
        return [assistant_output("Child finished.")]


async def test_native_child_uses_its_own_compaction_identity(tmp_path, monkeypatch):
    calls = []

    async def compact(request):
        calls.append(request)
        return "Keep the child's completed reference."

    peer = CompactionChildPeer(("default",))
    child_usage(peer, {"/root": (11, 3, 0, 0), "/root/alpha": (260_000, 1_000, 0, 0)})
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        agent = replace(agent_for(peer, executor), before_compact=compact)
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Delegate the question.")
            assert (await root.get_result()).final_output == "root complete"
            (child,) = await handles(session, 1)
            assert (await child.get_result()).final_output == "Child finished."
            assert len(calls) == 1
            assert calls[0].session_id == session.id
            assert calls[0].turn_id == child.turn_id
            assert calls[0].subagent_id == child.child_id
            assert calls[0].trigger == "auto"
