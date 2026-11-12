"""Deferred restore keeps the selected root output and the opening default."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agents import (
    Agent,
    InteractionRequired,
    NativeError,
    NativeOutput,
    NativeProvider,
    PromptedOutput,
    RunBudget,
    Turn,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_deferred_jobs import Journal, Output, Provider, declared, done
from tests.test_native_function_concurrency import queued
from tests.test_native_hosted_mcp import declaration
from tests.test_native_manual_tools import next_request, provider
from tests.test_native_output_modes import output_format


@pytest.mark.parametrize("changed", [False, True], ids=["sealed", "changed"])
async def test_deferred_start_preserves_selected_hosted_mcp(responses, monkeypatch, changed):
    journal, backend = Journal(), Provider([done()])
    jobs, manual = declared(journal, backend)
    docs = declaration(require_approval="never")
    other = declaration("other", require_approval="never")
    catalog = Path(__file__).with_name("fixtures") / "request-control-models.json"
    model = next(
        entry["slug"]
        for entry in json.loads(catalog.read_text())["models"]
        if not entry.get("use_responses_lite")
    )
    native = NativeProvider(base_url=responses.url, api_key="synthetic-deferred-key")
    agent = Agent(
        instructions="Render.",
        model=model,
        tools=[manual, docs, other],
        provider=native,
    )
    responses.respond(function_call("render", {"pages": 2}, "deferred-render"))
    responses.respond(assistant_output("finished"))

    async with timeout(20), native, agent.session() as original:
        await original.submit("Render.", tools=[manual.ref, docs.ref])
        request = await next_request(original.interactions(), manual)
        if changed:
            prepare = jobs.prepare

            async def change_selection(*args, **kwargs):
                saved = await prepare(*args, **kwargs)
                return saved.model_copy(
                    update={"operation_tools": [*saved.operation_tools, other.ref]}
                )

            monkeypatch.setattr(jobs, "prepare", change_selection)
            with pytest.raises(ValueError, match="restored_hosted_mcp"):
                async with jobs.start(original, request):
                    pytest.fail("The changed checkpoint was treated as the SDK-sealed state")
            assert len(responses.requests) == 1
            assert backend.calls == []
            return
        async with jobs.start(original, request) as (_, resumed):
            assert isinstance(resumed, Turn)
            assert (await resumed.get_result()).final_output == "finished"

    assert [
        [tool["server_label"] for tool in request["tools"] if tool["type"] == "mcp"]
        for request in responses.requests
    ] == [["docs"], ["docs"]]


@pytest.mark.parametrize(
    "limit, restore_options, expected",
    [
        pytest.param(None, {}, 2, id="inherit-unlimited"),
        pytest.param(1, {}, 1, id="inherit-limited"),
        pytest.param(
            None, {"max_function_concurrency": 1}, 1, id="explicit-limit-unlimited-source"
        ),
        pytest.param(2, {"max_function_concurrency": 1}, 1, id="explicit-limit-limited-source"),
        pytest.param(1, {"max_function_concurrency": None}, 2, id="explicit-unlimited"),
    ],
)
async def test_deferred_start_preserves_function_admission_without_changing_hook_arguments(
    responses, monkeypatch, limit, restore_options, expected
):
    journal, backend = Journal(), Provider([done()])
    jobs, manual = declared(journal, backend)
    entered = []
    release = asyncio.Event()

    async def admission_settled(session):
        await queued(session, 2)
        # Both callbacks must reach user code or the admission queue before observation.
        while True:
            admission = session._active.function_admission
            waiting = len(admission._waiters or ()) if admission is not None else 0
            if len(entered) + waiting == 2:
                return
            await asyncio.sleep(0)

    @function_tool
    async def work(value: int) -> str:
        entered.append(value)
        await release.wait()
        return str(value)

    responses.respond(function_call("render", {"pages": 2}, "deferred-render"))
    responses.respond(
        function_call("work", {"value": 1}, "work-1"),
        function_call("work", {"value": 2}, "work-2"),
    )
    responses.respond(assistant_output("finished"))
    agent = Agent(
        instructions="Render, then work.",
        tools=[manual, work],
        provider=provider(responses),
    )
    hook_calls = []
    async with timeout(20), agent.session(max_function_concurrency=limit) as original:
        turn = await original.submit("Render.")
        request = await next_request(original.interactions(), manual)
        session_factory = agent.session

        @asynccontextmanager
        async def hook(*, state, context):
            # Existing application hooks need no new concurrency keyword.
            hook_calls.append(state)
            async with session_factory(state=state, context=context, **restore_options) as restored:
                yield restored

        monkeypatch.setattr(agent, "session", hook)
        async with jobs.start(original, request) as (restored, resumed):
            assert isinstance(resumed, Turn)
            try:
                await admission_settled(restored)
                assert len(entered) == expected
            finally:
                release.set()
            assert (await resumed.get_result()).final_output == "finished"
            assert sorted(entered) == [1, 2]
            saved = await restored.save_state()
        with pytest.raises(InteractionRequired):
            await turn.get_result()
        # Explicit destinations choose a current policy rather than saved host state.
        entered.clear()
        release = asyncio.Event()
        destination_limit = 1 if limit is None else 2
        responses.respond(
            function_call("work", {"value": 3}, "explicit-3"),
            function_call("work", {"value": 4}, "explicit-4"),
        )
        responses.respond(assistant_output("explicit"))
        async with session_factory(
            state=saved, max_function_concurrency=destination_limit
        ) as destination:
            explicit = await destination.submit("Use the destination policy.")
            try:
                await admission_settled(destination)
                assert len(entered) == destination_limit
            finally:
                release.set()
            assert (await explicit.get_result()).final_output == "explicit"
            assert sorted(entered) == [3, 4]
    assert len(hook_calls) == 1
    assert len(responses.requests) == 5


@pytest.mark.parametrize("limit", [0, -1, True])
async def test_deferred_start_rejects_invalid_hook_concurrency(responses, monkeypatch, limit):
    journal, backend = Journal(), Provider([done()])
    jobs, manual = declared(journal, backend)
    responses.respond(function_call("render", {"pages": 2}, "deferred-render"))
    agent = Agent(instructions="Render.", tools=[manual], provider=provider(responses))
    async with timeout(20), agent.session(max_function_concurrency=1) as original:
        await original.submit("Render.")
        request = await next_request(original.interactions(), manual)
        session_factory = agent.session

        @asynccontextmanager
        async def hook(*, state, context):
            async with session_factory(
                state=state, context=context, max_function_concurrency=limit
            ) as restored:
                yield restored

        monkeypatch.setattr(agent, "session", hook)
        with pytest.raises(ValueError, match="max_function_concurrency must be a positive integer"):
            async with jobs.start(original, request):
                pytest.fail("The hook's invalid concurrency limit was accepted")
    assert len(responses.requests) == 1
    assert backend.calls == []


@pytest.mark.parametrize(
    "default, later_reply, later_value",
    [
        pytest.param(None, "plain default", "plain default", id="plain"),
        pytest.param(
            NativeOutput(str), '{"response":"native default"}', "native default", id="native-str"
        ),
        pytest.param(
            PromptedOutput(str), '"prompted default"', "prompted default", id="prompted-str"
        ),
    ],
)
async def test_deferred_plain_override_resumes_once_then_restores_opening_default(
    responses, default, later_reply, later_value
):
    journal, backend = Journal(), Provider([done("synthetic-report")])
    jobs, tool = declared(journal, backend)
    lifecycle = []

    class ScopedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            restoring = kwargs.get("state") is not None
            lifecycle.append(("enter", restoring, self.output_type))
            try:
                async with super().session(**kwargs) as session:
                    yield session
            finally:
                lifecycle.append(("exit", restoring, self.output_type))

    connection = provider(responses)
    agent = ScopedAgent(
        instructions="Render.", tools=[tool], provider=connection, output_type=default
    )
    responses.respond(function_call("render", {"pages": 2}, "synthetic-render"))
    responses.respond(assistant_output("synthetic plain result"))
    responses.respond(assistant_output(later_reply))
    try:
        async with timeout(20), agent.session() as live:
            original = await live.submit("Render a synthetic report.", output_type=str)
            request = await next_request(live.interactions(), tool)
            async with jobs.start(live, request) as (restored, resumed):
                assert resumed is restored.restored_turn
                assert resumed.turn_id == original.turn_id
                assert resumed.submission_id != original.submission_id
                with pytest.raises(
                    (RuntimeError, ValueError), match="foreign|expired|delivered|open"
                ):
                    await request.respond(Output(report="synthetic recovery"))
                assert (await resumed.get_result()).final_output == "synthetic plain result"
                state = SessionState.from_json(journal.state_json)
                assert state.format_version == 5 and state.native_continuation is not None
                assert state.native_continuation.output_schema is None
                later = await restored.submit("Now use the opening default.")
                assert (await later.get_result()).final_output == later_value
                assert (await resumed.get_result()).final_output == "synthetic plain result"
                assert (await restored.save_state()).format_version == 4
            assert backend.active == 0
            assert backend.calls == [("start", 2)]
            assert journal.log.count("prepare") == journal.log.count("claim-start") == 1
            assert journal.log.count("save_result") == 1
            with pytest.raises(InteractionRequired):
                await original.get_result()
        assert lifecycle == [
            ("enter", False, default),
            ("enter", True, default),
            ("exit", True, default),
            ("exit", False, default),
        ]
        first, continuation, later_request = responses.requests
        assert output_format(first) is None and output_format(continuation) is None
        assert "synthetic-report" in tool_output(continuation, "synthetic-render")
        if isinstance(default, NativeOutput):
            assert (
                output_format(later_request)["schema"]["properties"]["response"]["type"] == "string"
            )
        else:
            assert output_format(later_request) is None
        if isinstance(default, PromptedOutput):
            assert "does not apply to later requests" in str(later_request["input"])
    finally:
        await connection.aclose()


@pytest.mark.parametrize(
    "default", [NativeOutput(str), PromptedOutput(str)], ids=["native", "prompted"]
)
async def test_deferred_restore_denial_still_expires_original_grant_and_starts_no_job(
    responses, default
):
    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    exits = []

    class DenyingAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                raise PermissionError("synthetic restore denied")
            try:
                async with super().session(**kwargs) as session:
                    yield session
            finally:
                exits.append("joined")

    connection = provider(responses)
    agent = DenyingAgent(
        instructions="Render.", tools=[tool], provider=connection, output_type=default
    )
    responses.respond(function_call("render", {"pages": 2}, "synthetic-denied"))
    try:
        async with timeout(20), agent.session() as live:
            original = await live.submit("Render.", output_type=str)
            request = await next_request(live.interactions(), tool)
            with pytest.raises(PermissionError, match="synthetic restore denied"):
                async with jobs.start(live, request):
                    pytest.fail("The original subclass must own restore admission.")
            assert journal.log == ["prepare"] and journal.state_json
            assert backend.calls == [] and backend.active == 0
            with pytest.raises((RuntimeError, ValueError), match="foreign|expired|delivered|open"):
                await request.respond(Output(report="synthetic recovery"))
        with pytest.raises(InteractionRequired) as paused:
            await original.get_result()
        assert paused.value.state is not None
        assert paused.value.state.to_json() == journal.state_json
        assert len(responses.requests) == 1 and exits == ["joined"]
    finally:
        await connection.aclose()


async def test_deferred_start_inherits_complete_observation_retention(responses, monkeypatch):
    from agents.observation import EventGap, OutputTextDelta
    from tests.test_native_agent_observation import text_response

    jobs, manual = declared(Journal(), Provider([done()]))
    responses.respond(function_call("render", {"pages": 2}, "retained-render"))
    responses.respond(*text_response("retained-output", *["x"] * 350))
    agent = Agent(instructions="Render, then answer.", tools=[manual], provider=provider(responses))
    async with timeout(30), agent.session(observation_retention="all") as original:
        original_turn = await original.submit("Render.")
        request = await next_request(original.interactions(), manual)
        session_factory = agent.session

        @asynccontextmanager
        async def hook(*, state, context):
            async with session_factory(state=state, context=context) as restored:
                yield restored

        monkeypatch.setattr(agent, "session", hook)
        async with jobs.start(original, request) as (_, resumed):
            assert (await resumed.get_result()).final_output == "x" * 350
            events = [event async for event in resumed.events()]
            assert not any(isinstance(event, EventGap) for event in events)
            assert (
                "".join(event.delta for event in events if isinstance(event, OutputTextDelta))
                == "x" * 350
            )
        with pytest.raises(InteractionRequired):
            await original_turn.get_result()


@pytest.mark.parametrize("max_requests", [1, 2])
@pytest.mark.parametrize("kind", ["requests", "tokens", "combined"])
async def test_deferred_start_preserves_request_budget_through_session_hook(
    responses, monkeypatch, max_requests, kind
):
    journal, backend = Journal(), Provider([done()])
    jobs, manual = declared(journal, backend)
    responses.respond(function_call("render", {"pages": 2}, "deferred-render"))
    if max_requests == 2:
        responses.respond(assistant_output("finished"))
    agent = Agent(instructions="Render and answer.", tools=[manual], provider=provider(responses))
    hook_states = []
    async with timeout(20), agent.session() as original:
        limits = RunBudget(
            max_requests=max_requests if kind != "tokens" else None,
            max_tokens=(14 if max_requests == 1 else 100) if kind != "requests" else None,
        )
        await original.submit("Render.", budget=limits)
        request = await next_request(original.interactions(), manual)
        session_factory = agent.session

        @asynccontextmanager
        async def hook(*, state, context):
            hook_states.append(state)
            async with session_factory(state=state, context=context) as restored:
                yield restored

        monkeypatch.setattr(agent, "session", hook)
        async with jobs.start(original, request) as (_, resumed):
            assert isinstance(resumed, Turn)
            if max_requests == 1:
                with pytest.raises(NativeError) as stopped:
                    await resumed.get_result()
                assert stopped.value.code == "run_budget_exceeded"
            else:
                assert (await resumed.get_result()).final_output == "finished"
    assert len(hook_states) == 1
    assert hook_states[0].run_budget.max_requests == (max_requests if kind != "tokens" else None)
    assert hook_states[0].run_budget.admitted_requests == (1 if kind != "tokens" else None)
    if kind != "requests":
        assert hook_states[0].run_budget.consumed_tokens == 14
    assert len(responses.requests) == max_requests
