from __future__ import annotations

import logging

import pytest

import agents._debug as _debug
from agents import Agent, RunConfig
from agents.items import ToolCallOutputItem
from agents.run import AgentRunner
from agents.run_context import RunContextWrapper
from agents.run_state import RunState
from tests.fake_model import FakeModel
from tests.test_responses import get_text_message


@pytest.mark.asyncio
async def test_run_streamed_resume_omits_tool_output_in_log_when_dont_log(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)

    model = FakeModel()
    model.set_next_output([get_text_message("ok")])
    agent = Agent(name="log-agent", model=model)
    context_wrapper: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    state = RunState(
        context=context_wrapper,
        original_input="hi",
        starting_agent=agent,
        max_turns=1,
    )

    raw_output = {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "secret",
    }
    state._generated_items = [ToolCallOutputItem(agent=agent, raw_item=raw_output, output="secret")]

    caplog.set_level(logging.DEBUG, logger="openai.agents")

    runner = AgentRunner()
    streamed_result = runner.run_streamed(agent, state, run_config=RunConfig())
    async for _event in streamed_result.stream_events():
        pass

    record = next(
        (
            rec
            for rec in caplog.records
            if "Resuming from RunState in run_streaming()" in rec.message
        ),
        None,
    )
    assert record is not None
    details = getattr(record, "generated_items_details", [])
    assert details
    assert "output" not in details[0]
