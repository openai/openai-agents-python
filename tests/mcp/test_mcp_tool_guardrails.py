from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, RunConfig, RunContextWrapper, Runner, ToolExecutionConfig
from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp, MCPUtil
from agents.testing import ScriptedModel
from agents.tool import FunctionTool
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailData,
    ToolOutputGuardrail,
    ToolOutputGuardrailData,
    tool_input_guardrail,
    tool_output_guardrail,
)

from ..test_responses import get_function_tool_call, get_text_message
from .helpers import FakeMCPServer


def _allow_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput.allow()


def _allow_output(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput.allow()


_INPUT_GUARDRAIL = ToolInputGuardrail(_allow_input)
_OUTPUT_GUARDRAIL = ToolOutputGuardrail(_allow_output)


def test_local_mcp_transport_constructors_accept_tool_guardrails() -> None:
    servers = [
        MCPServerStdio(
            params={"command": "python"},
            tool_input_guardrails=[_INPUT_GUARDRAIL],
            tool_output_guardrails=[_OUTPUT_GUARDRAIL],
        ),
        MCPServerSse(
            params={"url": "https://example.test/sse"},
            tool_input_guardrails=[_INPUT_GUARDRAIL],
            tool_output_guardrails=[_OUTPUT_GUARDRAIL],
        ),
        MCPServerStreamableHttp(
            params={"url": "https://example.test/mcp"},
            tool_input_guardrails=[_INPUT_GUARDRAIL],
            tool_output_guardrails=[_OUTPUT_GUARDRAIL],
        ),
    ]

    for server in servers:
        assert server.tool_input_guardrails == [_INPUT_GUARDRAIL]
        assert server.tool_output_guardrails == [_OUTPUT_GUARDRAIL]


@pytest.mark.asyncio
async def test_mcp_guardrail_mapping_uses_original_tool_name_before_public_prefixing() -> None:
    server = FakeMCPServer(
        server_name="billing",
        tool_input_guardrails={"charge": [_INPUT_GUARDRAIL]},
        tool_output_guardrails={"charge": [_OUTPUT_GUARDRAIL]},
    )
    server.add_tool("charge", {})
    server.add_tool("lookup", {})

    tools = await MCPUtil.get_function_tools(
        server,
        False,
        RunContextWrapper(context=None),
        Agent(name="test", instructions="test"),
        include_server_in_tool_names=True,
    )
    by_name = {tool.name: tool for tool in tools if isinstance(tool, FunctionTool)}

    charge_tool = by_name["mcp_billing__charge"]
    lookup_tool = by_name["mcp_billing__lookup"]
    assert charge_tool.tool_input_guardrails == [_INPUT_GUARDRAIL]
    assert charge_tool.tool_output_guardrails == [_OUTPUT_GUARDRAIL]
    assert not lookup_tool.tool_input_guardrails
    assert not lookup_tool.tool_output_guardrails


async def _run_agent(agent: Agent[Any], *, streaming: bool):
    if streaming:
        result = Runner.run_streamed(agent, input="use the tool")
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, input="use the tool")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_local_mcp_guardrails_run_through_function_tool_pipeline(streaming: bool) -> None:
    events: list[tuple[str, str]] = []

    @tool_input_guardrail
    def input_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        events.append(("input", data.context.tool_name))
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    def output_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        events.append(("output", data.context.tool_name))
        return ToolGuardrailFunctionOutput.allow()

    server = FakeMCPServer(
        tool_input_guardrails=[input_guardrail],
        tool_output_guardrails=[output_guardrail],
    )
    server.add_tool("search", {})

    model = ScriptedModel(
        steps=[
            [get_function_tool_call("search", "{}", call_id="call-search")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="test", model=model, mcp_servers=[server])

    result = await _run_agent(agent, streaming=streaming)

    assert server.tool_calls == ["search"]
    assert events == [("input", "search"), ("output", "search")]
    assert len(result.tool_input_guardrail_results) == 1
    assert len(result.tool_output_guardrail_results) == 1


@pytest.mark.asyncio
async def test_local_mcp_input_guardrail_can_reject_before_server_call() -> None:
    @tool_input_guardrail
    def reject_input(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.reject_content("blocked by MCP input guardrail")

    server = FakeMCPServer(tool_input_guardrails=[reject_input])
    server.add_tool("delete", {})

    model = ScriptedModel(
        steps=[
            [get_function_tool_call("delete", "{}", call_id="call-delete")],
            [get_text_message("done")],
        ]
    )
    result = await Runner.run(
        Agent(name="test", model=model, mcp_servers=[server]),
        input="delete",
    )

    assert server.tool_calls == []
    assert len(result.tool_input_guardrail_results) == 1
    assert result.tool_input_guardrail_results[0].output.behavior["type"] == "reject_content"


@pytest.mark.asyncio
async def test_local_mcp_guardrails_follow_existing_preapproval_semantics() -> None:
    calls: list[str] = []

    @tool_input_guardrail
    def input_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        calls.append(data.context.tool_name)
        return ToolGuardrailFunctionOutput.allow()

    server = FakeMCPServer(
        require_approval="always",
        tool_input_guardrails=[input_guardrail],
    )
    server.add_tool("charge", {})

    model = ScriptedModel(
        steps=[
            [get_function_tool_call("charge", "{}", call_id="call-charge")],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="test", model=model, mcp_servers=[server])
    config = RunConfig(tool_execution=ToolExecutionConfig(pre_approval_tool_input_guardrails=True))

    first = await Runner.run(agent, "charge", run_config=config)
    assert first.interruptions
    assert calls == ["charge"]
    assert server.tool_calls == []

    state = first.to_state()
    state.approve(state.get_interruptions()[0])
    resumed = await Runner.run(agent, state, run_config=config)

    assert resumed.final_output == "done"
    assert calls == ["charge", "charge"]
    assert server.tool_calls == ["charge"]
    assert len(resumed.tool_input_guardrail_results) == 1
