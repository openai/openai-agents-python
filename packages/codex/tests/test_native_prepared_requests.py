"""Prepared sampling copies preserve native execution and operation ownership."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Event as ThreadEvent
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, ValidationError

from agents import (
    Agent,
    CapabilityError,
    FunctionTool,
    HostedVerificationMCP,
    HttpMCP,
    InteractionRequired,
    NativeError,
    NativeOutput,
    NativeProvider,
    PreparedRequest,
    PromptedOutput,
    ReplaceArguments,
    ToolContext,
    ToolOutput,
    TurnStoppedError,
    WebSearch,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


@dataclass
class EnumPeer(Peer):
    business_effects: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request):
        body = await request.json()
        response = await super().handle(request)
        if body.get("method") == "tools/list":
            payload = json.loads(response.body)
            for tool in payload["result"]["tools"]:
                tool["inputSchema"]["properties"]["key"]["enum"] = ["one", "43"]
            return web.json_response(payload)
        if body.get("method") == "tools/call":
            arguments = body["params"]["arguments"]
            if arguments["key"] in {"one", "43"}:
                self.business_effects.append(arguments)
            else:
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "content": [{"type": "text", "text": "invalid enum"}],
                            "isError": True,
                        },
                    }
                )
        return response


def note(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def wire_instructions(request: dict[str, Any]) -> list[str]:
    return [
        part["text"]
        for item in request["input"]
        if item.get("type") == "message" and item.get("role") == "developer"
        for part in item.get("content", [])
        if part.get("type") == "input_text"
    ]


@pytest.mark.parametrize("entry", ["session", "agent_run"])
async def test_combined_edits_keep_function_and_mcp_effect_owners_and_idle_restore(
    responses: ResponsesFixture,
    entry: str,
) -> None:
    effects = []
    policies = []
    snapshots = []
    peer = EnumPeer(names=("search",))

    def before(ctx: ToolContext[None]):
        policies.append(("before", ctx.call_id, dict(ctx.original_arguments)))
        return ReplaceArguments({"kind": "one", "value": "corrected"})

    def after(ctx: ToolContext[None], value: ToolOutput):
        policies.append(("after", ctx.call_id, dict(ctx.effective_arguments)))
        return ToolOutput.text("SHAPED_RESULT")

    @function_tool(before=before, after=after)
    def reserve(kind: Literal["one", "valid"], value: str) -> str:
        effects.append((kind, value))
        return "raw result"

    def approve(ctx, call):
        arguments = json.loads(call.input)
        policies.append(("mcp-approval", call.call_id, arguments))
        return arguments["key"] in {"43"}

    async def edit(turn, request: PreparedRequest, *, preparation_index, recovery_attempt):
        snapshots.append((turn, preparation_index, recovery_attempt, deepcopy(request)))
        if any(
            item.get("type") == "function_call_output" and item.get("call_id") == "fn-1"
            for item in request["input"]
        ):
            request["instructions"] = "sampling instructions"
            request["input"].append(note("SAMPLING_ONLY_NOTE"))
            for tool in request["tools"]:
                tool["description"] = "sampling description"
                key = "kind" if tool["name"] == "reserve" else "key"
                tool["parameters"]["properties"][key]["enum"] = ["one", "valid", "43", "two"]
            # Application-retained objects may change as soon as the callback
            # returns; the captured bytes must already be independent.
            asyncio.get_running_loop().call_soon(
                lambda: request.update(instructions="late application mutation")
            )
            return request
        return None

    responses.respond(function_call("reserve", {"kind": "one", "value": "original"}, "fn-1"))
    responses.respond(mcp_call("search", "mcp__records", "mcp-1"))
    responses.respond(assistant_output("complete"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="canonical instructions",
            tools=[
                reserve,
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    approval_mode="preapproved",
                    approval=approve,
                ),
            ],
            model="gpt-5.5",
            provider=provider(responses),
        )
        if entry == "agent_run":
            result = await agent.run("Use both", prepare_request=edit, return_state=True)
            assert result.final_output == "complete" and result.state is not None
            turn, saved = result.turn, result.state
        else:
            async with agent.session() as session:
                turn = await session.submit("Use both", prepare_request=edit)
                assert (await turn.get_result()).final_output == "complete"
                saved = await session.save_state()
        assert effects == [("one", "corrected")]
        assert len(peer.calls) == 1
        assert peer.business_effects == [{"key": "43"}]
        assert [entry[0] for entry in policies] == ["before", "after", "mcp-approval"]
        assert all(owner is turn for owner, *_ in snapshots)
        assert [(index, recovery) for _, index, recovery, _ in snapshots] == [
            (0, 0),
            (1, 0),
            (2, 0),
        ]
        assert tool_output(responses.requests[1], "fn-1") == "SHAPED_RESULT"
        for captured in responses.requests[1:]:
            assert "sampling instructions" in wire_instructions(captured)
            assert "late application mutation" not in wire_instructions(captured)
            assert any("SAMPLING_ONLY_NOTE" in json.dumps(item) for item in captured["input"])
            assert {tool["description"] for tool in declarations(captured).values()} == {
                "sampling description"
            }
            assert "previous_response_id" not in captured
        assert policies == [
            ("before", "fn-1", {"kind": "one", "value": "original"}),
            ("after", "fn-1", {"kind": "one", "value": "corrected"}),
            ("mcp-approval", "mcp-1", {"key": "43"}),
        ]
        assert peer.label in tool_output(responses.requests[2], "mcp-1")
        assert "SAMPLING_ONLY_NOTE" not in json.dumps(saved.history)
        assert reserve.input_type.model_json_schema()["properties"]["kind"]["enum"] == [
            "one",
            "valid",
        ]
        responses.respond(assistant_output("restored"))
        seen = []

        def restored(turn, request, **indices):
            seen.append(request)
            return None

        async with agent.session(state=saved) as session:
            assert (
                await (await session.submit("Continue", prepare_request=restored)).get_result()
            ).final_output == "restored"
            assert "SAMPLING_ONLY_NOTE" not in json.dumps(seen)


@pytest.mark.parametrize("target", ["function", "mcp"])
async def test_sampled_enum_never_grants_argument_authority(
    responses: ResponsesFixture,
    target: str,
) -> None:
    effects = []
    peer = EnumPeer(names=("search",))

    @function_tool
    def reserve(kind: Literal["one", "valid"]) -> str:
        effects.append(kind)
        return kind

    def approve(ctx, call):
        return json.loads(call.input)["key"] in {"one"}

    def widen(turn, request, **indices):
        for tool in request["tools"]:
            field = "kind" if tool["name"] == "reserve" else "key"
            tool["parameters"]["properties"][field]["enum"] = ["one", "valid", "43", "two"]
        return request

    event = function_call("reserve", {"kind": "two"}, "invalid")
    if target == "mcp":
        event = mcp_call("search", "mcp__records", "invalid")
        event["item"]["arguments"] = '{"key":"two"}'
    responses.respond(event)
    responses.respond(assistant_output("denied"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Use declared tools",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[
                reserve,
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    approval_mode="preapproved",
                    approval=approve,
                ),
            ],
        )
        if target == "function":
            with pytest.raises(ValidationError):
                await agent.run("Try", prepare_request=widen)
        else:
            with pytest.raises(NativeError, match="Native tool interception"):
                await agent.run("Try", prepare_request=widen)
            assert len(responses.requests) == 1
        assert not effects and not peer.calls
        # MCP's zero-RPC result comes from explicit application approval,
        # rather than an SDK-provided MCP JSON Schema validator.


@pytest.mark.parametrize("mutation", ["throw", "extra", "rename", "schema", "input"])
async def test_invalid_preparation_never_dispatches(
    responses: ResponsesFixture, mutation: str
) -> None:
    @function_tool
    def reserve(kind: str) -> str:
        pytest.fail("invalid edit dispatched a tool")

    def edit(turn, request, **indices):
        if mutation == "throw":
            raise LookupError("application preparation failed")
        if mutation == "extra":
            request["credentials"] = "synthetic unsupported value"
        elif mutation == "rename":
            request["tools"][0]["name"] = "invented"
        elif mutation == "schema":
            request["tools"][0]["parameters"]["maximum"] = 99
        else:
            request["input"][0]["unknown"] = None
        return request

    async with timeout(15):
        with pytest.raises(LookupError if mutation == "throw" else NativeError):
            await Agent(
                model="gpt-5.5",
                instructions="No effect",
                tools=[reserve],
                provider=provider(responses),
            ).run(
                "Try",
                prepare_request=edit,
            )
        assert not responses.requests


async def test_cancellation_joins_ignoring_callback_before_later_turn(
    responses: ResponsesFixture,
) -> None:
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    joined = asyncio.Event()

    async def hold(turn, request, **indices):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        finally:
            joined.set()
        return request

    async with (
        timeout(20),
        Agent(
            model="gpt-5.5", instructions="No old dispatch", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Hold", prepare_request=hold)
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel())
        await cancelled.wait()
        assert not stopping.done() and not joined.is_set() and not responses.requests
        release.set()
        await stopping
        assert joined.is_set() and not responses.requests
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        responses.respond(assistant_output("later"))
        assert (await (await session.submit("Later")).get_result()).final_output == "later"


async def test_retired_reply_cannot_consume_later_nonce(responses: ResponsesFixture) -> None:
    ids = []
    entered, release = asyncio.Event(), asyncio.Event()
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    async with (
        timeout(20),
        Agent(
            model="gpt-5.5", instructions="Answer", provider=provider(responses)
        ).session() as session,
    ):
        native = session._native

        class CaptureIds:
            def __getattr__(self, name):
                return getattr(native, name)

            def _reply_prepared_request(self, request_id, data_json=None, error=False):
                ids.append(request_id)
                return native._reply_prepared_request(request_id, data_json, error)

        session._native = CaptureIds()
        first = await session.submit("First", prepare_request=lambda turn, request, **indices: None)
        await first.get_result()

        async def hold(turn, request, **indices):
            entered.set()
            await release.wait()
            return None

        second = await session.submit("Second", prepare_request=hold)
        await entered.wait()
        assert native._reply_prepared_request(ids[0], None, False) is False
        release.set()
        assert (await second.get_result()).final_output == "second"
        assert ids[0] != ids[1]


async def test_manual_operation_refuses_readiness_and_export_but_continues(
    responses: ResponsesFixture,
) -> None:
    manual = FunctionTool(
        name="lookup", description="Lookup", input_type=LookupArgs, output_type=str
    )
    indices = []

    def edit(turn, request, *, preparation_index, recovery_attempt):
        indices.append(preparation_index)
        return None

    responses.respond(function_call("lookup", {"key": "one"}, "manual"))
    responses.respond(assistant_output("complete"))
    async with (
        timeout(20),
        Agent(
            model="gpt-5.5", instructions="Use lookup", tools=[manual], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Read", prepare_request=edit)
        stream = session.interactions()
        request = await next_request(stream, manual)
        assert (await session.checkpoint_status(kind="manual_wait")).status == "not_ready"
        with pytest.raises(CapabilityError, match="prepare_request"):
            await session.suspend()
        with pytest.raises(CapabilityError, match="prepare_request"):
            await session.save_state()
        await request.respond("manual result")
        assert (await turn.get_result()).final_output == "complete"
        assert indices == [0, 1]
        await stream.aclose()
        assert await session.save_state()


async def test_stream_recovery_prepares_again(responses: ResponsesFixture) -> None:
    indices = []

    def edit(turn, request, *, preparation_index, recovery_attempt):
        indices.append((preparation_index, recovery_attempt))
        return request

    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "type": "server_error",
                    "code": "server_error",
                    "message": "retry synthetic stream",
                }
            },
        }
    )
    responses.respond(assistant_output("recovered"))
    async with timeout(30):
        result = await Agent(
            model="gpt-5.5", instructions="Answer", provider=provider(responses)
        ).run("Try", prepare_request=edit)
        assert result.final_output == "recovered"
        assert indices == [(0, 0), (1, 1)]
        assert len(responses.requests) == 2


async def test_http_retry_reuses_frozen_preparation(responses: ResponsesFixture) -> None:
    attempts = []
    preparations = []

    async def retry_once(request):
        body = await request.json()
        attempts.append(body)
        if len(attempts) == 1:
            return web.Response(status=503)
        return await responses.handle(request)

    def edit(turn, request, *, preparation_index, recovery_attempt):
        preparations.append((preparation_index, recovery_attempt))
        request["instructions"] = "frozen retry instructions"
        return request

    app = web.Application()
    app.router.add_post("/v1/responses", retry_once)
    responses.respond(assistant_output("retried"))
    async with timeout(30), TestServer(app) as server:
        agent = Agent(
            model="gpt-5.5",
            instructions="Answer",
            provider=NativeProvider(
                base_url=str(server.make_url("/v1")),
                api_key="synthetic-retry",
            ),
        )
        assert (await agent.run("Try", prepare_request=edit)).final_output == "retried"
        assert preparations == [(0, 0)] and len(attempts) == 2
        assert "frozen retry instructions" in wire_instructions(attempts[1])
        for field in ("input", "tools"):
            assert attempts[0][field] == attempts[1][field]
        assert all("previous_response_id" not in request for request in attempts)


@pytest.mark.parametrize("boundary", ["pre_turn", "after_tool"])
@pytest.mark.parametrize("enabled", [False, True], ids=["omitted_control", "editor"])
async def test_automatic_compaction_refuses_before_hidden_dispatch(
    responses: ResponsesFixture,
    boundary: str,
    enabled: bool,
) -> None:
    effects = []
    preparations = []
    observed = []

    @function_tool
    def effect() -> str:
        effects.append("completed once")
        return "tool result"

    def edit(turn, request, *, preparation_index, recovery_attempt):
        preparations.append((preparation_index, recovery_attempt))
        return None

    async def large_usage(event):
        if event["type"] == "response.completed" and len(responses.requests) == 1:
            # The baseline model context is 272,000 tokens. Reported usage,
            # rather than a huge synthetic input, crosses its actual threshold.
            event["response"]["usage"].update(input_tokens=1_000_000, total_tokens=1_000_003)

    responses.before_event = large_usage
    responses.respond(
        assistant_output("seed")
        if boundary == "pre_turn"
        else function_call("effect", {}, "effect-1")
    )
    responses.respond(assistant_output("synthetic compaction summary"))
    responses.respond(assistant_output("complete"))
    async with (
        timeout(30),
        Agent(
            instructions="Use the original effect",
            tools=[effect],
            model="gpt-5.5",
            provider=provider(responses),
        ).session() as session,
    ):
        native = session._native

        class Observed:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _next_session_event(self):
                raw = await native._next_session_event()
                if raw is not None:
                    event = json.loads(raw)
                    if event is not None:
                        observed.append(event)
                return raw

        session._native = Observed()
        if boundary == "pre_turn":
            assert (await (await session.submit("Seed usage")).get_result()).final_output == "seed"
            observed.clear()
        turn = await session.submit("Continue", prepare_request=edit if enabled else None)
        if enabled:
            with pytest.raises(NativeError, match="automatic compaction"):
                await turn.get_result()
            assert len(responses.requests) == 1
            assert preparations == ([] if boundary == "pre_turn" else [(0, 0)])
            assert len([event for event in observed if event["msg"]["type"] == "error"]) == 1
        else:
            assert (await turn.get_result()).final_output == "complete"
            assert len(responses.requests) == 3
            assert not preparations
        assert effects == ([] if boundary == "pre_turn" else ["completed once"])


@pytest.mark.parametrize("enabled", [False, True], ids=["omitted_control", "editor"])
async def test_post_turn_compaction_preserves_completed_answer(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    from agents import _native

    create_engine = _native.create_engine
    open_conversation = _native._open_conversation
    configured_paths = []

    def configured_json(config_json):
        # The facade does not expose compaction thresholds. Keep the actual
        # engine and public operation path, configuring only this test's budget.
        config = json.loads(config_json)
        config.update(
            model_context_window=200_000,
            model_auto_compact_token_limit=10_000,
            model_post_turn_compact_threshold_percent=1,
        )
        return json.dumps(config)

    async def configured_engine(config_json, *args, **kwargs):
        configured_paths.append("ordinary")
        return await create_engine(configured_json(config_json), *args, **kwargs)

    async def configured_conversation(config_json, *args, **kwargs):
        configured_paths.append("atomic")
        return await open_conversation(configured_json(config_json), *args, **kwargs)

    monkeypatch.setattr(_native, "create_engine", configured_engine)
    monkeypatch.setattr(_native, "_open_conversation", configured_conversation)
    preparations = []

    def edit(turn, request, *, preparation_index, recovery_attempt):
        preparations.append((preparation_index, recovery_attempt))
        return None

    async def completed_usage(event):
        if event["type"] == "response.completed" and len(responses.requests) == 1:
            event["response"]["usage"].update(
                input_tokens=12_000, output_tokens=1, total_tokens=12_001
            )

    responses.before_event = completed_usage
    responses.respond(assistant_output("completed answer"))
    responses.respond(assistant_output("synthetic compaction summary"))
    async with timeout(30), provider(responses) as current_provider:
        result = await Agent(model="gpt-5.5", instructions="Answer", provider=current_provider).run(
            "Complete", prepare_request=edit if enabled else None
        )
    assert configured_paths == ["ordinary" if enabled else "atomic"]
    assert result.final_output == "completed answer"
    assert len(responses.requests) == (1 if enabled else 2)
    assert preparations == ([(0, 0)] if enabled else [])


async def test_mcp_without_application_policy_reaches_server_validation(
    responses: ResponsesFixture,
) -> None:
    peer = EnumPeer(names=("search",))

    def widen(turn, request, **indices):
        request["tools"][0]["parameters"]["properties"]["key"]["enum"].append("two")
        return request

    event = mcp_call("search", "mcp__records", "server-denied")
    event["item"]["arguments"] = '{"key":"two"}'
    responses.respond(event)
    responses.respond(assistant_output("server denied"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Use records",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
        )
        assert (await agent.run("Try", prepare_request=widen)).final_output == "server denied"
        assert len(peer.calls) == 1 and peer.business_effects == []


@pytest.mark.parametrize("action", ["cancel", "close"])
async def test_sync_preparation_joins_executor_before_cancel_or_close_returns(
    responses: ResponsesFixture,
    action: str,
) -> None:
    loop = asyncio.get_running_loop()
    entered, interrupted = asyncio.Event(), asyncio.Event()
    release, finished = ThreadEvent(), ThreadEvent()

    class LateResult(dict):
        def items(self):
            pytest.fail("a retired synchronous callback result was serialized")

    def hold(turn, request, **indices):
        loop.call_soon_threadsafe(entered.set)
        try:
            release.wait()
            return LateResult(instructions="retired value", input=[], tools=[])
        finally:
            finished.set()

    async with (
        timeout(20),
        Agent(
            model="gpt-5.5",
            instructions="Join the actual worker",
            provider=provider(responses),
        ).session() as session,
    ):
        native = session._native

        class ObserveInterruption:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _interrupt_session(self, *args, **kwargs):
                result = await native._interrupt_session(*args, **kwargs)
                interrupted.set()
                return result

            async def _interrupt_graph(self, *args, **kwargs):
                result = await native._interrupt_graph(*args, **kwargs)
                interrupted.set()
                return result

        session._native = ObserveInterruption()
        turn = await session.submit("Hold the worker", prepare_request=hold)
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel() if action == "cancel" else session._aclose())
        try:
            # A real native interruption has settled, while the application
            # worker still cannot finish. This needs no elapsed-time oracle.
            await interrupted.wait()
            assert not stopping.done() and not finished.is_set() and not responses.requests
        finally:
            release.set()
        await stopping
        assert finished.is_set() and not responses.requests
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        if action == "cancel":
            responses.respond(assistant_output("successor"))
            assert (await (await session.submit("Later")).get_result()).final_output == "successor"


async def test_editor_cannot_restore_a_tool_removed_by_step_selection(
    responses: ResponsesFixture,
) -> None:
    effects = []
    saved = []

    @function_tool
    def visible() -> str:
        effects.append("visible")
        return "visible"

    @function_tool
    def hidden() -> str:
        effects.append("hidden")
        return "hidden"

    def capture(turn, request, **indices):
        saved.extend(deepcopy(tool) for tool in request["tools"] if tool["name"] == "hidden")
        return None

    def restore(turn, request, **indices):
        assert [tool["name"] for tool in request["tools"]] == ["visible"]
        request["tools"].append(saved[0])
        return request

    responses.respond(assistant_output("captured"))
    async with (
        timeout(20),
        Agent(
            model="gpt-5.5",
            instructions="Select only current tools",
            tools=[visible, hidden],
            provider=provider(responses),
        ).session() as session,
    ):
        await (await session.submit("Capture", prepare_request=capture)).get_result()
        assert len(saved) == 1
        turn = await session.submit(
            "Restore excluded",
            before_step=lambda turn, **indices: [visible],
            prepare_request=restore,
        )
        with pytest.raises(NativeError):
            await turn.get_result()
        assert len(responses.requests) == 1 and not effects


async def test_provider_invented_omitted_identity_has_no_handler_with_editor(
    responses: ResponsesFixture,
) -> None:
    effects = []
    prepared = []

    @function_tool
    def visible() -> str:
        effects.append("visible")
        return "visible"

    @function_tool
    def hidden() -> str:
        effects.append("hidden")
        return "hidden"

    def edit(turn, request, **indices):
        prepared.append(deepcopy(request))
        return request

    responses.respond(function_call("hidden", {}, "invented-omitted"))
    responses.respond(assistant_output("denied"))
    async with (
        timeout(20),
        Agent(
            model="gpt-5.5",
            instructions="Use the selected roster",
            tools=[visible, hidden],
            provider=provider(responses),
        ).session() as session,
    ):
        turn = await session.submit(
            "Try omitted identity",
            before_step=lambda turn, **indices: [visible],
            prepare_request=edit,
        )
        try:
            assert (await turn.get_result()).final_output == "denied"
        except NativeError:
            # Both terminal refusal and a model-visible unknown-tool reply are
            # native outcomes; neither grants a handler to the omitted identity.
            pass
        assert prepared and responses.requests and not effects
        assert set(declarations(responses.requests[0])) == {("functions", "visible")}


async def test_steering_during_tool_wait_reaches_next_owned_preparation(
    responses: ResponsesFixture,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects, prepared = [], []

    @function_tool
    async def hold() -> str:
        entered.set()
        await release.wait()
        effects.append("one effect")
        return "released"

    def edit(turn, request, *, preparation_index, recovery_attempt):
        prepared.append((turn, preparation_index, deepcopy(request)))
        return None

    responses.respond(function_call("hold", {}, "held-effect"))
    responses.respond(assistant_output("steered"))
    async with (
        timeout(20),
        Agent(
            model="gpt-5.5",
            instructions="Keep the original owner",
            tools=[hold],
            provider=provider(responses),
        ).session() as session,
    ):
        turn = await session.submit("Hold", prepare_request=edit)
        await entered.wait()
        try:
            await turn.steer("STEERING_NOTE")
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "steered"
        assert effects == ["one effect"]
        assert all(owner is turn for owner, _, _ in prepared)
        assert [index for _, index, _ in prepared] == [0, 1]
        assert "STEERING_NOTE" in json.dumps(prepared[1][2]["input"])


class PreparedAnswer(BaseModel):
    value: int


@pytest.mark.parametrize("default_repairs", [False, True])
@pytest.mark.parametrize("safe_output", ["prompted", "native"])
async def test_resolved_operation_output_rejects_repair_before_dispatch(
    responses: ResponsesFixture,
    default_repairs: bool,
    safe_output: str,
) -> None:
    callbacks = []

    def edit(turn, request, **indices):
        callbacks.append(request)
        return None

    agent = Agent(
        model="gpt-5.5",
        instructions="Return a value",
        provider=provider(responses),
        output_type=PromptedOutput(PreparedAnswer, max_retries=1 if default_repairs else 0),
    )
    async with timeout(20), agent.session() as session:
        repair_options = (
            {}
            if default_repairs
            else {"output_type": PromptedOutput(PreparedAnswer, max_retries=1)}
        )
        with pytest.raises(ValueError, match="correction attempts"):
            await session.submit("Reject repair", prepare_request=edit, **repair_options)
        assert not responses.requests and not callbacks
        responses.respond(assistant_output('{"value":7}'))
        output = (
            PromptedOutput(PreparedAnswer, max_retries=0)
            if safe_output == "prompted"
            else NativeOutput(PreparedAnswer)
        )
        turn = await session.submit("Safe output", output_type=output, prepare_request=edit)
        assert (await turn.get_result()).final_output.value == 7
        assert len(responses.requests) == len(callbacks) == 1


async def test_output_repair_without_editor_keeps_existing_correction_flow(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("invalid JSON"))
    responses.respond(assistant_output('{"value":9}'))
    async with timeout(30):
        result = await Agent(
            model="gpt-5.5",
            instructions="Return a value",
            provider=provider(responses),
            output_type=PromptedOutput(PreparedAnswer, max_retries=1),
        ).run("Repair if needed")
        assert result.final_output.value == 9
        assert len(responses.requests) == 2


@pytest.mark.parametrize("entry", ["agent", "session"])
@pytest.mark.parametrize("selection", ["empty", "reference", "hosted-excluded"])
async def test_editor_uses_the_selected_tools(
    responses: ResponsesFixture, entry: str, selection: str
) -> None:
    @function_tool(defer_loading=False)
    def allowed() -> str:
        return "allowed"

    @function_tool(defer_loading=True)
    def deferred() -> str:
        return "deferred"

    selected = [] if selection == "empty" else [allowed.ref]
    received: list[list[str]] = []

    def edit(turn: Any, request: PreparedRequest, **indices: int) -> None:
        received.append([tool["name"] for tool in request["tools"]])
        return None

    responses.respond(assistant_output("done"))
    agent = Agent(
        model="gpt-5.5",
        instructions="Answer.",
        tools=[allowed, WebSearch() if selection == "hosted-excluded" else deferred],
        provider=provider(responses),
    )
    if entry == "agent":
        result = await agent.run("answer", tools=selected, prepare_request=edit)
    else:
        async with agent.session() as session:
            result = await (
                await session.submit("answer", tools=selected, prepare_request=edit)
            ).get_result()
    assert result.final_output == "done"
    assert received == [[] if selection == "empty" else ["allowed"]]
    assert len(responses.requests) == 1
    wire = responses.requests[0].get("tools", [])
    assert [tool["name"] for tool in wire] == received[0]


async def test_editor_rejects_selected_deferred_and_hosted_references(
    responses: ResponsesFixture,
) -> None:
    from agents import ToolRef

    @function_tool(defer_loading=True)
    def deferred() -> str:
        return "unused"

    agent = Agent(
        model="gpt-5.5",
        instructions="Answer.",
        tools=[deferred, WebSearch()],
        provider=provider(responses),
    )
    async with agent.session() as session:
        for ref in (deferred.ref, ToolRef("run", "web")):
            with pytest.raises(ValueError, match="prepare_request"):
                await session.submit(
                    "answer", tools=[ref], prepare_request=lambda turn, request, **indices: None
                )
    assert responses.requests == []


async def test_editor_checks_each_dynamic_roster_before_dispatch(
    responses: ResponsesFixture,
) -> None:
    effects: list[str] = []
    rosters: list[list[str]] = []

    @function_tool(defer_loading=False)
    def allowed() -> str:
        effects.append("allowed")
        return "done"

    @function_tool(defer_loading=True)
    def deferred() -> str:
        pytest.fail("deferred handler ran")

    def prepare(turn: Any, request: PreparedRequest, **indices: int) -> None:
        rosters.append([tool["name"] for tool in request["tools"]])

    responses.respond(function_call("allowed", {}, "effect"))
    agent = Agent(
        model="gpt-5.5",
        instructions="Answer.",
        tools=[allowed, deferred],
        provider=provider(responses),
    )
    async with agent.session() as session:
        turn = await session.submit(
            "answer",
            before_step=lambda turn, **indices: [deferred if effects else allowed],
            prepare_request=prepare,
        )
        with pytest.raises(NativeError, match="prepare_request"):
            await turn.get_result()
    assert effects == ["allowed"]
    assert rosters == [["allowed"]]
    assert len(responses.requests) == 1


async def test_editor_rejects_a_fixed_hosted_registration_with_an_empty_roster(
    responses: ResponsesFixture,
) -> None:
    peer = Peer()
    edits: list[PreparedRequest] = []

    def edit(turn: Any, request: PreparedRequest, **indices: int) -> None:
        edits.append(request)

    responses.respond(assistant_output("unexpected"))
    async with peer.serve() as url:
        agent = Agent(
            model="gpt-5.5",
            instructions="Answer.",
            tools=[HostedVerificationMCP(url=url, tools=["search"], approval_mode="preapproved")],
            provider=provider(responses),
        )
        async with agent.session() as session:
            with pytest.raises(ValueError, match="hosted"):
                await session.submit("answer", tools=[], prepare_request=edit)
    assert edits == []
    assert responses.requests == []
    assert peer.calls == []


@pytest.mark.parametrize("selection", ["default", "excluded", "handled"])
async def test_manual_editor_run_requires_a_handler_for_selected_manual_tools(
    responses: ResponsesFixture, selection: str
) -> None:
    manual = FunctionTool(
        name="lookup", description="Lookup", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        model="gpt-5.5", instructions="Answer.", tools=[manual], provider=provider(responses)
    )
    edits: list[int] = []

    def edit(turn: Any, request: PreparedRequest, **indices: int) -> None:
        edits.append(len(request["tools"]))

    if selection == "excluded":
        responses.respond(assistant_output("done"))
        result = await agent.run("answer", tools=[], prepare_request=edit)
        assert result.final_output == "done"
        assert edits == [0]
        assert len(responses.requests) == 1
        return

    responses.respond(function_call("lookup", {"key": "one"}, "manual"))
    if selection == "default":
        with pytest.raises(InteractionRequired):
            await agent.run("answer", prepare_request=edit)
        assert edits == []
        assert responses.requests == []
        return

    responses.respond(assistant_output("done"))

    async def answer(question: Any) -> Any:
        request = manual.match(question)
        assert request is not None and request.arguments.key == "one"
        return request.reply("manual result")

    result = await agent.run("answer", on_interaction=answer, prepare_request=edit)
    assert result.final_output == "done"
    assert edits == [1, 1]
    assert len(responses.requests) == 2


@pytest.mark.parametrize("synchronous", [False, True])
async def test_run_editor_uses_falsy_provider_override(responses, synchronous):
    class FalsyProvider(NativeProvider):
        def __bool__(self):
            return False

    responses.respond(assistant_output("overridden"))
    override = FalsyProvider(base_url=responses.url, api_key="synthetic", supports_websockets=False)
    agent = Agent(
        instructions="Use the selected provider.",
        model="gpt-5.5",
        provider=NativeProvider(
            base_url="http://127.0.0.1:1/v1", api_key="synthetic", supports_websockets=True
        ),
    )
    options = {
        "provider": override,
        "prepare_request": lambda _turn, _request, **_indices: None,
    }
    result = (
        await asyncio.to_thread(agent.run_sync, "answer", **options)
        if synchronous
        else await agent.run("answer", **options)
    )
    assert result.final_output == "overridden"
    assert len(responses.requests) == 1


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("websockets", [False, True])
async def test_run_editor_uses_delegated_session_transport(responses, synchronous, websockets):
    delegated = NativeProvider(
        base_url=responses.url, api_key="synthetic", supports_websockets=websockets
    )

    class DelegatedAgent(Agent):
        def session(self, **options):
            return super().session(provider=delegated, **options)

    # The declared provider must not stand in for the Session factory's provider.
    declared = provider(responses) if websockets else None
    agent = DelegatedAgent(model="gpt-5.5", instructions="Answer.", provider=declared)
    edits = []

    def edit(_turn, request, **_indices):
        edits.append(True)
        request["instructions"] = "Edited through the delegated HTTP Session."
        return request

    async def run():
        if synchronous:
            return await asyncio.to_thread(agent.run_sync, "answer", prepare_request=edit)
        return await agent.run("answer", prepare_request=edit)

    if websockets:
        with pytest.raises(ValueError, match="HTTP"):
            await run()
        assert edits == []
        assert responses.requests == []
    else:
        responses.respond(assistant_output("delegated"))
        result = await run()
        assert result.final_output == "delegated"
        assert edits == [True]
        assert len(responses.requests) == 1
        assert "Edited through the delegated HTTP Session." in json.dumps(responses.requests[0])


@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize(
    "selection", ["empty", "eager", "default", "manual", "handled", "later-manual"]
)
async def test_run_editor_checks_manual_tools_after_before_step(
    responses: ResponsesFixture, synchronous: bool, selection: str
) -> None:
    manual = FunctionTool(
        name="lookup", description="Lookup", input_type=LookupArgs, output_type=str
    )
    effects: list[str] = []
    steps: list[int] = []
    rosters: list[list[str]] = []

    @function_tool
    def eager() -> str:
        effects.append("eager")
        return "eager result"

    agent = Agent(
        model="gpt-5.5",
        instructions="Answer.",
        tools=[manual, eager],
        provider=provider(responses),
    )

    def before_step(turn: Any, *, step_index: int) -> Any:
        steps.append(step_index)
        if selection == "empty":
            return []
        if selection == "eager" or selection == "later-manual" and step_index == 0:
            return [eager.ref]
        if selection == "default":
            return None
        return [manual.ref]

    def edit(turn: Any, request: PreparedRequest, **indices: int) -> None:
        rosters.append([tool["name"] for tool in request["tools"]])

    async def answer(question: Any) -> Any:
        request = manual.match(question)
        assert request is not None and request.arguments.key == "one"
        return request.reply("manual result")

    options: dict[str, Any] = {"before_step": before_step, "prepare_request": edit}
    if selection == "handled":
        options["on_interaction"] = answer
    if selection in {"eager", "later-manual"}:
        responses.respond(function_call("eager", {}, "effect"))
    elif selection != "empty":
        responses.respond(function_call("lookup", {"key": "one"}, "manual"))
    responses.respond(assistant_output("done"))

    async def run() -> Any:
        if synchronous:
            return await asyncio.to_thread(agent.run_sync, "answer", **options)
        return await agent.run("answer", **options)

    if selection in {"default", "manual", "later-manual"}:
        with pytest.raises(InteractionRequired):
            await run()
        if selection == "later-manual":
            assert steps == [0, 1]
            assert rosters == [["eager"]]
            assert len(responses.requests) == 1
            assert effects == ["eager"]
            return
        assert steps == [0]
        assert rosters == []
        assert responses.requests == []
        assert effects == []
        return

    result = await run()
    assert result.final_output == "done"
    expected = [] if selection == "empty" else ["eager" if selection == "eager" else "lookup"]
    assert rosters == [expected] * (1 if selection == "empty" else 2)
    assert steps == list(range(len(rosters)))
    assert len(responses.requests) == len(rosters)
    assert [
        [tool["name"] for tool in request.get("tools", [])] for request in responses.requests
    ] == rosters
    assert effects == (["eager"] if selection == "eager" else [])
