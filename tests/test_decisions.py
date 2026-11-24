"""Decisions at the explicit-input, client, callback and Runner boundaries."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from agents import (
    Agent,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    RunState,
    SQLiteSession,
    handoff,
)
from agents.decisions import DecisionError, decision_handoff
from agents.decorators import tool
from agents.exceptions import UserError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

CONFIG = RunConfig(tracing_disabled=True)


class Peer:
    """Offline transport for the generated Decisions resource."""

    def __init__(self, value="blue"):
        if not hasattr(AsyncOpenAI, "decisions"):
            pytest.skip("This client has no Decisions resource.")
        self.requests = []
        self.value = value
        self.client = AsyncOpenAI(
            api_key="synthetic",
            base_url="https://fixture.invalid/v1",
            http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(self.respond), trust_env=False
            ),
        )

    async def respond(self, request):
        assert request.url.path == "/v1/decisions"
        self.requests.append(json.loads(request.content))
        if self.value == "transport":
            return httpx2.Response(500, json={})
        answer = {"name": "decision", "type": "choice", "choice": self.value}
        if self.value == "refusal":
            answer = {"name": "decision", "type": "refusal", "refusal": "private"}
        return httpx2.Response(
            200,
            headers={"x-request-id": "request"},
            json={
                "model": "fixture",
                "answers": [answer],
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "total_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )


class BlockingPeer(Peer):
    """Control suspension and simulate a provider returning success after cancellation."""

    def __init__(self):
        super().__init__("blue")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.request_task: asyncio.Task[Any] | None = None

    def expire_deadline(self, monkeypatch, *, before_dispatch):
        async def wait(tasks, *, timeout=None):
            if timeout is None:
                return await asyncio.wait(tasks)
            assert timeout == 0.01
            (self.request_task,) = tasks
            if not before_dispatch:
                await asyncio.wait_for(self.started.wait(), 3)
            return set(), set(tasks)

        # Only the Decisions deadline expires here. Keep real task cancellation and joins.
        clock = SimpleNamespace(**vars(asyncio))
        clock.wait = wait
        monkeypatch.setattr("agents.decisions.asyncio", clock)

    def check_timeout(self, *, before_dispatch):
        assert self.request_task is not None and self.request_task.done()
        if before_dispatch:
            assert self.request_task.cancelled()
            assert not self.started.is_set() and not self.finished.is_set() and not self.requests
        else:
            assert self.started.is_set() and self.finished.is_set() and len(self.requests) == 1

    async def respond(self, request):
        self.started.set()
        try:
            # Guard a missing cancellation without using elapsed time to trigger the deadline.
            await asyncio.wait_for(self.release.wait(), 5)
        except asyncio.CancelledError:
            await asyncio.sleep(0)
        self.finished.set()
        return await super().respond(request)


def source_agent(arguments=None):
    return Agent(
        name="Source",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call(
                            "select_specialist",
                            arguments or {"input": "evidence"},
                            call_id="transfer",
                        )
                    ],
                    response_id="parent",
                    request_id="original",
                )
            ]
        ),
    )


async def run(source, stream, value="history", **kwargs):
    if stream:
        result = Runner.run_streamed(source, value, run_config=CONFIG, **kwargs)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(source, value, run_config=CONFIG, **kwargs)


def test_openai_single_target_suggests_ordinary_handoff():
    source = source_agent()
    with pytest.raises(ValueError, match=r"handoff\(target\)"):
        decision_handoff(
            source,
            client=Peer().client,
            model="fixture",
            question="Which?",
            targets={"blue": Agent(name="Blue")},
        )
    assert not source.handoffs


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("routed", [False, True])
async def test_input_filter_changes_specialist_history_only(stream, routed):
    def summary(data):
        return replace(data, input_history="summary", input_items=())

    model = ScriptedModel([[assistant_message("done")]])
    target = Agent(name="Blue", model=model)
    provider = Peer("blue")
    if routed:
        source = source_agent()
        route = decision_handoff(
            source,
            client=provider.client,
            model="fixture",
            question="Which?",
            targets={"blue": target, "red": Agent(name="Red")},
            input_filter=summary,
        )
    else:
        route = handoff(target, input_filter=summary)
        source = Agent(
            name="Source",
            model=ScriptedModel([[function_call(route.tool_name, {}, call_id="transfer")]]),
        )
    assert not source.handoffs
    source.handoffs.append(route)
    result = await run(source, stream)
    assert result.last_agent is target
    assert model.calls[0].input == [{"role": "user", "content": "summary"}]
    assert [request["input"] for request in provider.requests] == (["evidence"] if routed else [])


@pytest.mark.parametrize("count", [0, 1, 2, 255, 256])
async def test_openai_handoff_catalog_uses_permitted_choice_count(count):
    model = ScriptedModel([[assistant_message("stay")]])
    source = Agent(name="Source", model=model)
    targets = {str(i): Agent(name=str(i)) for i in range(max(2, count))}
    client = Peer().client
    route = decision_handoff(
        source,
        client=client,
        model="fixture",
        question="Which?",
        targets=targets,
        is_available=lambda ctx, label: int(label) < count,
    )
    source.handoffs.append(route)
    await run(source, False)
    assert model.calls[0].handoffs == ([route] if 2 <= count <= 255 else [])


@pytest.mark.parametrize("stream", [False, True])
async def test_openai_handoff_revoked_after_catalog_skips_provider(stream, monkeypatch):
    source = source_agent()
    targets = {"blue": Agent(name="Blue", model=ScriptedModel()), "red": Agent(name="Red")}

    class Hooks(RunHooks):
        async def on_llm_end(self, context, agent, response):
            del targets["red"]

    provider = Peer()
    source.handoffs.append(
        decision_handoff(
            source, client=provider.client, model="fixture", question="Which?", targets=targets
        )
    )
    with pytest.raises(ValueError, match="unavailable"):
        await run(source, stream, hooks=Hooks())
    assert not provider.requests
    assert isinstance(source.model, ScriptedModel)
    assert len(source.model.calls) == 1 and source.model.calls[0].handoffs


@pytest.mark.parametrize("stream", [False, True])
async def test_runner_preserves_context_call_identity_and_specialist_permissions(stream):
    context = {"allowed": "blue"}
    transfers, effects = [], []

    class Hooks(RunHooks):
        async def on_handoff(self, context, from_agent, to_agent):
            transfers.append((context.context, from_agent, to_agent))

    @tool(is_enabled=False)
    def hidden() -> str:
        raise AssertionError

    @tool(needs_approval=True)
    def gated() -> str:
        effects.append("approved")
        return "done"

    model = ScriptedModel([[function_call("gated", {}, call_id="restricted")]])
    blue = Agent(
        name="Blue", model=model, tools=[hidden, gated], tool_use_behavior="stop_on_first_tool"
    )
    red = Agent(name="Red", model=ScriptedModel())
    source = source_agent()
    provider = Peer("blue")
    targets = {"red": red, "blue": blue}
    route = decision_handoff(
        source,
        client=provider.client,
        model="fixture",
        question="Which?",
        targets=targets,
        is_available=lambda ctx, label: label == "red" or ctx.context["allowed"] == label,
    )
    source.handoffs.append(route)
    result = await run(
        source, stream, context=context, hooks=Hooks(), auto_previous_response_id=True
    )
    assert result.last_agent is blue
    assert len(transfers) == 1
    ctx, parent, target = transfers[0]
    assert ctx is context and parent is source and target is blue
    assert result.raw_responses[0].request_id == "original"
    assert model.calls[0].previous_response_id == "parent"
    assert [
        i["call_id"] for i in model.calls[0].input if i.get("type") == "function_call_output"
    ] == ["transfer"]
    assert [t.name for t in model.calls[0].tools] == ["gated"]
    assert not effects and len(result.interruptions) == 1
    assert result.context_wrapper.usage.total_tokens == 7
    saved = result.to_state().to_string()
    with pytest.raises(UserError):
        await RunState.from_string(source, saved)
    with pytest.raises(UserError):
        await RunState.from_string(source, saved, context_override={"allowed": "red"})
    del targets["blue"]
    with pytest.raises(UserError):
        await RunState.from_string(source, saved, context_override=context)
    targets["blue"] = blue.clone()
    with pytest.raises(UserError):
        await RunState.from_string(source, saved, context_override=context)
    targets["blue"] = blue
    source.handoffs[:] = [handoff(blue, is_enabled=False)]
    with pytest.raises(UserError):
        await RunState.from_string(source, saved, context_override=context)
    source.handoffs[:] = [route]
    state = await RunState.from_string(source, saved, context_override=context)
    state.approve(state.get_interruptions()[0])
    resumed = await Runner.run(source, state, run_config=CONFIG)
    assert resumed.final_output == "done" and effects == ["approved"]
    assert len(model.calls) == len(provider.requests) == 1
    assert provider.requests[0]["input"] == "evidence"
    assert provider.requests[0]["questions"][0]["choices"] == [{"value": "red"}, {"value": "blue"}]


async def test_restore_rebuilt_roster_binds_label_and_agent_without_reselection():
    provider = Peer("blue")
    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append("record")
        return "done"

    def build(label):
        source = source_agent()
        target = Agent(
            name="Specialist",
            tools=[record],
            tool_use_behavior="stop_on_first_tool",
            model=ScriptedModel([[function_call("record", {}, call_id="write")]]),
        )
        source.handoffs.append(
            decision_handoff(
                source,
                client=provider.client,
                model="fixture",
                question="Which?",
                targets={label: target, "red": Agent(name="Red")},
            )
        )
        return source, target

    source, _ = build("blue")
    saved = (await run(source, False)).to_state().to_json()
    with pytest.raises(UserError, match="agent identity"):
        await RunState.from_json(build("green")[0], saved)
    restored, target = build("blue")
    state = await RunState.from_json(restored, saved)
    state.approve(state.get_interruptions()[0])
    result = await Runner.run(restored, state, run_config=CONFIG)
    assert result.last_agent is target and result.final_output == "done"
    assert effects == ["record"] and len(provider.requests) == 1


@pytest.mark.parametrize(
    "case",
    [
        "replaced",
        "removed",
        "permission",
        "unknown",
        "timeout",
        "timeout_before_dispatch",
        "cancel",
    ],
)
async def test_no_transfer_after_revocation_failure_or_cancellation(case, monkeypatch):
    source = source_agent()
    model = ScriptedModel([[assistant_message("unexpected")]])
    targets = {"blue": Agent(name="Blue", model=model), "red": Agent(name="Red")}
    context = {"allowed": True}
    provider = BlockingPeer()
    before_dispatch = case == "timeout_before_dispatch"
    timeout = before_dispatch or case == "timeout"
    if timeout:
        provider.expire_deadline(monkeypatch, before_dispatch=before_dispatch)
    source.handoffs.append(
        decision_handoff(
            source,
            client=provider.client,
            model="fixture",
            timeout=0.01 if timeout else 30,
            question="Which?",
            targets=targets,
            is_available=lambda ctx, label: ctx.context["allowed"],
        )
    )
    task = asyncio.create_task(run(source, False, context=context))
    try:
        if not timeout:
            await asyncio.wait_for(provider.started.wait(), 3)
        error: type[BaseException] = ValueError
        if case == "replaced":
            targets["blue"] = targets["blue"].clone()
        elif case == "removed":
            source.handoffs.clear()
        elif case == "permission":
            context["allowed"] = False
        elif case == "unknown":
            provider.value = "unlisted_tool"
            error = DecisionError
        elif timeout:
            error = asyncio.TimeoutError
        else:
            task.cancel()
            error = asyncio.CancelledError
        if error in (ValueError, DecisionError):
            provider.release.set()
        with pytest.raises(error):
            await task
        if timeout:
            provider.check_timeout(before_dispatch=before_dispatch)
        else:
            assert provider.finished.is_set() and len(provider.requests) == 1
        assert isinstance(source.model, ScriptedModel)
        assert not model.calls and len(source.model.calls) == 1
    finally:
        provider.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("arguments", [{"input": "a", "grant": True}, {"input": "é" * 4097}])
async def test_model_cannot_extend_routing_input(arguments):
    source = source_agent(arguments)
    provider = Peer("blue")
    source.handoffs.append(
        decision_handoff(
            source,
            client=provider.client,
            model="fixture",
            question="Which?",
            targets={"blue": Agent(name="Blue"), "red": Agent(name="Red")},
        )
    )
    with pytest.raises(ValueError, match="routing input"):
        await run(source, False)
    assert not provider.requests


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["arguments", "refusal", "invalid_response"])
@pytest.mark.parametrize("hide_model,hide_tool", [(True, False), (False, True), (False, False)])
@pytest.mark.parametrize("recover", [False, True])
async def test_handoff_error_redaction(
    monkeypatch, stream, failure, hide_model, hide_tool, recover
):
    from agents import _debug
    from agents.exceptions import _is_error_data_redacted
    from tests.test_error_logging_redaction import _agents_traceback_frame_locals

    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", hide_model)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", hide_tool)
    marker = "SYNTHETIC_PRIVATE_ROUTING_EVIDENCE"
    arguments = {"input": marker}
    if failure == "arguments":
        arguments["extra"] = "unknown"
    source = source_agent(arguments)

    provider = Peer("refusal" if failure == "refusal" else "unknown")

    handoff = decision_handoff(
        source,
        client=provider.client,
        model="fixture",
        question="Which?",
        targets={"blue": Agent(name="Blue"), "red": Agent(name="Red")},
    )
    source.handoffs.append(handoff)
    errors: list[Exception] = []
    if recover:

        def report(ctx, error):
            if hide_model or hide_tool:
                assert error.__traceback__ is None
            errors.append(error)
            return "Selection failed"

        handoff.failure_error_function = report
        assert isinstance(source.model, ScriptedModel)
        source.model.enqueue([assistant_message("recovered")])
        assert (await run(source, stream)).final_output == "recovered"
        (error,) = errors
    else:
        with pytest.raises(ValueError) as caught:
            await run(source, stream)
        error = caught.value
    if failure == "arguments":
        assert not provider.requests
    else:
        assert isinstance(error, DecisionError) and error.reason == failure
        assert error.request_id == "request"
    assert _is_error_data_redacted(error) == (hide_model or hide_tool)
    if hide_model or hide_tool:
        assert error.__cause__ is error.__context__ is None
        assert marker not in str(error)
        assert marker not in repr(_agents_traceback_frame_locals(error))


async def test_immediate_stream_cancellation_drains_late_selection():
    source = source_agent()
    model = ScriptedModel()
    provider = BlockingPeer()
    source.handoffs.append(
        decision_handoff(
            source,
            client=provider.client,
            model="fixture",
            question="Which?",
            targets={"blue": Agent(name="Blue", model=model), "red": Agent(name="Red")},
        )
    )
    result = Runner.run_streamed(source, "history", run_config=CONFIG)

    async def drain():
        async for _ in result.stream_events():
            pass

    consumer = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(provider.started.wait(), 3)
    finally:
        result.cancel()
        await consumer
    assert provider.finished.is_set() and not model.calls
    assert result.last_agent is source


@pytest.mark.parametrize("stream", [False, True])
async def test_primary_can_answer_without_selection_and_clones_need_their_own_route(stream):
    provider = Peer("blue")
    model = ScriptedModel(
        [
            [assistant_message("direct")],
            [assistant_message("direct")],
            [function_call("select_specialist", {"input": "evidence"}, call_id="clone")],
            [function_call("select_specialist", {"input": "evidence"}, call_id="transfer")],
        ]
    )
    source = Agent(name="Source", model=model)
    original = Agent(name="Blue", model=ScriptedModel([[assistant_message("original")]]))
    route = decision_handoff(
        source,
        question="Which?",
        targets={"blue": original, "red": Agent(name="Red")},
        client=provider.client,
        model="fixture",
    )
    source.handoffs.append(route)
    shallow = source.clone()
    assert shallow.handoffs is source.handoffs
    for agent in (source, shallow):
        assert (await run(agent, stream)).final_output == "direct"
    assert len(model.calls[0].handoffs) == 1 and not model.calls[1].handoffs
    assert not provider.requests

    clone = source.clone(handoffs=[])
    alternate = Agent(name="Blue", model=ScriptedModel([[assistant_message("alternate")]]))
    clone.handoffs.append(
        decision_handoff(
            clone,
            question="Which?",
            targets={"blue": alternate, "red": Agent(name="Red")},
            client=provider.client,
            model="fixture",
        )
    )
    assert source.handoffs == [route]
    assert (await run(clone, stream)).last_agent is alternate
    assert (await run(source, stream)).last_agent is original
    assert len(provider.requests) == 2


async def test_explicit_recovery_returns_to_caller_without_transferring():
    model = ScriptedModel(
        [
            [function_call("select_specialist", {"input": "evidence"}, call_id="transfer")],
            [assistant_message("direct")],
        ]
    )
    source = Agent(name="Source", model=model)
    provider = Peer("unknown")
    route = decision_handoff(
        source,
        client=provider.client,
        model="fixture",
        question="Which?",
        targets={"blue": Agent(name="Blue"), "red": Agent(name="Red")},
    )
    route.failure_error_function = lambda ctx, error: "Transfer unavailable"
    source.handoffs.append(route)
    result = await run(source, False)
    assert result.last_agent is source and result.final_output == "direct"
    assert result.context_wrapper.usage.total_tokens == 7 and len(provider.requests) == 1
    assert any(
        i.get("output") == "Transfer unavailable" and i.get("call_id") == "transfer"
        for i in model.calls[1].input
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("approve_first", [False, True])
@pytest.mark.parametrize(
    "failure", ["cancel", "timeout", "timeout_before_dispatch", "refusal", "invalid"]
)
async def test_selection_failure_keeps_completed_tool_receipt(
    stream, approve_first, failure, monkeypatch
):
    effects = []

    before_dispatch = failure == "timeout_before_dispatch"
    timeout = before_dispatch or failure == "timeout"

    class Failing(BlockingPeer):
        async def respond(self, request):
            effects.append("select")
            if timeout:
                return await super().respond(request)
            if failure == "cancel":
                future = asyncio.get_running_loop().create_future()
                future.cancel("private details")
                return await future
            self.value = "refusal" if failure == "refusal" else "unknown"
            return await Peer.respond(self, request)

    peer = Failing()
    if timeout:
        peer.expire_deadline(monkeypatch, before_dispatch=before_dispatch)

    @tool(needs_approval=approve_first)
    def record() -> str:
        effects.append("record")
        return "recorded"

    source = Agent(
        name="Source",
        tools=[record],
        model=ScriptedModel(
            [
                [
                    function_call("record", {}, call_id="effect"),
                    function_call("select_specialist", {"input": "evidence"}, call_id="select"),
                ]
            ]
        ),
    )
    specialist = ScriptedModel()
    source.handoffs.append(
        decision_handoff(
            source,
            client=peer.client,
            model="fixture",
            timeout=0.01 if timeout else 30,
            question="Which?",
            targets={"blue": Agent(name="Blue", model=specialist), "red": Agent(name="Red")},
        )
    )
    session = SQLiteSession("receipts")
    try:
        value: Any = "history"
        if approve_first:
            paused = await run(source, stream, session=session)
            value = paused.to_state()
            value.approve(value.get_interruptions()[0])
        error: type[Exception] = RuntimeError if failure == "cancel" else DecisionError
        if timeout:
            error = asyncio.TimeoutError
        with pytest.raises(error) as caught:
            await run(source, stream, value, session=session)
        if failure == "cancel":
            assert str(caught.value) == "Decisions request was cancelled"
        if approve_first:
            items = [
                entry["raw_item"]
                for entry in value.to_json()["generated_items"]
                if entry["type"] != "tool_approval_item"
            ]
        else:
            items = await session.get_items()
        receipts = [i for i in items if i.get("call_id") == "effect"]
        assert [i["type"] for i in receipts] == ["function_call", "function_call_output"]
        assert receipts[-1].get("output") == "recorded"
        if timeout:
            peer.check_timeout(before_dispatch=before_dispatch)
        assert effects == (["record"] if before_dispatch else ["record", "select"])
        assert not specialist.calls
    finally:
        session.close()


@pytest.mark.parametrize("stream", [False, True])
async def test_nested_specialist_uses_fresh_context_and_owns_approval(stream):
    effects = []
    provider = Peer("blue")

    @tool(needs_approval=True)
    def record(ctx: RunContextWrapper[dict[str, str]]) -> str:
        effects.append(ctx.context)
        return "recorded"

    source = source_agent()
    target = Agent(
        name="Blue",
        tools=[record],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel([[function_call("record", {}, call_id="effect")]]),
    )
    route = decision_handoff(
        source,
        client=provider.client,
        model="fixture",
        question="Which?",
        targets={"blue": target, "red": Agent(name="Red")},
        is_available=lambda ctx, label: label == "red" or ctx.context["allowed"] == label,
    )
    source.handoffs.append(route)
    manager = Agent(
        name="Manager",
        tools=[source.as_tool(tool_name="ask", tool_description="Ask")],
        model=ScriptedModel(
            [
                [function_call("ask", {"input": "work"}, call_id="parent")],
                [assistant_message("done")],
            ]
        ),
    )
    paused = await run(manager, stream, context={"allowed": "blue"})
    saved = paused.to_state().to_string()
    fresh = {"allowed": "blue", "revision": "current"}
    with pytest.raises(UserError, match="unavailable"):
        await RunState.from_string(manager, saved)
    with pytest.raises(UserError, match="unavailable"):
        await RunState.from_string(manager, saved, context_override={"allowed": "red"})
    source.handoffs[:] = [target]
    with pytest.raises(UserError):
        await RunState.from_string(manager, saved, context_override=fresh)
    source.handoffs[:] = [route]
    state = await RunState.from_string(manager, saved, context_override=fresh)
    assert not effects
    state.approve(state.get_interruptions()[0])
    done = await run(manager, not stream, state)
    assert done.final_output == "done"
    assert len(effects) == len(provider.requests) == 1 and effects[0] is fresh


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["memory", "loaded", "session"])
@pytest.mark.parametrize("nested", [False, True])
async def test_resume_rechecks_only_selected_grant_before_any_effect(stream, mode, nested):
    from agents.memory.resumable_session import ResumableSession
    from tests.memory.test_resumable_session import MemoryStore

    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append("record")
        return "done"

    source = source_agent()
    blue = Agent(
        name="Blue",
        model=ScriptedModel([[function_call("record", {}, call_id="effect")]]),
        tools=[record],
        tool_use_behavior="stop_on_first_tool",
    )
    targets = {"blue": blue, "red": Agent(name="Red")}
    peer = Peer()
    grants = {"blue", "red"}
    route = decision_handoff(
        source,
        client=peer.client,
        model="fixture",
        question="Which?",
        targets=targets,
        is_available=lambda ctx, label: label in grants,
    )
    source.handoffs.append(route)
    root = source
    if nested:
        delegate = source.as_tool("ask", "Ask")
        sibling = Agent(name="Other").as_tool("ask", "Other", is_enabled=False)
        root = Agent(
            name="Manager",
            model=ScriptedModel(
                [
                    [function_call("ask", {"input": "work"}, call_id="child")],
                    [assistant_message("done")],
                ]
            ),
            tools=[delegate, sibling],
        )
    store = MemoryStore()
    session = await ResumableSession.create(store) if mode == "session" else None
    state = (await run(root, stream, context={}, session=session)).to_state()
    if session:
        state = await session.load_state(root, context_override={})
    elif mode == "loaded":
        state = await RunState.from_string(root, state.to_string(), context_override={})
    grants.clear()
    state.approve(state.get_interruptions()[0])
    before = store.records.copy()
    with pytest.raises(UserError, match="specialist unavailable"):
        await run(root, stream, state)
    grants.add("blue")
    source.handoffs.clear()
    with pytest.raises(UserError):
        await run(root, stream, state)
    assert not effects and store.records == before
    source.handoffs.append(route)
    if nested:
        for enabled, other in [(False, False), (True, True), (False, True)]:
            delegate.is_enabled, sibling.is_enabled = enabled, other
            with pytest.raises(UserError, match="same enabled agent tool"):
                await run(root, stream, state)
            assert not effects and store.records == before and len(peer.requests) == 1
        delegate.is_enabled, sibling.is_enabled = True, False
    assert (await run(root, stream, state)).final_output == "done"
    assert effects == ["record"] and len(peer.requests) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("other", ["plain", "handoff", "decision"])
async def test_shared_specialist_rejected_before_primary_inference(stream, other):
    source = source_agent()
    peer = Peer()
    blue = Agent(name="Blue")

    def route(name):
        return decision_handoff(
            source,
            client=peer.client,
            model="fixture",
            question="Which?",
            tool_name=name,
            targets={"blue": blue, "red": Agent(name="Red")},
        )

    source.handoffs.append(route("select_specialist"))
    source.handoffs.append(
        blue if other == "plain" else handoff(blue) if other == "handoff" else route("second")
    )
    with pytest.raises(UserError, match="separate specialist"):
        await run(source, stream)
    assert isinstance(source.model, ScriptedModel)
    assert not source.model.calls and not peer.requests


def test_invalid_permanent_configuration(monkeypatch):
    client = Peer().client
    blue = Agent(name="Blue")
    targets = {"blue": blue, "red": Agent(name="Red")}

    def build(model="fixture", roster=targets):
        return decision_handoff(
            source_agent(), client=client, model=model, question="Which?", targets=roster
        )

    with pytest.raises(ValueError, match="model"):
        build(" ")
    with pytest.raises(ValueError, match="separate"):
        build(roster={"a": blue, "b": blue})
    with pytest.raises(ValueError, match="255"):
        build(roster={str(i): Agent(name=str(i)) for i in range(256)})
    monkeypatch.setattr(AsyncOpenAI, "decisions", None, raising=False)
    with pytest.raises(ValueError, match="Decisions support"):
        build()


async def test_transport_error_has_no_retry():
    from openai import InternalServerError

    peer = Peer("transport")
    source = source_agent()
    source.handoffs.append(
        decision_handoff(
            source,
            client=peer.client,
            model="fixture",
            question="Which?",
            targets={"a": Agent(name="A"), "b": Agent(name="B")},
        )
    )
    with pytest.raises(InternalServerError):
        await run(source, False)
    assert len(peer.requests) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_selector_names_must_be_unique_before_inference(stream):
    source = source_agent()
    peer = Peer()
    for _ in range(2):
        source.handoffs.append(
            decision_handoff(
                source,
                client=peer.client,
                model="fixture",
                question="Which?",
                targets={"blue": Agent(name="Blue"), "red": Agent(name="Red")},
            )
        )
    with pytest.raises(UserError, match="unique tool_name"):
        await run(source, stream)
    assert isinstance(source.model, ScriptedModel)
    assert not source.model.calls and not peer.requests
