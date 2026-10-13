"""Approval persistence must accept the names emitted by provider model dumps."""

from __future__ import annotations

import pytest
from openai.types.responses import ResponseFunctionToolCall

from agents import Agent, RunConfig, Runner, RunState
from agents.decorators import tool
from agents.testing import ModelStep, ScriptedModel

from .test_responses import get_text_message


@pytest.mark.asyncio
@pytest.mark.skipif(
    "async_" not in ResponseFunctionToolCall.model_fields,
    reason="Older supported openai versions do not declare the async alias",
)
@pytest.mark.parametrize("approve", [True, False], ids=["approve", "reject"])
@pytest.mark.parametrize(
    ("serialization", "async_value"),
    [("live", None), ("json", None), ("string", None), ("json", False), ("string", True)],
)
async def test_function_call_alias_survives_approval_resume(
    serialization: str, async_value: bool | None, approve: bool
) -> None:
    executions: list[str] = []

    @tool(needs_approval=True)
    async def needs_ok() -> str:
        executions.append("called")
        return "approved output"

    call = ResponseFunctionToolCall.model_validate(
        {
            "id": "fc_alias",
            "call_id": "call_alias",
            "type": "function_call",
            "name": "needs_ok",
            "arguments": "{}",
            "async": async_value,
        }
    )
    model = ScriptedModel([ModelStep(output=[call]), ModelStep(output=[get_text_message("done")])])
    agent = Agent(name="Alias approval", model=model, tools=[needs_ok])
    config = RunConfig(tracing_disabled=True)
    result = await Runner.run(agent, "start", run_config=config)
    assert len(result.interruptions) == 1
    state = result.to_state()
    if serialization == "json":
        saved = state.to_json()
        # Released snapshots use Python field names. Keep that durable representation readable.
        assert (
            saved["current_step"]["data"]["interruptions"][0]["raw_item"]["async_"] is async_value
        )
        saved["$schemaVersion"] = "1.18"
        state = await RunState.from_json(agent, saved)
    elif serialization == "string":
        state = await RunState.from_string(agent, state.to_string())

    interruption = state.get_interruptions()[0]
    assert isinstance(interruption.raw_item, ResponseFunctionToolCall)
    assert interruption.raw_item.async_ is async_value
    assert "async_" in interruption.raw_item.model_fields_set
    assert "async_" not in (interruption.raw_item.model_extra or {})
    if approve:
        state.approve(interruption)
    else:
        state.reject(interruption)
    resumed = await Runner.run(agent, state, run_config=config)
    assert resumed.final_output == "done"
    assert resumed.interruptions == []
    assert executions == (["called"] if approve else [])
    restored_call = resumed.raw_responses[0].output[0]
    assert isinstance(restored_call, ResponseFunctionToolCall)
    assert restored_call.async_ is async_value
    assert "async_" not in (restored_call.model_extra or {})
    model.assert_complete()
