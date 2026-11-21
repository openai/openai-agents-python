from __future__ import annotations

import asyncio
import json

import pytest

from agents import Agent, RunConfig, Runner
from agents.decorators import tool
from agents.memory.resumable_session import ResumableSession
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tracing import generation_span
from agents.tracing.config import get_operation_detail
from tests.memory.test_resumable_session import MemoryStore
from tests.testing_processor import fetch_ordered_spans


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("mode", ["default", "metadata", "content", "veto", "disabled"])
async def test_resumable_storage_uses_current_run_capture_policy(streamed: bool, mode: str) -> None:
    observations: list[tuple[str, str | None]] = []

    class Store(MemoryStore):
        def observe(self, phase: str) -> None:
            observations.append((phase, get_operation_detail()))
            with generation_span(model="checkpoint", input=[{"content": "CHECKPOINT_CANARY"}]):
                pass

        async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
            self.observe(json.loads(value)["status"])
            await super().save(session_id, value, expected=expected)
            # Exercise acknowledged-write recovery through the adjacent load callback.
            return False

        async def load(self, session_id: str) -> str | None:
            self.observe("load")
            return await super().load(session_id)

    store = Store()
    session = await ResumableSession.create(store)
    observations.clear()
    prior_spans = {s.span_id for s in fetch_ordered_spans()}
    inner_model = ScriptedModel([[assistant_message("done")]])
    inner = Agent(name="inner", model=inner_model)
    config = RunConfig(
        tracing={}
        if mode == "default"
        else {"operation_detail": "metadata" if mode == "metadata" else "content"},
        trace_include_sensitive_data=mode not in {"metadata", "veto"},
        tracing_disabled=mode == "disabled",
    )

    outer_detail = "metadata" if mode == "content" else "content"

    @tool
    async def nested() -> str:
        if streamed:
            result = Runner.run_streamed(inner, "question", session=session, run_config=config)
            async for _ in result.stream_events():
                pass
        else:
            result = await Runner.run(inner, "question", session=session, run_config=config)
        assert get_operation_detail() == outer_detail
        return str(result.final_output)

    outer = Agent(
        name="outer",
        tools=[nested],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel([[function_call("nested", {}, call_id="nested-call")]]),
    )
    result = await Runner.run(
        outer, "run", run_config=RunConfig(tracing={"operation_detail": outer_detail})
    )
    assert result.final_output == "done"
    assert store.checkpoint(session.session_id)["status"] == "ready"
    assert len(inner_model.calls) == 1
    expected = None if mode == "default" else "content" if mode == "content" else "metadata"
    checkpoints = [
        s
        for s in fetch_ordered_spans()
        if s.span_id not in prior_spans and getattr(s.span_data, "model", None) == "checkpoint"
    ]
    assert len(checkpoints) == (0 if mode == "disabled" else 4)
    for span in checkpoints:
        if mode in {"metadata", "veto"}:
            assert span.span_data.input is None
        else:
            assert "CHECKPOINT_CANARY" in json.dumps(span.export())
        assert span.span_data.operation.detail == expected
    assert observations == [(phase, expected) for phase in ["active", "load", "ready", "load"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_resumable_initial_claim_settles_before_cancellation(streamed: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Store(MemoryStore):
        async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
            if json.loads(value)["status"] == "active":
                entered.set()
                await release.wait()
            return await super().save(session_id, value, expected=expected)

    store = Store()
    session = await ResumableSession.create(store)
    model = ScriptedModel([[assistant_message("unused")]])
    agent = Agent(name="inner", model=model)
    config = RunConfig(tracing={"operation_detail": "metadata"}, trace_include_sensitive_data=False)
    if streamed:
        result = Runner.run_streamed(agent, "question", session=session, run_config=config)
        assert result.run_loop_task
        task = result.run_loop_task
    else:
        task = asyncio.create_task(
            Runner.run(agent, "question", session=session, run_config=config)
        )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if streamed:
            result.cancel()
        else:
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 5)
        assert store.checkpoint(session.session_id)["status"] == "active"
        assert not model.calls
        if streamed:
            assert result.is_complete
            assert [event async for event in result.stream_events()] == []
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
