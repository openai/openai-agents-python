"""A deferred call retains its live authority and keeps host outcomes out of Task reprs."""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import replace

import pytest

from agents import Agent, HttpMCP, NativeProvider, Turn, function_tool
from agents._runtime.experimental.deferred import _wait_for_job
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_deferred_jobs import Journal, Output, Provider, declared, done
from tests.test_native_manual_tools import next_request, provider
from tests.test_native_mcp_tools import Peer, mcp_call


def callback(label, calls):
    @function_tool(name="select")
    def select() -> str:
        calls.append(label)
        return label

    return select


async def pending(session, tool, responses):
    responses.respond(function_call(tool.name, {"pages": 2}, "job"))
    await session.submit("Start.")
    return await next_request(session.interactions(), tool)


@pytest.mark.parametrize(
    "change", ["before-start", "in-prepare", "replace-pending", "clone-checkpoint"]
)
async def test_implicit_restore_retains_callback_and_dispatches_session_policy(responses, change):
    calls, entries = [], []
    opening, future = callback("opening", calls), callback("future", calls)

    class Store(Journal):
        async def prepare(self, state_json, bindings):
            nonlocal future_agent
            await super().prepare(state_json, bindings)
            if change == "in-prepare":
                future_agent = replace(agent, tools=(tool, future))

    class PolicyAgent(Agent):
        def __copy__(self):
            pytest.fail("Opening copied the application Agent")

        def __reduce_ex__(self, protocol):
            pytest.fail("Opening pickled the application Agent")

        @asynccontextmanager
        async def session(self, **kwargs):
            assert self is agent or self is future_agent
            entries.append(kwargs.get("state") is not None)
            if change == "clone-checkpoint" and kwargs.get("state") is not None:
                kwargs["state"] = SessionState.from_json(kwargs["state"].to_json())
            async with super().session(**kwargs) as session:
                yield session

    jobs, tool = declared(Store(), Provider([done()]))
    agent = PolicyAgent(instructions="Select.", tools=[tool, opening], provider=provider(responses))
    future_agent = agent
    async with timeout(15), agent.session() as session:
        request = await pending(session, tool, responses)
        if change in {"before-start", "replace-pending", "clone-checkpoint"}:
            _, replacement = declared(Journal(), Provider([]))
            future_agent = replace(
                agent, tools=(replacement if change == "replace-pending" else tool, future)
            )
        responses.respond(function_call("select", {}, "restored"))
        responses.respond(assistant_output("finished"))
        async with jobs.start(session, request) as (_, turn):
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
    responses.respond(function_call("select", {}, "new"))
    responses.respond(assistant_output("new session"))
    assert (
        await future_agent.run(
            "Select.", on_interaction=lambda _: pytest.fail("New run unexpectedly requested a job")
        )
    ).final_output == "new session"
    assert entries == [False, True, False] and calls == ["opening", "future"]
    assert tool_output(responses.requests[2], "restored") == "opening"
    assert tool_output(responses.requests[4], "new") == "future"


async def test_mcp_declaration_changes_while_operation_acquires_the_opening_binding(responses):
    release = asyncio.Event()
    original, future = Peer(label="opening", release_ready=release), Peer(label="future")
    jobs, tool = declared(Journal(), Provider([done()]))
    async with (
        timeout(20),
        original.serve() as first,
        future.serve() as second,
        AsyncExitStack() as stack,
    ):

        def selection(url, key):
            return (tool, HttpMCP(name="records", tools=["search"], url=url, bearer_token=key))

        agent = Agent(
            instructions="Search.", tools=selection(first, "a1"), provider=provider(responses)
        )
        session = await stack.enter_async_context(agent.session())
        opening = asyncio.create_task(pending(session, tool, responses))
        try:
            await original.ready_entered.wait()
            assert not opening.done()
            agent = replace(agent, tools=selection(second, "b2"))
        finally:
            release.set()
            request = await opening
        responses.respond(mcp_call("search", "mcp__records", "restored"))
        responses.respond(assistant_output("finished"))
        async with jobs.start(session, request) as (_, turn):
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
        responses.respond(mcp_call("search", "mcp__records", "new"))
        responses.respond(assistant_output("new session"))
        async with agent.session() as fresh:
            assert (
                await (await fresh.submit("Search.")).get_result()
            ).final_output == "new session"
    assert len(original.calls) == len(future.calls) == 1
    assert set(original.auth) == {"Bearer a1"} and set(future.auth) == {"Bearer b2"}
    assert "opening" in tool_output(responses.requests[2], "restored")
    assert "future" in tool_output(responses.requests[4], "new")


@pytest.mark.parametrize(
    "field",
    [
        "instructions",
        "name",
        "model",
        "model_settings",
        "output_type",
        "programmatic_tool_calling",
        "provider",
    ],
)
async def test_future_agent_configuration_does_not_replace_the_live_selection(responses, field):
    jobs, tool = declared(Journal(), Provider([done()]))
    original = provider(responses)
    future = NativeProvider(base_url=responses.url, api_key="b2")
    agent = Agent(instructions="Render.", tools=[tool], provider=original)
    replacements = {
        "instructions": "Future.",
        "name": "future",
        "model": "future-model",
        "model_settings": {"reasoning_effort": "high"},
        "output_type": Output,
        "programmatic_tool_calling": True,
        "provider": future,
    }
    async with timeout(15), agent.session() as session:
        request = await pending(session, tool, responses)
        changed = replace(agent, **{field: replacements[field]})
        assert getattr(changed, field) == replacements[field]
        responses.respond(assistant_output("finished"))
        async with jobs.start(session, request) as (_, turn):
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
            with pytest.raises(RuntimeError):
                await original.aclose()
    assert [row["authorization"] for row in responses.request_headers] == [
        "Bearer " + original.api_key
    ] * 2
    await original.aclose()
    await future.aclose()


async def test_current_session_update_uses_explicit_restore_and_checkpoint_settings(responses):
    calls = []
    jobs, tool = declared(Journal(), Provider([done()]))
    agent = Agent(
        instructions="Select.",
        model_settings={"reasoning_effort": "medium"},
        tools=[tool, callback("opening", calls)],
        provider=provider(responses),
    )
    selected = (tool, callback("accepted", calls))
    async with timeout(15), agent.session(tools=selected) as session:
        await session.update(instructions="Use the accepted selection.")
        await session.update(model_settings={"reasoning_effort": "low"})
        agent = replace(
            agent,
            tools=selected,
            instructions="Use the accepted selection.",
            model_settings={"reasoning_effort": "high"},
        )
        request = await pending(session, tool, responses)
        with pytest.raises(ValueError, match="opening tools and instructions"):
            async with jobs.start(session, request):
                pytest.fail("An updated Session must use explicit restore")
        state = await jobs.prepare(session, request)
        responses.respond(function_call("select", {}, "accepted"))
        responses.respond(assistant_output("finished"))
        async with agent.session(state=state) as restored:
            fresh = await next_request(restored.interactions(), tool)
            turn = await jobs.advance(restored, fresh)
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
    responses.respond(assistant_output("new session"))
    assert (
        await agent.run(
            "Select.", on_interaction=lambda _: pytest.fail("New run unexpectedly requested a job")
        )
    ).final_output == "new session"
    assert calls == ["accepted"]
    assert [row["reasoning"]["effort"] for row in responses.requests] == [
        "low",
        "low",
        "low",
        "high",
    ]


@asynccontextmanager
async def debug_logs(caplog, private):
    loop = asyncio.get_running_loop()
    debug, duration = loop.get_debug(), loop.slow_callback_duration
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger("asyncio").addHandler(handler)

    async def public_control():
        return "visible-task"

    try:
        loop.set_debug(True)
        loop.slow_callback_duration = 0
        with caplog.at_level(logging.WARNING):
            yield
            logging.getLogger("application").warning("visible application message")
            await asyncio.create_task(public_control())
        assert loop.get_debug() and loop.slow_callback_duration == 0
    finally:
        loop.slow_callback_duration = duration
        loop.set_debug(debug)
        logging.getLogger("asyncio").removeHandler(handler)
    assert "result='visible-task'" in stream.getvalue()
    assert private not in stream.getvalue()
    records = [(r.name, r.getMessage()) for r in caplog.records]
    assert ("application", "visible application message") in records
    assert any(name == "asyncio" and "result='visible-task'" in msg for name, msg in records)
    assert all(private not in msg for _, msg in records)


async def test_public_prepare_keeps_journal_exception_out_of_task_logs(responses, caplog):
    class Store(Journal):
        async def prepare(self, state_json, bindings):
            raise PermissionError("j7Q")

    backend = Provider([])
    jobs, tool = declared(Store(), backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        request = await pending(session, tool, responses)
        async with debug_logs(caplog, "j7Q"):
            with pytest.raises(
                PermissionError, match="Deferred checkpoint preparation was denied"
            ) as error:
                await jobs.prepare(session, request)
            assert error.value.__context__ is None and error.value.__cause__ is None
    assert not backend.calls


@pytest.mark.parametrize("fail", [False, True], ids=["result", "exception"])
async def test_owned_job_preserves_outcome_identity_and_context(caplog, fail):
    marker = "r8V"
    binding = ContextVar("binding")
    loop = asyncio.get_running_loop()
    problem = PermissionError(marker)

    async def work():
        assert asyncio.get_running_loop() is loop and binding.get() is problem
        if fail:
            raise problem
        return marker

    token = binding.set(problem)
    try:
        async with debug_logs(caplog, marker):
            if fail:
                with pytest.raises(PermissionError) as error:
                    await _wait_for_job(work(), 2)
                assert error.value is problem
            else:
                assert (await _wait_for_job(work(), 2)) is marker
    finally:
        binding.reset(token)


async def test_owned_job_closes_work_cancelled_before_entry():
    async def work():
        pytest.fail("Cancelled work entered")

    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    coroutine = work()

    def cancelled(owner, wrapper, **kwargs):
        task = asyncio.Task(wrapper, loop=owner, **kwargs)
        task.cancel()
        return task

    try:
        loop.set_task_factory(cancelled)
        with pytest.raises(asyncio.CancelledError):
            await _wait_for_job(coroutine, 2)
        assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED
    finally:
        loop.set_task_factory(previous)
        coroutine.close()


@pytest.mark.parametrize("phase", ["project", "save-result"])
async def test_public_advance_keeps_host_exception_out_of_direct_task_logs(
    responses, caplog, phase
):
    marker = "p4K"

    class Store(Journal):
        async def save_result(self, claim, result_json):
            if phase == "save-result":
                raise PermissionError(marker)
            await super().save_result(claim, result_json)

    def project(value):
        if phase == "project":
            raise PermissionError(marker)
        return Output(report=value["public"])

    journal, backend = Store(), Provider([done()])
    jobs, tool = declared(journal, backend, project=project)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        request = await pending(session, tool, responses)
        async with debug_logs(caplog, marker):
            with pytest.raises(PermissionError, match="Deferred job operation was denied") as error:
                async with jobs.start(session, request):
                    pytest.fail("Private host failure advanced")
            assert error.value.__context__ is None and error.value.__cause__ is None
    (row,) = journal.rows.values()
    assert row.update.status == "completed" and row.result is None
    assert backend.calls == [("start", 2)]


@pytest.mark.parametrize("first_stop", ["caller", "deadline"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_owned_job_joins_repeated_interruption_without_logging_cleanup(
    caplog, first_stop, cleanup_fails
):
    entered, rolling_back, release = (asyncio.Event() for _ in range(3))
    outcome = []
    marker = "c6N"

    async def work():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            rolling_back.set()
            await release.wait()
            if cleanup_fails:
                raise PermissionError(marker)
        return marker

    async def drive():
        try:
            await _wait_for_job(work(), 0.01 if first_stop == "deadline" else 10)
        except BaseException as error:
            outcome.append(error)

    async with debug_logs(caplog, marker):
        driver = asyncio.create_task(drive())
        try:
            await entered.wait()
            if first_stop == "caller":
                driver.cancel("first")
            await rolling_back.wait()
            driver.cancel("later")
            await asyncio.sleep(0)
            assert not driver.done()
        finally:
            release.set()
            await driver
        assert len(outcome) == 1
        if first_stop == "caller":
            assert isinstance(outcome[0], asyncio.CancelledError) and str(outcome[0]) == "first"
        else:
            assert isinstance(outcome[0], asyncio.TimeoutError)


async def test_restore_hook_nested_ordinary_open_uses_future_agent(responses):
    calls = []
    opening, future = callback("opening", calls), callback("future", calls)

    class PolicyAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                responses.respond(function_call("select", {}, "nested"))
                responses.respond(assistant_output("nested session"))
                async with future_agent.session() as nested:
                    assert (
                        await (await nested.submit("Select.")).get_result()
                    ).final_output == "nested session"
                responses.respond(function_call("select", {}, "restored"))
                responses.respond(assistant_output("finished"))
            async with super().session(**kwargs) as session:
                yield session

    jobs, tool = declared(Journal(), Provider([done()]))
    agent = PolicyAgent(instructions="Select.", tools=[tool, opening], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        request = await pending(session, tool, responses)
        future_agent = replace(agent, tools=(tool, future))
        async with jobs.start(session, request) as (_, turn):
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
    assert calls == ["future", "opening"]


async def test_restore_hook_can_deny_implicit_recovery(responses):
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)

    class PolicyAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                raise PermissionError("Restore denied")
            async with super().session(**kwargs) as session:
                yield session

    agent = PolicyAgent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        request = await pending(session, tool, responses)
        with pytest.raises(PermissionError, match="Restore denied"):
            async with jobs.start(session, request):
                pytest.fail("The hook's policy was bypassed")
    assert journal.state_json and not backend.calls


async def test_live_provider_credential_stays_private_after_future_provider_selection(responses):
    journal, backend = Journal(), Provider([done("a1")])
    jobs, tool = declared(journal, backend)
    original = NativeProvider(base_url=responses.url, api_key="a1")
    future = NativeProvider(base_url=responses.url, api_key="b2")
    agent = Agent(instructions="Render.", tools=[tool], provider=original)
    async with timeout(15), agent.session() as session:
        request = await pending(session, tool, responses)
        future_agent = replace(agent, provider=future)
        assert future_agent.provider is future
        responses.respond(assistant_output("finished"))
        with pytest.raises(ValueError, match="Deferred job operation could not be completed"):
            async with jobs.start(session, request):
                pytest.fail("The live provider credential was delivered")
    (row,) = journal.rows.values()
    assert row.update.status == "completed" and row.result is None
    assert backend.calls == [("start", 2)] and len(responses.requests) == 1
    await original.aclose()
    await future.aclose()


async def test_captured_hook_context_does_not_override_later_explicit_restore(responses):
    from contextvars import copy_context

    calls, captures = [], []
    opening, future = callback("opening", calls), callback("future", calls)

    class PolicyAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None and not captures:
                captures.append((copy_context(), kwargs["state"]))
            async with super().session(**kwargs) as session:
                yield session

    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    agent = PolicyAgent(instructions="Select.", tools=[tool, opening], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        request = await pending(session, tool, responses)
        future_agent = replace(agent, tools=(tool, future))
        responses.respond(function_call("select", {}, "implicit"))
        responses.respond(assistant_output("finished"))
        async with jobs.start(session, request) as (_, turn):
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
    captured, state = captures[0]

    async def explicit():
        responses.respond(function_call("select", {}, "explicit"))
        responses.respond(assistant_output("finished again"))
        async with future_agent.session(state=state) as restored:
            request = await next_request(restored.interactions(), tool)
            turn = await jobs.advance(restored, request)
            assert (
                isinstance(turn, Turn)
                and (await turn.get_result()).final_output == "finished again"
            )

    async with timeout(15):
        await captured.run(asyncio.create_task, explicit())
    assert calls == ["opening", "future"] and backend.calls == [("start", 2)]


async def test_session_hook_context_lifetime(responses):
    scope = ContextVar("session_scope", default=None)
    jobs, tool = declared(Journal(), Provider([done()]))
    agent = Agent(instructions="Work.", tools=[tool], provider=provider(responses))
    entries = []
    caller = asyncio.current_task()
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def scoped_session(**kwargs):
            owner = asyncio.current_task()
            token = scope.set("active")
            entries.append("entered")
            try:
                async with original(**kwargs) as restored:
                    yield restored
            finally:
                entries.append("same-task" if asyncio.current_task() is owner else "different-task")
                scope.reset(token)
                entries.append("released")

        agent.session = scoped_session
        responses.respond(assistant_output("finished"))
        async with jobs.start(live, request) as (_fresh, turn):
            assert asyncio.current_task() is caller and scope.get() is None
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
    assert scope.get() is None and entries == ["entered", "same-task", "released"]


@asynccontextmanager
async def scope_outcomes(monkeypatch, marker):
    """Inspect the scoped owner's repr and its neutral readiness signal."""
    from agents import Session

    loop = asyncio.get_running_loop()
    factory, create_future = loop.get_task_factory(), loop.create_future
    tasks, signals = [], []

    def capture_task(owner, work, **kwargs):
        task = (
            factory(owner, work, **kwargs)
            if factory is not None
            else asyncio.Task(work, loop=owner, **kwargs)
        )
        if getattr(work, "__qualname__", "").startswith("_restored_session."):
            tasks.append(task)
        return task

    def capture_future():
        signal = create_future()
        if inspect.currentframe().f_back.f_code.co_name == "_restored_session":
            signals.append(signal)
        return signal

    monkeypatch.setattr(Session, "__repr__", lambda self: marker)
    monkeypatch.setattr(loop, "create_future", capture_future)
    loop.set_task_factory(capture_task)
    try:
        yield tasks, signals
    finally:
        loop.set_task_factory(factory)
    assert len(tasks) == len(signals) == 1
    for task in tasks:
        assert task.done() and not task.cancelled() and task.exception() is None
        assert task.result() is None and marker not in repr(task)
    for signal in signals:
        assert marker not in repr(signal) and not signal.cancelled()
        if signal.done():
            assert signal.exception() is None and signal.result() is None


@pytest.mark.parametrize(
    "mode",
    ["propagate", "suppress", "replace", "exit-error", "cancel-suppress", "cancel-propagate"],
)
async def test_session_hook_exit_preserves_body_semantics(responses, caplog, monkeypatch, mode):
    marker = "h3N"
    scope = ContextVar("hook_scope", default="caller")
    body_error = (
        asyncio.CancelledError(marker) if mode.startswith("cancel") else RuntimeError(marker)
    )
    replacement = PermissionError(marker)
    received, scopes = [], []
    jobs, tool = declared(Journal(), Provider([done()]))
    context = object()
    agent = Agent(instructions="Work.", tools=[tool], provider=provider(responses))
    caller = asyncio.current_task()
    async with timeout(15), agent.session(context=context) as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            assert kwargs["context"] is context
            owner = asyncio.current_task()
            token = scope.set(marker)
            try:
                async with original(**kwargs) as restored:
                    scopes.append(restored)
                    try:
                        yield restored
                    except (RuntimeError, asyncio.CancelledError) as problem:
                        received.append(problem)
                        assert problem is body_error
                        if mode == "replace":
                            raise replacement  # noqa: B904 - exercise ordinary replacement context
                        if mode not in ("suppress", "cancel-suppress"):
                            raise
                    if mode == "exit-error":
                        raise replacement
            finally:
                assert scope.get() == marker and asyncio.current_task() is owner
                scope.reset(token)

        agent.session = hook
        responses.respond(assistant_output("finished"))
        finalized = []

        async def unrelated_generator():
            try:
                yield
            finally:
                finalized.append(True)

        async with (
            debug_logs(caplog, marker),
            scope_outcomes(monkeypatch, marker) as (owners, _signals),
        ):
            unrelated = unrelated_generator()
            await anext(unrelated)
            await asyncio.create_task(unrelated.aclose())
            assert finalized == [True] and not owners
            caught = None
            try:
                async with jobs.start(live, request) as (fresh, turn):
                    assert asyncio.current_task() is caller and scope.get() == "caller"
                    assert fresh is scopes[0] and fresh._agent is agent
                    assert (
                        isinstance(turn, Turn)
                        and (await turn.get_result()).final_output == "finished"
                    )
                    if mode != "exit-error":
                        raise body_error
            except BaseException as problem:
                caught = problem
            assert scope.get() == "caller"
            assert caught is (
                None
                if mode in ("suppress", "cancel-suppress")
                else body_error
                if mode in ("propagate", "cancel-propagate")
                else replacement
            )
            if mode == "replace":
                assert caught.__cause__ is None and caught.__context__ is body_error
            assert received == ([] if mode == "exit-error" else [body_error])
    assert scopes[0]._closed


@pytest.mark.parametrize(
    ("phase", "fail_cleanup"),
    [
        ("body-caller", False),
        ("exit-caller", False),
        ("entry-caller", False),
        ("entry-timeout", False),
        ("cross-caller", False),
        ("cross-timeout", False),
        ("entry-caller", True),
        ("entry-timeout", True),
    ],
)
async def test_session_hook_joins_repeated_interruption(
    responses, caplog, monkeypatch, phase, fail_cleanup
):
    from pathlib import Path

    marker = "c5R"
    scope = ContextVar("hook_scope", default="caller")
    entered, in_body, retiring, release = (asyncio.Event() for _ in range(4))
    hold = asyncio.Event()
    scopes, exits, outcomes = [], [], []
    jobs, tool = declared(Journal(), Provider([done()]))
    agent = Agent(instructions="Work.", tools=[tool], provider=provider(responses))
    loop = asyncio.get_running_loop()
    clock, offset = loop.time, 0.0
    async with timeout(30), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def hook(**kwargs):
            owner = asyncio.current_task()
            token = scope.set(marker)
            try:
                async with original(**kwargs) as restored:
                    scopes.append(restored)
                    entered.set()
                    try:
                        if phase.startswith(("entry", "cross")):
                            try:
                                await hold.wait()
                            except asyncio.CancelledError:
                                if not phase.startswith("cross"):
                                    raise
                        try:
                            yield restored
                        except BaseException as problem:
                            exits.append(problem)
                            raise
                    finally:
                        retiring.set()
                        await release.wait()
                        if fail_cleanup:
                            raise PermissionError(marker)
            finally:
                assert scope.get() == marker and asyncio.current_task() is owner
                scope.reset(token)

        agent.session = hook
        responses.respond(assistant_output("finished"))

        async def drive():
            try:
                async with jobs.start(live, request, timeout_seconds=10) as (_, turn):
                    assert scope.get() == "caller"
                    assert (
                        isinstance(turn, Turn)
                        and (await turn.get_result()).final_output == "finished"
                    )
                    in_body.set()
                    if phase == "body-caller":
                        await hold.wait()
            except BaseException as problem:
                outcomes.append(problem)

        async with debug_logs(caplog, marker), scope_outcomes(monkeypatch, marker):
            # Advance the shared restore deadline after the hook opens.
            if phase.endswith("timeout"):
                monkeypatch.setattr(loop, "time", lambda: clock() + offset)
            driver = asyncio.create_task(drive())
            try:
                gate = (
                    in_body
                    if phase == "body-caller"
                    else retiring
                    if phase == "exit-caller"
                    else entered
                )
                await gate.wait()
                if phase.endswith("timeout"):
                    offset += 11
                else:
                    driver.cancel("first")
                await retiring.wait()
                driver.cancel("later")
                await asyncio.sleep(0)
                assert not driver.done() and scope.get() == "caller"
                home = Path(scopes[0]._home.name)
                assert home.is_dir()
            finally:
                release.set()
                await driver
            assert len(outcomes) == len(scopes) == 1
            if phase.endswith("timeout"):
                assert isinstance(outcomes[0], TimeoutError)
                assert str(outcomes[0]) == "Deferred session restore timed out"
            else:
                assert isinstance(outcomes[0], asyncio.CancelledError)
                assert str(outcomes[0]) == "first"
            assert not home.exists() and scopes[0]._closed
            if phase.startswith(("cross", "body")):
                assert len(exits) == 1
                if phase.endswith("caller"):
                    assert exits[0] is outcomes[0]
            else:
                assert not exits
            assert scope.get() == "caller"


async def test_session_hook_entry_error_keeps_identity_and_private_outcomes(
    responses, caplog, monkeypatch
):
    marker = "e4K"
    failure = PermissionError(marker)
    scope = ContextVar("entry_scope", default=None)
    jobs, tool = declared(Journal(), Provider([]))
    agent = Agent(instructions="Work.", tools=[tool], provider=provider(responses))
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session

        @asynccontextmanager
        async def denied(**kwargs):
            token = scope.set(marker)
            try:
                if kwargs.get("state") is not None:
                    raise failure
                async with original(**kwargs) as restored:
                    yield restored
            finally:
                scope.reset(token)

        agent.session = denied
        async with debug_logs(caplog, marker), scope_outcomes(monkeypatch, marker):
            with pytest.raises(PermissionError) as caught:
                async with jobs.start(live, request):
                    pytest.fail("Denied hook entered")
            assert caught.value is failure and scope.get() is None


async def test_session_scope_cancelled_before_entry_does_not_open_hook(responses, caplog):
    marker = "n6T"
    jobs, tool = declared(Journal(), Provider([]))
    agent = Agent(instructions="Work.", tools=[tool], provider=provider(responses))
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)
        original = agent.session
        entered, tasks = [], []
        loop = asyncio.get_running_loop()
        factory = loop.get_task_factory()

        @asynccontextmanager
        async def hook(**kwargs):
            entered.append(True)
            async with original(**kwargs) as restored:
                yield restored

        def cancelled(owner, work, **kwargs):
            task = (
                factory(owner, work, **kwargs)
                if factory is not None
                else asyncio.Task(work, loop=owner, **kwargs)
            )
            if getattr(work, "__qualname__", "").startswith("_restored_session."):
                tasks.append(task)
                task.cancel(marker)
            return task

        finalized = []

        async def unrelated_generator():
            try:
                yield
            finally:
                finalized.append(True)

        agent.session = hook
        async with debug_logs(caplog, marker):
            try:
                loop.set_task_factory(cancelled)
                unrelated = unrelated_generator()
                await anext(unrelated)
                await asyncio.create_task(unrelated.aclose())
                assert finalized == [True] and not tasks
                with pytest.raises(asyncio.CancelledError):
                    async with jobs.start(live, request):
                        pytest.fail("Cancelled scope entered")
            finally:
                loop.set_task_factory(factory)
            assert not entered and len(tasks) == 1
            assert tasks[0].cancelled() and marker not in repr(tasks[0])
