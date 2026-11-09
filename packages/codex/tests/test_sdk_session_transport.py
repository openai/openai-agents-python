"""Public Session ownership across retained HTTP and native Core promotion."""

from __future__ import annotations

import asyncio
import gc
import importlib
import json
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import Field, ValidationError

from agents import Agent, NativeProvider, Session, TurnStoppedError, function_tool
from agents.observation import OutputTextDelta, ToolFinished
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call, tool_output
from tests.test_native_agent_observation import text_response
from tests.test_native_instruction_updates import accepted_items, application_texts


@dataclass
class HTTPPeer(ResponsesFixture):
    # Retain actual transport objects: Python object-ID reuse cannot masquerade
    # as connection reuse. No test helper closes a client socket.
    transports: list[asyncio.Transport] = field(default_factory=list)
    finished_bodies: int = 0
    disconnected_bodies: int = 0
    expected_model: str = "gpt-5.2"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        transport = request.transport
        assert transport is not None
        self.transports.append(transport)
        if not self.requests:
            assert (await request.json())["model"] == self.expected_model
        try:
            response = await super().handle(request)
        except ConnectionResetError:
            self.disconnected_bodies += 1
            raise
        self.finished_bodies += 1
        return response

    def provider(self) -> NativeProvider:
        # Exercise the public auto/default choice for a custom HTTP provider.
        return NativeProvider(base_url=self.url, api_key="synthetic-retained-http-key")


@asynccontextmanager
async def http_peer(*, expected_model: str = "gpt-5.2") -> AsyncIterator[HTTPPeer]:
    fixture = HTTPPeer(expected_model=expected_model)
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    async with TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        yield fixture


async def disconnected(transports: list[asyncio.Transport]) -> None:
    # Passive observation must finish before TestServer can close its sockets.
    async with timeout(5):
        while any(not transport.is_closing() for transport in transports):
            await asyncio.sleep(0.001)


def assert_paired_continuation(
    request: dict[str, Any],
    expected_outputs: dict[str, str],
    new_input: str,
    *,
    call_counts: dict[str, int] | None = None,
) -> None:
    """Check the actual provider input, not only saved Core history."""
    items = request["input"]
    user_indices = [
        index
        for index, item in enumerate(items)
        if item.get("role") == "user" and new_input in json.dumps(item)
    ]
    assert len(user_indices) == 1
    new_user = user_indices[0]
    calls = [item for item in items if item.get("type") == "function_call"]
    outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert {item["call_id"] for item in calls} == set(expected_outputs)
    counts = call_counts or dict.fromkeys(expected_outputs, 1)
    assert len(calls) == len(outputs) == sum(counts.values()), request
    assert {item["call_id"] for item in outputs} == set(expected_outputs)
    for call_id, expected in expected_outputs.items():
        call_indices = [
            index
            for index, item in enumerate(items)
            if item.get("type") == "function_call" and item["call_id"] == call_id
        ]
        output_indices = [
            index
            for index, item in enumerate(items)
            if item.get("type") == "function_call_output" and item["call_id"] == call_id
        ]
        assert len(call_indices) == len(output_indices) == counts[call_id]
        for call_index, output_index in zip(call_indices, output_indices, strict=True):
            assert call_index < output_index < new_user
            output = items[output_index]["output"]
            actual = (
                output
                if isinstance(output, str)
                else "".join(item.get("text", "") for item in output)
            )
            if expected == "aborted":
                assert actual == "aborted" or actual.startswith("aborted by user after ")
            else:
                assert actual == expected


@pytest.mark.parametrize("use_tool", [False, True], ids=["text", "function"])
@pytest.mark.parametrize("model", ["gpt-5.2", None], ids=["responses", "default-lite"])
async def test_agent_fresh_public_runs_keep_history_isolated_and_close_http(
    use_tool: bool, model: str | None
) -> None:
    effects: list[str] = []

    @function_tool
    async def lookup(value: str) -> str:
        effects.append(value)
        return "resolved:" + value

    async with (
        timeout(30),
        http_peer(expected_model=model or "gpt-6.1-sol") as peer,
        peer.provider() as provider,
    ):
        agent = Agent(
            instructions="Answer precisely.",
            model=model,
            provider=provider,
            tools=[lookup] if use_tool else [],
        )
        roots = []
        for index in range(3):
            literal = f"literal-{index}"
            if use_tool:
                peer.respond(function_call("lookup", {"value": literal}, "reused-call-id"))
            peer.respond(assistant_output("BENCHMARK_OK"))
            result = await agent.run(f"isolated-input-{index}")
            assert result.final_output == "BENCHMARK_OK"
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == (28 if use_tool else 14)
            roots.append(result.turn.session_id)
            first = peer.requests[-2 if use_tool else -1]
            assert f"isolated-input-{index}" in json.dumps(first["input"])
            for earlier in range(index):
                assert f"isolated-input-{earlier}" not in json.dumps(first["input"])
            if use_tool:
                assert tool_output(peer.requests[-1], "reused-call-id") == "resolved:" + literal
        assert len(set(roots)) == 3
        assert effects == ([f"literal-{index}" for index in range(3)] if use_tool else [])
        assert len(peer.requests) == (6 if use_tool else 3)
        assert peer.finished_bodies == len(peer.requests)
        await provider.aclose()
        del agent
        gc.collect()
        await disconnected(peer.transports)


async def test_shared_provider_keeps_fresh_agents_and_model_catalogs_independent() -> None:
    effects: list[str] = []

    @function_tool
    async def alpha(value: str) -> str:
        effects.append("alpha:" + value)
        return "first:" + value

    @function_tool
    async def beta(value: str) -> str:
        effects.append("beta:" + value)
        return "second:" + value

    async with timeout(30), http_peer() as peer, peer.provider() as connection:
        peer.respond(function_call("alpha", {"value": "apple"}, "shared-provider-call"))
        peer.respond(assistant_output("alpha complete"))
        first = await Agent(
            model="gpt-5.2",
            instructions="Alpha private directions.",
            tools=[alpha],
            provider=connection,
        ).run(
            "alpha private question",
        )
        peer.respond(function_call("beta", {"value": "pear"}, "shared-provider-call"))
        peer.respond(assistant_output("beta complete"))
        second = await Agent(
            model="gpt-5.5",
            instructions="Beta private directions.",
            tools=[beta],
            provider=connection,
        ).run(
            "beta private question",
        )
        peer.respond(assistant_output("gamma complete"))
        third = await Agent(
            model="gpt-5.2", instructions="Gamma private directions.", provider=connection
        ).run(
            "gamma private question",
        )
        assert [first.final_output, second.final_output, third.final_output] == [
            "alpha complete",
            "beta complete",
            "gamma complete",
        ]
        assert len({result.turn.session_id for result in (first, second, third)}) == 3
        for result, tokens, context_window in (
            (first, 28, None),
            (second, 28, 258_400),
            (third, 14, None),
        ):
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == tokens
            assert result.thread_usage.model_context_window == context_window
        assert effects == ["alpha:apple", "beta:pear"]
        assert len(peer.requests) == peer.finished_bodies == 5
        assert tool_output(peer.requests[1], "shared-provider-call") == "first:apple"
        assert tool_output(peer.requests[3], "shared-provider-call") == "second:pear"
        for request, (model, instruction, prompt, tool_name) in zip(
            (peer.requests[0], peer.requests[2], peer.requests[4]),
            (
                ("gpt-5.2", "Alpha private directions.", "alpha private question", "alpha"),
                ("gpt-5.5", "Beta private directions.", "beta private question", "beta"),
                ("gpt-5.2", "Gamma private directions.", "gamma private question", None),
            ),
            strict=True,
        ):
            assert request["model"] == model
            assert application_texts(request) == [instruction]
            encoded = json.dumps(request["input"])
            for question in (
                "alpha private question",
                "beta private question",
                "gamma private question",
            ):
                assert (question in encoded) == (question == prompt)
            functions = [
                item["name"] for item in request.get("tools", []) if item["type"] == "function"
            ]
            assert functions == ([] if tool_name is None else [tool_name])
            if model == "gpt-5.5":
                assert request["text"]["verbosity"] == "low"
            else:
                assert request.get("text") is None
        assert all(
            headers["authorization"] == "Bearer synthetic-retained-http-key"
            for headers in peer.request_headers
        )
        await connection.aclose()
        del connection
        gc.collect()
        await disconnected(peer.transports)


async def test_shared_provider_cancelled_session_does_not_stop_independent_sibling() -> None:
    first_entered = asyncio.Event()
    first_cleaned = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def pending_first() -> str:
        effects.append("first")
        first_entered.set()
        try:
            await asyncio.Future()
        finally:
            first_cleaned.set()
        return "unreachable"

    @function_tool
    async def pending_second() -> str:
        effects.append("second")
        second_entered.set()
        await second_release.wait()
        return "independent result"

    async with timeout(30), http_peer() as peer, peer.provider() as connection:
        first_agent = Agent(
            model="gpt-5.2", instructions="Only first.", tools=[pending_first], provider=connection
        )
        second_agent = Agent(
            model="gpt-5.2",
            instructions="Only second.",
            tools=[pending_second],
            provider=connection,
        )
        async with first_agent.session() as first, second_agent.session() as second:
            peer.respond(function_call("pending_first", {}, "same-call"))
            first_handle = await first.submit("first private input")
            await first_entered.wait()
            peer.respond(function_call("pending_second", {}, "same-call"))
            second_handle = await second.submit("second private input")
            await second_entered.wait()
            waiting = asyncio.create_task((second_handle).get_result())
            try:
                assert first.id != second.id
                await first._cancel(first_handle)
                assert first_cleaned.is_set()
                with pytest.raises(TurnStoppedError):
                    await (first_handle).get_result()
                assert not waiting.done()
                assert effects == ["first", "second"]
                assert len(peer.requests) == 2
                peer.respond(assistant_output("sibling complete"))
                second_release.set()
                result = await waiting
                assert result.final_output == "sibling complete"
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == 28
                assert tool_output(peer.requests[2], "same-call") == "independent result"
                assert "first private input" not in json.dumps(peer.requests[2]["input"])
                assert application_texts(peer.requests[2]) == ["Only second."]
                assert all(
                    headers["authorization"] == "Bearer synthetic-retained-http-key"
                    for headers in peer.request_headers
                )
            finally:
                second_release.set()
                await asyncio.gather(waiting, return_exceptions=True)
        await connection.aclose()
        del first, second, first_agent, second_agent, connection
        gc.collect()
        await disconnected(peer.transports)


@pytest.mark.parametrize("model", ["gpt-5.2", None], ids=["responses", "default-lite"])
async def test_cancelled_stream_disconnects_then_same_session_can_complete(
    model: str | None,
) -> None:
    async with (
        timeout(30),
        http_peer(expected_model=model or "gpt-6.1-sol") as peer,
        peer.provider() as provider,
    ):
        gate = asyncio.Event()
        peer.delta_gate = gate
        peer.respond(*text_response("partial", "partial", "must not complete"))
        peer.respond(assistant_output("after cancellation"))
        agent = Agent(model=model, instructions="Answer.", provider=provider)
        try:
            async with agent.session() as session:
                handle = await session.submit("hold the stream")
                async for event in handle.events():
                    if isinstance(event, OutputTextDelta):
                        assert event.delta == "partial"
                        break
                await session._cancel(handle)
                await session._cancel(handle)
                with pytest.raises(TurnStoppedError):
                    await (handle).get_result()
                await disconnected(peer.transports[:1])
                gate.set()
                peer.delta_gate = None
                second = await session.submit("continue with a fresh request")
                assert (await (second).get_result()).final_output == "after cancellation"
                assert [turn["status"] for turn in [entry async for entry in session.turns()]] == [
                    "cancelled",
                    "completed",
                ]
            assert len(peer.requests) == 2
            assert peer.transports[0] is not peer.transports[1]
        finally:
            gate.set()
        await provider.aclose()
        del agent
        gc.collect()
        await disconnected(peer.transports)


async def test_saved_lightweight_history_restores_core_with_accepted_settings() -> None:
    async with timeout(30), http_peer() as peer:
        peer.respond(assistant_output("remembered"))
        peer.respond(assistant_output("restored"))
        agent = Agent(
            instructions="Remember the exact violet label.",
            model="gpt-5.2",
            model_settings={"reasoning_effort": "low"},
            provider=peer.provider(),
        )
        async with agent.session() as original:
            first = await original.submit("violet label")
            assert (await (first).get_result()).final_output == "remembered"
            await original.update(
                model="gpt-5.2-codex",
                model_settings={"reasoning_effort": "high", "service_tier": "flex"},
            )
            saved = await original.save_state()
            before_items = [entry async for entry in original.items()]
            before_turns = [entry async for entry in original.turns()]
        async with agent.session(state=saved) as restored:
            assert restored.id == original.id
            assert [entry async for entry in restored.items()] == before_items
            assert [entry async for entry in restored.turns()] == before_turns
            result = await (await restored.submit("what label?")).get_result()
            assert result.final_output == "restored"
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 28
        assert len(peer.requests) == 2
        request = peer.requests[1]
        assert request["model"] == "gpt-5.2-codex"
        assert request["reasoning"]["effort"] == "high"
        assert request["service_tier"] == "flex"
        assert "Remember the exact violet label." in json.dumps(request["input"])
        assert "violet label" in json.dumps(request["input"])
        assert "remembered" in json.dumps(request["input"])
        assert "synthetic-retained-http-key" not in saved.to_json()
        assert all(
            item.get("id") is None or item["id"].startswith("msg_")
            for request in peer.requests
            for item in request["input"]
            if item.get("type") == "message"
        )


async def test_two_tool_generations_survive_idle_compaction_with_original_ceiling() -> None:
    effects: list[str] = []

    @function_tool(name="lookup")
    async def old(value: str) -> str:
        pytest.fail("An obsolete callback ran after replacement")

    @function_tool(name="lookup")
    async def current(value: str) -> str:
        effects.append(value)
        return "current:" + value

    @function_tool
    async def forbidden() -> str:
        pytest.fail("A tool outside the opening ceiling ran")

    async with timeout(30), http_peer() as peer:
        peer.respond(assistant_output("remembered before compaction"))
        peer.respond(assistant_output("Summary: preserve violet and the lookup instruction."))
        peer.respond(function_call("lookup", {"value": "exact-after-promotion"}, "after-compact"))
        peer.respond(assistant_output("completed after compaction"))
        agent = Agent(
            model="gpt-5.2",
            instructions="Remember violet. Use lookup.",
            tools=[old],
            provider=peer.provider(),
        )
        async with agent.session(tools=[current]) as session:
            original_id = session.id
            assert (await (await session.submit("remember violet")).get_result()).final_output == (
                "remembered before compaction"
            )
            with pytest.raises(ValueError):
                await session.submit("foreign", tools=[forbidden])
            await session.compact()
            assert session.id == original_id
            state = await session.save_state()
            assert any(record["type"] == "compacted" for record in state.history)
            result = await (await session.submit("use lookup now")).get_result()
            assert result.final_output == "completed after compaction"
            assert [turn["status"] for turn in [entry async for entry in session.turns()]] == [
                "completed",
                "completed",
                "completed",
            ]
        assert effects == ["exact-after-promotion"]
        assert len(peer.requests) == 4
        assert tool_output(peer.requests[3], "after-compact") == "current:exact-after-promotion"


async def test_cancel_after_public_tool_acceptance_keeps_output_without_replaying_effect() -> None:
    effects: list[str] = []
    blocked, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def lookup(value: str) -> str:
        effects.append(value)
        return "accepted:" + value

    async with timeout(30), http_peer() as peer:

        async def hold_continuation(event: dict[str, Any]) -> None:
            if len(peer.requests) == 2 and event["type"] == "response.created":
                blocked.set()
                await release.wait()

        peer.before_event = hold_continuation
        peer.respond(function_call("lookup", {"value": "once"}, "accepted-call"))
        peer.respond(assistant_output("unaccepted continuation"))
        agent = Agent(
            model="gpt-5.2",
            instructions="Use lookup once.",
            tools=[lookup],
            provider=peer.provider(),
        )
        try:
            async with agent.session() as session:
                handle = await session.submit("use lookup")
                async for event in handle.events():
                    if isinstance(event, ToolFinished):
                        assert event.call_id == "accepted-call"
                        assert event.status == "completed"
                        break
                await blocked.wait()
                await session._cancel(handle)
                saved = await session.save_state()
                assert [entry async for entry in session.turns()][0]["status"] == "cancelled"
                outputs = [
                    record["payload"]
                    for record in saved.history
                    if record["type"] == "response_item"
                    and record["payload"].get("type") == "function_call_output"
                ]
                assert len(outputs) == 1
                assert outputs[0]["call_id"] == "accepted-call"
                assert "accepted:once" in json.dumps(outputs[0])
                release.set()
        finally:
            release.set()
        peer.before_event = None
        peer.respond(assistant_output("restored without another effect"))
        async with agent.session(state=saved) as restored:
            result = await (await restored.submit("continue")).get_result()
            assert result.final_output == "restored without another effect"
        assert effects == ["once"]
        assert len(peer.requests) == 3
        assert tool_output(peer.requests[2], "accepted-call") == "accepted:once"


@pytest.mark.parametrize("invalid_second", ["duplicate", "unadvertised"])
async def test_streamed_function_calls_reject_invalid_dispatch_without_replaying_effects(
    invalid_second: str,
) -> None:
    from agents import NativeError

    effects: list[str] = []

    @function_tool
    async def lookup(value: str) -> str:
        effects.append(value)
        return "effect:" + value

    async with timeout(30), http_peer() as peer:
        peer.respond(
            function_call("lookup", {"value": "first"}, "call-first"),
            function_call(
                "lookup" if invalid_second == "duplicate" else "not_advertised",
                {"value": "second"},
                "call-first" if invalid_second == "duplicate" else "call-second",
            ),
        )
        peer.respond(assistant_output("native rejection recorded"))
        agent = Agent(
            model="gpt-5.2",
            instructions="Use only lookup.",
            tools=[lookup],
            provider=peer.provider(),
        )
        async with agent.session() as session:
            handle = await session.submit("run the tool batch")
            rejected_by_session = False
            try:
                result = await (handle).get_result()
            except NativeError as error:
                # A duplicate received after a previous Python reply reaches
                # the existing Session guard. Pending native duplicates are
                # instead answered by Core with a per-call rejection.
                assert invalid_second == "duplicate"
                assert "reused within this submission" in str(error)
                rejected_by_session = True
            else:
                assert result.final_output == "native rejection recorded"
            if invalid_second == "duplicate":
                # Concurrent native calls can reach admission in either order,
                # but the same ID must never authorize two effects.
                assert len(effects) <= 1
                assert set(effects) <= {"first", "second"}
                if not rejected_by_session:
                    assert len(effects) == 1
            else:
                assert effects == ["first"]
            accepted_effects = effects.copy()
            session_id = session.id
            # If the turn failed before Core's continuation, the fixture still
            # owns that unused response. Recovery can consume the same text.
            if not peer.replies:
                peer.respond(assistant_output("native rejection recorded"))
            result = await (await session.submit("continue after rejected batch")).get_result()
            assert result.final_output == "native rejection recorded"
            assert session.id == session_id
            items = peer.requests[-1]["input"]
            outputs = [item for item in items if item.get("type") == "function_call_output"]
            calls = [item for item in items if item.get("type") == "function_call"]
            assert len(calls) == len(outputs) == 2
            assert sorted(call["call_id"] for call in calls) == sorted(
                output["call_id"] for output in outputs
            )
            if not rejected_by_session:
                rejection = (
                    "Duplicate Python tool call"
                    if invalid_second == "duplicate"
                    else "unsupported call:"
                )
                assert sum(rejection in json.dumps(output) for output in outputs) == 1
            finished = [
                event
                async for event in handle.events()
                if isinstance(event, ToolFinished) and event.status == "completed"
            ]
            for value in accepted_effects if finished else ():
                count = sum("effect:" + value in json.dumps(output) for output in outputs)
                if rejected_by_session and count == 0:
                    # The ordinary reply channel can accept Python's output before
                    # Core commits it. Duplicate-ID rejection may then abort both
                    # in-flight calls. A completed continuation must retain the result.
                    assert all(
                        str(output["output"]).startswith("aborted by user after ")
                        for output in outputs
                    ), outputs
                else:
                    assert count == 1, outputs
            saved = await session.save_state()
        assert effects == accepted_effects
        assert 2 <= len(peer.requests) <= 3
        assert peer.requests[0]["model"] == "gpt-5.2"
        peer.respond(assistant_output("restored without repeating effects"))
        async with agent.session(state=saved) as restored:
            result = await (await restored.submit("after restore")).get_result()
            assert result.final_output == "restored without repeating effects"
            restored_outputs = [
                item
                for item in peer.requests[-1]["input"]
                if item.get("type") == "function_call_output"
            ]
            assert [(item["call_id"], item["output"]) for item in restored_outputs] == [
                (item["call_id"], item["output"]) for item in outputs
            ]
        assert effects == accepted_effects


async def test_reported_usage_promotes_before_followup_without_replaying_tool() -> None:
    from agents.observation import ContextCompacted

    effects: list[str] = []

    @function_tool
    async def lookup() -> str:
        effects.append("called")
        return "short"

    async with timeout(30), http_peer() as peer:

        async def report_large_usage(event: dict[str, Any]) -> None:
            if len(peer.requests) == 1 and event["type"] == "response.completed":
                event["response"]["usage"] = {
                    "input_tokens": 249_999,
                    "output_tokens": 1,
                    "total_tokens": 250_000,
                }

        peer.before_event = report_large_usage
        peer.respond(function_call("lookup", {}, "once-before-compaction"))
        peer.respond(assistant_output("Compacted summary: lookup returned short."))
        peer.respond(assistant_output("after required compaction"))
        agent = Agent(
            instructions="Use lookup, then answer.",
            tools=[lookup],
            model="gpt-5.2",
            provider=peer.provider(),
        )
        async with agent.session() as session:
            handle = await session.submit("look up once")
            events = handle.events()
            result = await (handle).get_result()
            assert result.final_output == "after required compaction"
            observed = [event async for event in events]
            assert any(isinstance(event, ContextCompacted) for event in observed)
            assert result.thread_usage is not None
            # gpt-5.2 has no catalog limit. Core still compacts with its
            # operating fallback without reporting that fallback as known.
            assert result.thread_usage.model_context_window is None
            assert result.thread_usage.total_token_usage.total_tokens >= 250_000
            saved = await session.save_state()
            assert any(record["type"] == "compacted" for record in saved.history)
        assert effects == ["called"]
        assert len(peer.requests) == 3
        assert peer.requests[1].get("tools", []) == []
        assert "Compacted summary: lookup returned short." in json.dumps(peer.requests[2]["input"])


async def test_provider_nonterminal_response_continues_the_same_public_turn() -> None:
    async with timeout(30), http_peer() as peer:

        async def continue_first_response(event: dict[str, Any]) -> None:
            if len(peer.requests) == 1 and event["type"] == "response.completed":
                event["response"]["end_turn"] = False

        peer.before_event = continue_first_response
        peer.respond(assistant_output("intermediate response"))
        peer.respond(assistant_output("final response"))
        agent = Agent(
            model="gpt-5.2", instructions="Complete the requested answer.", provider=peer.provider()
        )
        async with agent.session() as session:
            handle = await session.submit("answer in full")
            result = await (handle).get_result()
            assert result.final_output == "final response"
            assert result.turn is handle
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 28
            turns = [entry async for entry in session.turns()]
            assert len(turns) == 1
            assert turns[0]["id"] == handle.turn_id
            assert turns[0]["status"] == "completed"
        assert len(peer.requests) == 2
        assert "intermediate response" in json.dumps(peer.requests[1]["input"])


async def test_active_handoff_cancel_joins_core_then_same_session_resumes() -> None:
    from agents.observation import TurnFinished

    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def lookup() -> str:
        effects.append("once")
        return "short"

    async with timeout(30), http_peer() as peer:

        async def gate_core_compaction(event: dict[str, Any]) -> None:
            if len(peer.requests) == 1 and event["type"] == "response.completed":
                event["response"]["usage"] = {
                    "input_tokens": 249_999,
                    "output_tokens": 1,
                    "total_tokens": 250_000,
                }
            if len(peer.requests) == 2 and event["type"] == "response.created":
                entered.set()
                await release.wait()

        peer.before_event = gate_core_compaction
        peer.respond(function_call("lookup", {}, "handoff-call"))
        peer.respond(assistant_output("must not commit this compacted summary"))
        # Cancellation before a summary commits leaves the high usage intact;
        # the next turn must compact again before its regular model response.
        peer.respond(assistant_output("Retried summary: lookup returned short."))
        peer.respond(assistant_output("same session resumed"))
        agent = Agent(
            instructions="Use lookup once.",
            tools=[lookup],
            model="gpt-5.2",
            provider=peer.provider(),
        )
        try:
            async with agent.session() as session:
                session_id = session.id
                handle = await session.submit("look up once")
                events = handle.events()
                async for event in events:
                    if isinstance(event, ToolFinished):
                        assert event.status == "completed"
                        break
                await entered.wait()
                assert peer.requests[1].get("tools", []) == []
                await session._cancel(handle)
                await disconnected(peer.transports[1:2])
                with pytest.raises(TurnStoppedError):
                    await (handle).get_result()
                remaining = [event async for event in events]
                terminal = [event for event in remaining if isinstance(event, TurnFinished)]
                assert len(terminal) == 1
                assert terminal[0].status == "cancelled"
                saved = await session.save_state()
                outputs = [
                    row["payload"]
                    for row in saved.history
                    if row["type"] == "response_item"
                    and row["payload"].get("type") == "function_call_output"
                    and row["payload"].get("call_id") == "handoff-call"
                ]
                assert len(outputs) == 1
                assert "short" in json.dumps(outputs[0])
                release.set()
                result = await (await session.submit("continue after cancellation")).get_result()
                assert session.id == session_id
                assert result.final_output == "same session resumed"
        finally:
            release.set()
        assert effects == ["once"]
        assert len(peer.requests) == 4
        assert peer.requests[2].get("tools", []) == []
        assert "Retried summary: lookup returned short." in json.dumps(peer.requests[3]["input"])


async def test_parallel_tool_completion_keeps_declared_output_order() -> None:
    release_a, entered_a = asyncio.Event(), asyncio.Event()
    completed: list[str] = []

    @function_tool
    async def lookup(name: str) -> str:
        if name == "A":
            entered_a.set()
            await release_a.wait()
        completed.append(name)
        return "output-" + name

    async with timeout(30), http_peer() as peer:
        peer.respond(
            function_call("lookup", {"name": "A"}, "call-A"),
            function_call("lookup", {"name": "B"}, "call-B"),
        )
        peer.respond(assistant_output("ordered"))
        agent = Agent(
            model="gpt-5.2",
            instructions="Use both tools.",
            tools=[lookup],
            provider=peer.provider(),
        )
        try:
            async with agent.session() as session:
                handle = await session.submit("run both")
                async for event in handle.events():
                    if isinstance(event, ToolFinished) and event.call_id == "call-B":
                        assert event.status == "completed"
                        break
                await entered_a.wait()
                assert completed == ["B"]
                release_a.set()
                assert (await (handle).get_result()).final_output == "ordered"
                saved = await session.save_state()
        finally:
            release_a.set()
        assert completed == ["B", "A"]
        assert peer.requests[0]["model"] == "gpt-5.2"
        assert len(peer.requests) == 2
        sent_outputs = [
            item for item in peer.requests[1]["input"] if item.get("type") == "function_call_output"
        ]
        stored_outputs = [
            row["payload"]
            for row in saved.history
            if row["type"] == "response_item"
            and row["payload"].get("type") == "function_call_output"
        ]
        assert [item["call_id"] for item in sent_outputs] == ["call-A", "call-B"]
        assert [item["call_id"] for item in stored_outputs] == ["call-A", "call-B"]
        assert tool_output(peer.requests[1], "call-A") == "output-A"
        assert tool_output(peer.requests[1], "call-B") == "output-B"


async def test_cancel_preserves_later_accepted_tool_while_earlier_tool_is_held() -> None:
    release_a, joined_a, entered_a = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def lookup(name: str) -> str:
        if name == "A":
            entered_a.set()
            try:
                await release_a.wait()
            finally:
                joined_a.set()
        effects.append(name)
        return "output-" + name

    async with timeout(30), http_peer() as peer:
        peer.respond(
            function_call("lookup", {"name": "A"}, "call-A"),
            function_call("lookup", {"name": "B"}, "call-B"),
        )
        peer.respond(assistant_output("restored without replay"))
        agent = Agent(
            model="gpt-5.2",
            instructions="Use both tools.",
            tools=[lookup],
            provider=peer.provider(),
        )
        try:
            async with agent.session() as session:
                progress = session._native._manual_progress()
                handle = await session.submit("run both")
                async for event in handle.events():
                    if isinstance(event, ToolFinished) and event.call_id == "call-B":
                        assert event.status == "completed"
                        break
                await entered_a.wait()
                assert effects == ["B"]
                # Only B can reach Core's terminal tool outcome while A is held.
                await session._native._wait_manual_progress(progress)
                await session._cancel(handle)
                assert joined_a.is_set()
                with pytest.raises(TurnStoppedError):
                    await (handle).get_result()
                saved = await session.save_state()
                accepted = [
                    row["payload"]
                    for row in saved.history
                    if row["type"] == "response_item"
                    and row["payload"].get("type") == "function_call_output"
                    and row["payload"].get("call_id") == "call-B"
                ]
                assert len(accepted) == 1
                assert "output-B" in json.dumps(accepted[0])
                session_id = session.id
                result = await (await session.submit("continue live without replay")).get_result()
                assert result.final_output == "restored without replay"
                assert session.id == session_id
                assert_paired_continuation(
                    peer.requests[1],
                    {"call-A": "aborted", "call-B": "output-B"},
                    "continue live without replay",
                )
                assert effects == ["B"]
        finally:
            release_a.set()
        peer.respond(assistant_output("restored without replay"))
        async with agent.session(state=saved) as restored:
            result = await (
                await restored.submit("continue without repeating effects")
            ).get_result()
            assert result.final_output == "restored without replay"
        assert effects == ["B"]
        assert len(peer.requests) == 3
        assert_paired_continuation(
            peer.requests[2],
            {"call-A": "aborted", "call-B": "output-B"},
            "continue without repeating effects",
        )


async def test_large_text_completion_preserves_content_and_joins_provider_cleanup() -> None:
    chunk = 'plain "quotes" \\ slash\n\t café 漢字 🙂 end\n' * 2048
    expected = "BEGIN\n" + chunk * 16 + "END"
    assert 1024 * 1024 < len(expected.encode("utf-8")) < 2 * 1024 * 1024

    async with http_peer() as peer:
        provider = peer.provider()
        agent = Agent(model="gpt-5.2", instructions="Return the text exactly.", provider=provider)
        peer.respond(*text_response("large-text", "BEGIN\n", *([chunk] * 16), "END"))
        try:
            result = await agent.run("Return the complete large text.")
            assert result.final_output == expected
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.input_tokens == 11
            assert result.thread_usage.total_token_usage.output_tokens == 3
            assert result.thread_usage.total_token_usage.total_tokens == 14
            assert len(peer.requests) == peer.finished_bodies == 1
            assert peer.disconnected_bodies == 0
        finally:
            await provider.aclose()
        # Observe client retirement before the test server can close the socket.
        await disconnected(peer.transports)
        assert peer.transports
        assert all(transport.is_closing() for transport in peer.transports)


async def test_split_citations_and_trailing_blank_message_preserve_visible_text() -> None:
    async with timeout(30), http_peer() as peer:
        peer.respond(
            *text_response(
                "citation-message",
                "Visible<oai-",
                "mem-citation>hidden-source",
                "</oai-mem-citation>",
                " answer",
            ),
            assistant_output(" \n"),
        )
        agent = Agent(model="gpt-5.2", instructions="Answer.", provider=peer.provider())
        async with agent.session() as session:
            handle = await session.submit("answer with a citation")
            events = handle.events()
            result = await (handle).get_result()
            deltas = [event.delta async for event in events if isinstance(event, OutputTextDelta)]
            assert "".join(deltas) == "Visible answer"
            assert result.final_output == "Visible answer"
        assert len(peer.requests) == 1


async def test_partial_sse_eof_retries_with_accepted_tools_and_no_duplicate_usage() -> None:
    effects: list[str] = []

    @function_tool
    async def lookup() -> str:
        effects.append("once")
        return "accepted-once"

    async with timeout(30), http_peer() as peer:

        async def remove_first_completion(event: dict[str, Any]) -> None:
            if len(peer.requests) == 1 and event["type"] == "response.completed":
                # Core already accepted the completed call item. HTTP EOF
                # before the response terminal cannot erase its real effect.
                event["type"] = "response.in_progress"

        peer.before_event = remove_first_completion
        peer.respond(function_call("lookup", {}, "retry-call"))
        peer.respond(assistant_output("retry succeeded"))
        agent = Agent(
            model="gpt-5.2",
            instructions="Use lookup once.",
            tools=[lookup],
            provider=peer.provider(),
        )
        async with agent.session() as session:
            result = await (await session.submit("look up once")).get_result()
            assert result.final_output == "retry succeeded"
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 14
            saved = await session.save_state()
            outputs = [
                row["payload"]
                for row in saved.history
                if row["type"] == "response_item"
                and row["payload"].get("type") == "function_call_output"
            ]
            assert len(outputs) == 1
            assert outputs[0]["call_id"] == "retry-call"
        assert effects == ["once"]
        assert len(peer.requests) == 2
        assert peer.requests[0]["input"] != peer.requests[1]["input"]
        assert tool_output(peer.requests[1], "retry-call") == "accepted-once"


async def test_partial_sse_retry_rejects_replay_of_an_already_accepted_call() -> None:
    from agents import NativeError

    effects: list[str] = []

    @function_tool
    async def lookup() -> str:
        effects.append("once")
        return "accepted-once"

    async with timeout(30), http_peer() as peer:

        async def remove_first_completion(event: dict[str, Any]) -> None:
            if len(peer.requests) == 1 and event["type"] == "response.completed":
                event["type"] = "response.in_progress"

        peer.before_event = remove_first_completion
        peer.respond(function_call("lookup", {}, "retry-call"))
        peer.respond(function_call("lookup", {}, "retry-call"))
        peer.respond(assistant_output("must not grant replay"))
        agent = Agent(
            model="gpt-5.2",
            instructions="Use lookup once.",
            tools=[lookup],
            provider=peer.provider(),
        )
        async with agent.session() as session:
            with pytest.raises(NativeError, match="reused within this submission"):
                await (await session.submit("look up once")).get_result()
            assert effects == ["once"]
            assert len(peer.requests) == 2
            assert tool_output(peer.requests[1], "retry-call") == "accepted-once"
            saved = await session.save_state()
            results = [
                row["payload"]
                for row in saved.history
                if row["type"] == "response_item"
                and row["payload"].get("type") == "function_call_output"
            ]
            assert sum("accepted-once" in json.dumps(item) for item in results) == 1


async def test_content_filter_retry_preserves_guidance_and_already_streamed_tool_result() -> None:
    effects: list[str] = []

    @function_tool
    async def accepted_effect() -> str:
        effects.append("once")
        return "accepted before terminal filter"

    async with timeout(30), http_peer() as peer:

        async def reject_first_completion(event: dict[str, Any]) -> None:
            if len(peer.requests) == 1 and event["type"] == "response.completed":
                event["type"] = "response.incomplete"
                event["response"]["incomplete_details"] = {"reason": "content_filter"}
            if len(peer.requests) == 2 and event["type"] == "response.created":
                developer_items = [
                    item for item in peer.requests[1]["input"] if item.get("role") == "developer"
                ]
                assert json.dumps(developer_items).count("<content_filter_guidance>") == 1
                assert tool_output(peer.requests[1], "filtered-call") == (
                    "accepted before terminal filter"
                )

        peer.before_event = reject_first_completion
        peer.respond(function_call("accepted_effect", {}, "filtered-call"))
        peer.respond(assistant_output("safe retry"))
        agent = Agent(
            model="gpt-5.2",
            instructions="Preserve this application instruction.",
            tools=[accepted_effect],
            provider=peer.provider(),
        )
        async with agent.session() as session:
            result = await (await session.submit("answer safely")).get_result()
            assert result.final_output == "safe retry"
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 14
            saved = await session.save_state()
            guidance = [
                row["payload"]
                for row in saved.history
                if row["type"] == "response_item"
                and "<content_filter_guidance>" in json.dumps(row["payload"])
            ]
            assert len(guidance) == 1
        async with agent.session(state=saved) as restored:
            assert "<content_filter_guidance>" in (await restored.save_state()).to_json()
        different = Agent(
            model="gpt-5.2", instructions="Changed authority.", provider=peer.provider()
        )
        with pytest.raises(ValueError, match="instructions"):
            async with different.session(state=saved):
                pytest.fail("A mismatched application instruction passed checkpoint validation")
        assert effects == ["once"]
        assert len(peer.requests) == 2


@pytest.mark.parametrize("failure", ["exception", "timeout"])
async def test_failed_callback_next_live_submission_has_paired_aborted_output(failure: str) -> None:
    effects: list[str] = []
    joined = asyncio.Event()

    @function_tool(callback_timeout=0.01 if failure == "timeout" else None)
    async def lookup() -> str:
        effects.append("once")
        try:
            if failure == "exception":
                raise ValueError("expected callback failure")
            await asyncio.Future()
            raise AssertionError("unreachable")
        finally:
            joined.set()

    async with timeout(30), http_peer() as peer:
        peer.respond(function_call("lookup", {}, "failed-call"))
        peer.respond(assistant_output("continued after failed callback"))
        agent = Agent(
            model="gpt-5.2", instructions="Use lookup.", tools=[lookup], provider=peer.provider()
        )
        async with agent.session() as session:
            handle = await session.submit("invoke lookup")
            with pytest.raises(ValueError if failure == "exception" else TimeoutError):
                await (handle).get_result()
            assert joined.is_set()
            assert effects == ["once"]
            session_id = session.id
            result = await (await session.submit("continue after callback failure")).get_result()
            assert result.final_output == "continued after failed callback"
            assert session.id == session_id
            assert_paired_continuation(
                peer.requests[1], {"failed-call": "aborted"}, "continue after callback failure"
            )
        assert effects == ["once"]
        assert len(peer.requests) == 2


async def test_same_agent_model_switch_refreshes_catalog_metadata_and_closes_transport() -> None:
    async with timeout(30), http_peer() as peer, peer.provider() as provider:
        agent = Agent(model="gpt-5.2", instructions="Answer briefly.", provider=provider)
        roots = []
        for index, (model, context_window) in enumerate(
            [("gpt-5.2", None), ("gpt-5.5", 258_400), ("gpt-5.2", None)]
        ):
            agent = replace(agent, model=model)
            expected = f"model-result-{index}"
            peer.respond(assistant_output(expected))
            result = await agent.run(f"model-input-{index}")
            assert result.final_output == expected
            assert result.thread_usage is not None
            assert result.thread_usage.model_context_window == context_window
            assert result.thread_usage.total_token_usage.total_tokens == 14
            assert result.thread_usage.last_token_usage.total_tokens == 14
            roots.append(result.turn.session_id)
            request = peer.requests[index]
            assert request["model"] == model
            if model == "gpt-5.5":
                assert request["text"]["verbosity"] == "low"
            else:
                assert request.get("text") is None
            assert f"model-input-{index}" in json.dumps(request["input"])
            for earlier in range(index):
                assert f"model-input-{earlier}" not in json.dumps(request["input"])
        assert len(set(roots)) == 3
        assert len(peer.requests) == peer.finished_bodies == 3
        await provider.aclose()
        del agent
        gc.collect()
        await disconnected(peer.transports)


async def session_workspace(session: Session[Any, Any]) -> Path:
    saved = await session.save_state()
    assert saved.root_metadata is not None
    path = Path(saved.root_metadata["cwd"])
    assert path.is_dir()
    return path


@pytest.mark.parametrize("instructions", ["", "  A\n", "A"])
async def test_initial_instruction_receipt_restores_before_any_model_request(
    instructions: str,
) -> None:
    async with timeout(30), http_peer() as peer:
        agent = Agent(model="gpt-5.2", instructions=instructions, provider=peer.provider())
        async with agent.session() as session:
            saved = await session.save_state()
        raw = json.loads(saved.to_json())
        assert len(accepted_items(raw)) == 1
        assert peer.requests == []
        agent = replace(agent, instructions="different")
        with pytest.raises(ValueError, match="latest accepted instructions"):
            async with agent.session(state=saved):
                pytest.fail("Restore changed accepted application instructions")
        agent = replace(agent, instructions=instructions)
        initial_receipt = accepted_items(raw)[0]
        # Import the initial receipt before any model work, then round-trip
        # Core's real context without allocating another instruction revision.
        async with agent.session(state=saved) as idle_restore:
            saved = await idle_restore.save_state()
            restored_raw = json.loads(saved.to_json())
            restored_receipt = accepted_items(restored_raw)
            assert len(restored_receipt) == 1
            assert restored_receipt[0]["id"] == initial_receipt["id"]
            assert restored_receipt[0]["content"] == initial_receipt["content"]
        assert peer.requests == []
        peer.respond(assistant_output("restored initial authority"))
        async with agent.session(state=saved) as restored:
            assert (
                await (await restored.submit("first actual input")).get_result()
            ).final_output == "restored initial authority"
        assert application_texts(peer.requests[0]) == [instructions]


@pytest.mark.parametrize("after_turn", [False, True])
@pytest.mark.parametrize("replacement", ["B", ""])
async def test_instruction_update_promotes_with_current_history_and_settings(
    after_turn: bool, replacement: str
) -> None:
    async with timeout(30), http_peer() as peer:
        agent = Agent(
            model="gpt-5.2",
            instructions="A",
            model_settings={"reasoning_effort": "low"},
            provider=peer.provider(),
        )
        async with agent.session() as session:
            identity = session.id
            if after_turn:
                peer.respond(assistant_output("prior accepted output"))
                await (await session.submit("prior accepted input")).get_result()
            await session.update(instructions=replacement)
            assert session.id == identity
            saved = await session.save_state()
            assert len(accepted_items(json.loads(saved.to_json()))) == 2
            peer.respond(assistant_output("updated authority"))
            assert (
                await (await session.submit("new input")).get_result()
            ).final_output == "updated authority"
        assert application_texts(peer.requests[-1])[-1] == replacement
        assert application_texts(peer.requests[-1]).count(replacement) == 1
        assert peer.requests[-1]["reasoning"]["effort"] == "low"
        if after_turn:
            assert "prior accepted input" in json.dumps(peer.requests[-1]["input"])
            assert "prior accepted output" in json.dumps(peer.requests[-1]["input"])
        agent = replace(agent, instructions=replacement)
        async with agent.session(state=saved) as restored:
            assert restored.id == identity
            assert len(accepted_items(json.loads((await restored.save_state()).to_json()))) == 2


async def test_shared_provider_keeps_each_initial_instruction_receipt() -> None:
    async with timeout(30), http_peer() as peer:
        agent = Agent(model="gpt-5.2", instructions="first", provider=peer.provider())
        async with agent.session() as first:
            agent = replace(agent, instructions="second")
            async with agent.session() as second:
                assert await session_workspace(first) != await session_workspace(second)
                first_receipt = accepted_items(json.loads((await first.save_state()).to_json()))
                second_receipt = accepted_items(json.loads((await second.save_state()).to_json()))
                assert len(first_receipt) == len(second_receipt) == 1
                assert first_receipt[0]["id"] != second_receipt[0]["id"]
                for session, expected in ((second, "second"), (first, "first")):
                    peer.respond(assistant_output(expected))
                    assert (
                        await (await session.submit("current authority")).get_result()
                    ).final_output == expected
                    assert application_texts(peer.requests[-1]) == [expected]


@pytest.mark.parametrize("use_tool", [False, True], ids=["text", "function"])
async def test_default_model_uses_lite_wire_and_preserves_stream_usage_and_history(
    use_tool: bool,
) -> None:
    calls: list[str] = []

    @function_tool
    async def lookup() -> str:
        calls.append("once")
        return "lite accepted output"

    async with timeout(30), http_peer(expected_model="gpt-6.1-sol") as peer:
        if use_tool:
            peer.respond(function_call("lookup", {}, "lite-call"))
        peer.respond(*text_response("lite-answer", "visible ", "answer"))
        agent = Agent(
            instructions="Answer.", tools=[lookup] if use_tool else [], provider=peer.provider()
        )
        async with agent.session() as session:
            handle = await session.submit("lite ordinary input")
            stream = handle.events()
            result = await (handle).get_result()
            assert result.final_output == "visible answer"
            assert (
                "".join(
                    [event.delta async for event in stream if isinstance(event, OutputTextDelta)]
                )
                == "visible answer"
            )
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == (28 if use_tool else 14)
            saved = await session.save_state()
        assert calls == (["once"] if use_tool else [])
        assert len(peer.requests) == (2 if use_tool else 1)
        for request, headers in zip(peer.requests, peer.request_headers, strict=True):
            assert headers["x-openai-internal-codex-responses-lite"] == "true"
            assert request["reasoning"]["context"] == "all_turns"
            assert request["parallel_tool_calls"] is False
            assert "tools" not in request
            declarations = [
                item for item in request["input"] if item.get("type") == "additional_tools"
            ]
            assert len(declarations) == int(use_tool)
            if use_tool:
                assert request["input"][0] == declarations[0]
                namespace = declarations[0]["tools"]
                assert len(namespace) == 1
                assert namespace[0]["type"] == "namespace" and namespace[0]["name"] == "functions"
                assert [tool["name"] for tool in namespace[0]["tools"]] == ["lookup"]
        assert not any(
            row["type"] == "response_item" and row["payload"].get("type") == "additional_tools"
            for row in saved.history
        )
        if use_tool:
            assert tool_output(peer.requests[-1], "lite-call") == "lite accepted output"
            assert peer.requests[0]["input"][0]["id"] == peer.requests[1]["input"][0]["id"]
        peer.respond(assistant_output("restored lite answer"))
        async with agent.session(state=saved) as restored:
            assert (
                await (await restored.submit("resume lite")).get_result()
            ).final_output == "restored lite answer"
        assert "lite ordinary input" in json.dumps(peer.requests[-1]["input"])
        assert peer.request_headers[-1]["x-openai-internal-codex-responses-lite"] == "true"
        assert all(
            item.get("id") is None or item["id"].startswith("msg_")
            for request in peer.requests
            for item in request["input"]
            if item.get("type") == "message"
        )


async def test_lite_catalog_replacement_and_model_switch_keep_current_declarations() -> None:
    calls: list[str] = []

    @function_tool(name="lookup")
    async def original() -> str:
        calls.append("original")
        return "old receipt"

    @function_tool(name="lookup")
    async def replacement() -> str:
        calls.append("replacement")
        return "new receipt"

    async with timeout(30), http_peer(expected_model="gpt-6.1-sol") as peer:
        agent = Agent(instructions="Use lookup.", tools=[original], provider=peer.provider())
        async with agent.session() as session:
            for label in ("first", "second"):
                if label == "second":
                    with pytest.raises(ValueError, match="captured"):
                        await session.submit("unbound", tools=[replacement])
                peer.respond(function_call("lookup", {}, label))
                peer.respond(assistant_output(label))
                assert (await (await session.submit(label)).get_result()).final_output == label
            prefixes = [request["input"][0] for request in peer.requests]
            assert prefixes[0]["id"] == prefixes[1]["id"]
            assert prefixes[2]["id"] == prefixes[3]["id"]
            for prefix, request, headers in zip(
                prefixes, peer.requests, peer.request_headers, strict=True
            ):
                assert prefix["type"] == "additional_tools"
                assert prefix["role"] == "developer"
                assert "tools" not in request
                assert headers["x-openai-internal-codex-responses-lite"] == "true"
            # Compare visible declarations; Core owns opaque IDs across publications.
            assert prefixes[0]["tools"] == prefixes[2]["tools"]
            assert "agents-schema" not in json.dumps(prefixes)
            namespace = prefixes[0]["tools"]
            assert len(namespace) == 1
            assert namespace[0]["type"] == "namespace" and namespace[0]["name"] == "functions"
            assert [tool["name"] for tool in namespace[0]["tools"]] == ["lookup"]
            await session.update(model="gpt-5.2")
            peer.respond(assistant_output("standard request"))
            await (await session.submit("standard")).get_result()
            assert peer.requests[-1]["tools"] == namespace[0]["tools"]
            assert not any(
                item.get("type") == "additional_tools" for item in peer.requests[-1]["input"]
            )
            assert "x-openai-internal-codex-responses-lite" not in peer.request_headers[-1]
            await session.update(model="gpt-6.1-sol")
            peer.respond(assistant_output("lite again"))
            await (await session.submit("lite")).get_result()
            assert peer.requests[-1]["input"][0]["tools"] == prefixes[2]["tools"]
            assert peer.request_headers[-1]["x-openai-internal-codex-responses-lite"] == "true"
        assert calls == ["original", "original"]
        assert tool_output(peer.requests[1], "first") == "old receipt"
        assert tool_output(peer.requests[3], "second") == "old receipt"


async def test_provider_and_agent_changes_keep_live_sessions_isolated() -> None:
    async with timeout(30), http_peer() as peer, peer.provider() as provider:
        agent = Agent(
            model="gpt-5.2",
            instructions="original instruction",
            model_settings={"reasoning_effort": "low"},
            provider=provider,
        )
        first_owner = agent.session()
        first = await first_owner.__aenter__()
        assert first._home is not None
        try:
            original = await session_workspace(first)
            async with agent.session() as sibling:
                sibling_workspace = await session_workspace(sibling)
                assert sibling_workspace != original and sibling_workspace.is_dir()
                assert sibling.id != first.id
                assert sibling._home is not None
                agent = replace(agent, instructions="replacement instruction")
                agent = replace(agent, model="gpt-5.5")
                agent = replace(agent, model_settings={"reasoning_effort": "high"})
                async with agent.session() as replacement:
                    changed = await session_workspace(replacement)
                    assert changed != original
                    assert original.is_dir() and changed.is_dir()
                    for session, prompt, model, instruction, effort in (
                        (first, "first isolated input", "gpt-5.2", "original instruction", "low"),
                        (
                            replacement,
                            "replacement isolated input",
                            "gpt-5.5",
                            "replacement instruction",
                            "high",
                        ),
                        (
                            sibling,
                            "sibling isolated input",
                            "gpt-5.2",
                            "original instruction",
                            "low",
                        ),
                    ):
                        peer.respond(assistant_output(prompt))
                        result = await (await session.submit(prompt)).get_result()
                        assert result.final_output == prompt
                        assert result.thread_usage is not None
                        assert result.thread_usage.total_token_usage.total_tokens == 14
                        request = peer.requests[-1]
                        assert request["model"] == model
                        assert request["reasoning"]["effort"] == effort
                        assert instruction in json.dumps(request["input"])
                        for other in (
                            "first isolated input",
                            "replacement isolated input",
                            "sibling isolated input",
                        ):
                            assert (other in json.dumps(request["input"])) == (other == prompt)
                    assert original.is_dir()
                assert not changed.exists()
                assert sibling_workspace.is_dir()
            assert not sibling_workspace.exists()
            assert original.is_dir()
        finally:
            await first_owner.__aexit__(None, None, None)
        assert not original.exists()
        assert await result.turn.get_result() is result
        await provider.aclose()
        await disconnected(peer.transports)
        ref = weakref.ref(agent)
        del first, first_owner, sibling, replacement, session, result, agent
        gc.collect()
        assert ref() is None
        assert not changed.exists()


async def test_checkpoint_restores_after_source_session_home_is_deleted() -> None:
    async with timeout(30), http_peer() as peer:
        agent = Agent(
            model="gpt-5.2", instructions="restore this instruction", provider=peer.provider()
        )
        async with agent.session() as session:
            prepared = await session_workspace(session)
            peer.respond(assistant_output("accepted history"))
            result = await (await session.submit("original input")).get_result()
            assert result.final_output == "accepted history"
            saved = await session.save_state()
        source_id = session.id
        assert not prepared.exists()
        del session, agent
        gc.collect()
        restored_agent = Agent(
            model="gpt-5.2", instructions="restore this instruction", provider=peer.provider()
        )
        peer.respond(assistant_output("restored result"))
        async with restored_agent.session(state=saved) as restored:
            assert restored.id == source_id
            result = await (await restored.submit("continue restored history")).get_result()
            assert result.final_output == "restored result"
            request = peer.requests[-1]
            assert "original input" in json.dumps(request["input"])
            assert "accepted history" in json.dumps(request["input"])
            assert "continue restored history" in json.dumps(request["input"])
        assert len(peer.requests) == 2


async def test_cancelled_open_retires_late_core_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inspect the atomic Engine/Thread tuple before Session publication.
    monkeypatch.setattr("_agents_codex.agent._completed_run_eligible", lambda *args: False)
    native = importlib.import_module("agents._native")
    create = native._open_conversation
    entered = asyncio.Event()
    release = asyncio.Event()
    homes = []
    engines = []
    threads = []

    async def held_create(*args: Any, **kwargs: Any) -> Any:
        opened = await create(*args, **kwargs)
        engine, thread, home = opened
        assert (home is None) == engine._runtime_is_shared
        engines.append(engine)
        threads.append(thread)
        if home is not None:
            homes.append(Path(home.name))
        entered.set()
        await release.wait()
        return opened

    async with timeout(30), http_peer() as peer:
        agent = Agent(model="gpt-5.2", instructions="cancel acquisition", provider=peer.provider())
        monkeypatch.setattr(native, "_open_conversation", held_create)
        opening = asyncio.create_task(agent.run("must never reach provider"))
        try:
            await entered.wait()
            assert len(engines) == len(threads) == 1
            assert all(home.is_dir() for home in homes)
            opening.cancel("cancel opening")
            await asyncio.sleep(0)
            assert not opening.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await opening
        finally:
            release.set()
            await asyncio.gather(opening, return_exceptions=True)
        with pytest.raises(RuntimeError, match="closed"):
            await engines[0].start_thread()
        with pytest.raises(RuntimeError, match="closed"):
            await threads[0].read_goal()
        assert all(not home.exists() for home in homes)
        assert peer.requests == []
        monkeypatch.setattr(native, "_open_conversation", create)
        peer.respond(assistant_output("later acquisition succeeded"))
        assert (await agent.run("try again")).final_output == "later acquisition succeeded"
        await agent.provider.aclose()
        assert agent.provider._closed and agent.provider._transport is None


async def test_core_workspaces_and_instruction_updates_stay_session_owned() -> None:
    async with timeout(30), http_peer() as peer:
        agent = Agent(model="gpt-5.2", instructions="A", provider=peer.provider())
        for index in range(3):
            peer.respond(assistant_output(f"result-{index}"))
            assert (await agent.run(f"input-{index}")).final_output == f"result-{index}"
        async with agent.session() as session, agent.session() as sibling:
            original_workspace = await session_workspace(session)
            sibling_workspace = await session_workspace(sibling)
            assert original_workspace != sibling_workspace
            assert original_workspace.is_dir() and sibling_workspace.is_dir()
            identity = session.id
            await session.update(instructions="B")
            assert session.id == identity
            assert await session_workspace(session) == original_workspace
            assert await session_workspace(sibling) == sibling_workspace
            peer.respond(assistant_output("independent sibling"))
            assert (
                await (await sibling.submit("separate authority")).get_result()
            ).final_output == "independent sibling"
            assert application_texts(peer.requests[-1]) == ["A"]
        assert not original_workspace.exists() and not sibling_workspace.exists()
        await agent.provider.aclose()


@pytest.mark.parametrize("selection", ["extra_config", "sqlite_override"])
async def test_current_config_uses_a_real_isolated_core_home(
    selection: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = importlib.import_module("agents._native")
    create = native.create_engine
    ambient = tmp_path / "ambient-home"
    monkeypatch.setenv("CODEX_HOME", str(ambient))
    if selection == "sqlite_override":
        monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "sqlite"))
    captured = []

    async def check_config(config: str, runtime: str, *args: Any, **kwargs: Any) -> Any:
        captured.append(json.loads(runtime))
        if selection == "extra_config":
            value = json.loads(config)
            value["features.memory_tool"] = False
            config = json.dumps(value)
        return await create(config, runtime, *args, **kwargs)

    monkeypatch.setattr(native, "create_engine", check_config)
    async with timeout(30), http_peer() as peer:
        agent = Agent(model="gpt-5.2", instructions="A", provider=peer.provider())
        async with agent.session() as session:
            workspace = await session_workspace(session)
            assert workspace.is_dir() and workspace.name == "workspace"
            assert workspace.parent != ambient
            assert session._home is not None and Path(session._home.name) == workspace.parent
        assert len(captured) == 1
        assert Path(captured[0]["codex_home"]) == workspace.parent
        assert not workspace.parent.exists() and not ambient.exists()
        assert peer.requests == []


async def test_cancelled_core_restore_retries_orphan_cleanup_and_releases_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    native = importlib.import_module("agents._native")
    create = native.create_checkpoint_engine
    entered, release = asyncio.Event(), asyncio.Event()
    engines: list[Any] = []
    homes: list[Path] = []
    backups: list[Path] = []

    async def held_create(*args: Any, **kwargs: Any) -> Any:
        engine = await create(*args, **kwargs)
        engines.append(engine)
        home = Path(json.loads(args[1])["codex_home"])
        backup = home.with_name(home.name + "-held")
        home.rename(backup)
        home.write_text("block directory cleanup")
        homes.append(home)
        backups.append(backup)
        entered.set()
        await release.wait()
        return engine

    async with timeout(30), http_peer() as peer:
        agent = Agent(model="gpt-5.2", instructions="A", provider=peer.provider())
        async with agent.session() as original:
            saved = await original.save_state()
        monkeypatch.setattr(native, "create_checkpoint_engine", held_create)
        context = agent.session(state=saved)
        opening = asyncio.create_task(context.__aenter__())
        try:
            await entered.wait()
            assert homes[0].is_file() and backups[0].is_dir()
            opening.cancel()
            await asyncio.sleep(0)
            assert not opening.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await opening
            with pytest.raises(RuntimeError, match="directory"):
                await agent.provider.aclose()
            with pytest.raises(RuntimeError, match="closed"):
                await engines[0].start_thread()
            assert peer.requests == []
        finally:
            release.set()
            await asyncio.gather(opening, return_exceptions=True)
            if homes and homes[0].is_file():
                homes[0].unlink()
                backups[0].rename(homes[0])
        await agent.provider.aclose()
        assert not homes[0].exists()


async def test_core_home_cleanup_can_be_retried() -> None:
    async with timeout(30), http_peer() as peer:
        async with Agent(
            model="gpt-5.2", instructions="A", provider=peer.provider()
        ).session() as session:
            await session.update(instructions="B")
            assert session._home is not None
            home = Path(session._home.name)
            cleanup = session._home.cleanup
            failures = 0

            def fail_twice() -> None:
                nonlocal failures
                if failures < 2:
                    failures += 1
                    raise OSError("retryable Core home cleanup")
                cleanup()

            class FailingHome:
                name = str(home)

                def cleanup(self) -> None:
                    fail_twice()

            session._home = FailingHome()
            with pytest.raises(OSError, match="retryable Core home cleanup"):
                await session._aclose()
            assert home.is_dir() and not session._closed
            with pytest.raises(OSError, match="retryable Core home cleanup"):
                await session._aclose()
            assert home.is_dir() and not session._closed
            await session._aclose()
            assert session._closed and not home.exists()
            assert peer.requests == []


@pytest.mark.parametrize("model", ["gpt-5.2", None], ids=["responses", "default-lite"])
async def test_ordinary_function_schema_subset_still_rejects_invalid_arguments(
    model: str | None,
) -> None:
    effects: list[str] = []

    @function_tool(description="Read one record.")
    async def lookup(
        key: Annotated[str, Field(description="Three-character record key", min_length=3)],
    ) -> str:
        effects.append(key)
        return "found:" + key

    expected = {
        "type": "function",
        "name": "lookup",
        "description": "Read one record.",
        "strict": False,
        "parameters": {
            "additionalProperties": False,
            "properties": {
                "key": {
                    "description": "Three-character record key",
                    "type": "string",
                }
            },
            "required": ["key"],
            "type": "object",
        },
    }
    async with timeout(30), http_peer(expected_model=model or "gpt-6.1-sol") as peer:
        agent = Agent(
            model=model,
            instructions="Read the requested record.",
            tools=[lookup],
            provider=peer.provider(),
        )
        async with agent.session() as session:
            peer.respond(function_call("lookup", {"key": "x"}, "short-key"))
            with pytest.raises(ValidationError, match="at least 3 characters"):
                await (await session.submit("Read a short key.")).get_result()
            assert effects == []
            assert len(peer.requests) == 1

            # Queue recovery only after the failed turn has settled.
            peer.respond(function_call("lookup", {"key": "record-a"}, "valid-key"))
            peer.respond(assistant_output("record found"))
            result = await (await session.submit("Read record-a.")).get_result()
            assert result.final_output == "record found"
            assert effects == ["record-a"]
            assert tool_output(peer.requests[-1], "valid-key") == "found:record-a"

        assert len(peer.requests) == 3
        # Validate declarations both after recovery and in a new Session.
        ordinary_requests = list(peer.requests)
        peer.respond(function_call("lookup", {"key": "record-b"}, "ordinary-key"))
        peer.respond(assistant_output("ordinary record found"))
        async with agent.session() as session:
            result = await (await session.submit("Read record-b.")).get_result()
            assert result.final_output == "ordinary record found"
        assert effects == ["record-a", "record-b"]
        assert len(peer.requests) == 5
        assert tool_output(peer.requests[-1], "ordinary-key") == "found:record-b"
        ordinary_requests.extend(peer.requests[3:])
        for request in ordinary_requests:
            if model is None:
                assert "tools" not in request
                [declaration] = [
                    item for item in request["input"] if item.get("type") == "additional_tools"
                ]
                assert declaration["role"] == "developer"
                assert declaration["tools"] == [
                    {
                        "type": "namespace",
                        "name": "functions",
                        "description": "",
                        "tools": [expected],
                    }
                ]
            else:
                assert request["tools"] == [expected]
