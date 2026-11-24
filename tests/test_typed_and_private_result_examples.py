import asyncio
from contextlib import closing

import pytest

from agents import Agent, RunConfig, Runner, SQLiteSession
from agents.models.multi_provider import MultiProvider
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from examples.basic.typed_answer_repair import RepairLimitError, Verdict, repair
from examples.mcp.bounded_tool_output import main as reports_recipe


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_typed_semantic_repair_and_bounded_failure(streamed):
    invalid = assistant_message('{"decision":"approve","evidence_count":0}')
    valid = assistant_message('{"decision":"reject","evidence_count":2}')
    model = ScriptedModel([[invalid], [valid]])
    result, review = await repair(model, streamed=streamed)
    assert result == Verdict(decision="reject", evidence_count=2)
    assert review.attempts == len(model.calls) == 2
    assert model.calls[1].system_instructions is not None
    assert "positive evidence_count" in model.calls[1].system_instructions
    failed = ScriptedModel([[invalid], [invalid]])
    with pytest.raises(RepairLimitError, match="two candidates"):
        await repair(failed, streamed=streamed)
    assert len(failed.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("supplied", [False, True], ids=["default-provider", "caller-provider"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancellation"])
async def test_repair_provider_ownership(monkeypatch, streamed, supplied, outcome):
    closed = []
    close = MultiProvider.aclose

    async def record_close(provider):
        await close(provider)
        closed.append(provider)

    monkeypatch.setattr(MultiProvider, "aclose", record_close)
    config = RunConfig(tracing_disabled=False) if supplied else None
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(call):
        assert call.tracing.is_disabled()
        if outcome == "failure":
            raise RuntimeError("model failed")
        if outcome == "cancellation":
            entered.set()
            await release.wait()
        return [assistant_message('{"decision":"approve","evidence_count":1}')]

    model = ScriptedModel([ModelStep.respond(respond)])
    task = asyncio.create_task(repair(model, streamed=streamed, run_config=config))
    try:
        if outcome == "cancellation":
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "failure":
            with pytest.raises(RuntimeError, match="model failed"):
                await task
        else:
            answer, _ = await task
            assert answer == Verdict(decision="approve", evidence_count=1)
        if config is None:
            assert len(closed) == 1
        else:
            assert closed == []
            assert not config.tracing_disabled
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if config is not None:
            await config.model_provider.aclose()


@pytest.mark.asyncio
async def test_large_private_source_and_sensitive_error_never_enter_history(monkeypatch):
    secret = "credential-secret " * 100

    async def source():
        return secret

    monkeypatch.setattr(reports_recipe, "site_report", source)
    with (
        closing(reports_recipe.Reports("owner")) as reports,
        closing(SQLiteSession("private")) as session,
    ):
        model = ScriptedModel(
            [
                [function_call("inspect_site", {}, call_id="one")],
                [assistant_message("Public summary")],
            ]
        )
        result = await Runner.run(
            Agent(name="demo", tools=[reports.site_tool()], model=model),
            "Synthetic prompt",
            context="owner",
            session=session,
            run_config=RunConfig(tracing_disabled=True),
        )
        assert "credential-secret" not in str(await session.get_items())
        assert "credential-secret" not in str(model.calls[1].input)
        assert "credential-secret" not in str(result.final_output)
        reference, _, _ = reports.saved("owner")[0]
        assert reports.fetch("owner", reference).startswith("credential-secret")
        with pytest.raises(PermissionError):
            reports.fetch("other", reference)
    with pytest.raises(PermissionError):
        reports.fetch("owner", reference)

    async def failure():
        raise RuntimeError("credential-secret")

    monkeypatch.setattr(reports_recipe, "site_report", failure)
    with closing(reports_recipe.Reports("owner")) as reports:
        model = ScriptedModel(
            [
                [function_call("inspect_site", {}, call_id="one")],
                [assistant_message("Public error summary")],
            ]
        )
        result = await Runner.run(
            Agent(name="demo", tools=[reports.site_tool()], model=model),
            "Synthetic prompt",
            context="owner",
            run_config=RunConfig(tracing_disabled=True),
        )
        assert "credential-secret" not in str(result.new_items)
        assert "Inspection report unavailable" in str(model.calls[1].input)
        assert reports.saved("owner") == ()


@pytest.mark.asyncio
async def test_schema_invalid_repair_never_replays_sensitive_candidate():
    from examples.basic.typed_answer_repair import repair_schema

    model = ScriptedModel(
        [
            [assistant_message('{"decision":"credential-secret","evidence_count":"bad"}')],
            [assistant_message('{"decision":"reject","evidence_count":1}')],
        ]
    )
    answer, attempts = await repair_schema(model)
    assert answer == Verdict(decision="reject", evidence_count=1) and attempts == 2
    assert "credential-secret" not in str(model.calls[1].input)
    failed = ScriptedModel([[assistant_message("credential-secret")]] * 2)
    with pytest.raises(RepairLimitError, match="two attempts") as error:
        await repair_schema(failed)
    assert "credential-secret" not in str(error.value) and len(failed.calls) == 2


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(asyncio, "eager_task_factory"), reason="Requires Python 3.12+")
@pytest.mark.parametrize("cancel_during_run", [False, True])
async def test_streamed_repair_joins_eager_run(monkeypatch, cancel_during_run):
    entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closing, allow_close, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    close = MultiProvider.aclose

    async def slow_close(provider):
        closing.set()
        await allow_close.wait()
        await close(provider)
        closed.set()

    monkeypatch.setattr(MultiProvider, "aclose", slow_close)

    async def respond(call):
        entered.set()
        try:
            await release.wait()
            return [assistant_message('{"decision":"approve","evidence_count":1}')]
        finally:
            settled.set()

    loop = asyncio.get_running_loop()
    previous = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        task = asyncio.create_task(
            repair(ScriptedModel([ModelStep.respond(respond)]), streamed=True)
        )
    finally:
        loop.set_task_factory(previous)
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not task.done()
        if cancel_during_run:
            task.cancel("first")
        else:
            release.set()
        await asyncio.wait_for(closing.wait(), timeout=5)
        assert settled.is_set()
        if cancel_during_run:
            task.cancel("second")
            await asyncio.sleep(0)
        assert not task.done()
        allow_close.set()
        if cancel_during_run:
            with pytest.raises(asyncio.CancelledError, match="first"):
                await task
        else:
            answer, review = await task
            assert answer == Verdict(decision="approve", evidence_count=1)
            assert review.attempts == 1
        assert closed.is_set()
    finally:
        release.set()
        allow_close.set()
        await asyncio.gather(task, return_exceptions=True)
