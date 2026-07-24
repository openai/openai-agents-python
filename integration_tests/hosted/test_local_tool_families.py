from __future__ import annotations

from typing import Any

import pytest

from agents import (
    Agent,
    CustomTool,
    ModelSettings,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    ShellCommandRequest,
    ShellTool,
    ToolCallOutputItem,
)
from agents.tool_context import ToolContext

pytestmark = pytest.mark.hosted


@pytest.mark.parametrize(
    "streaming",
    [False, pytest.param(True, marks=pytest.mark.nightly)],
    ids=["nonstreaming", "streaming"],
)
async def test_custom_tools_preserve_raw_string_inputs_and_outputs(
    integration_model: str,
    streaming: bool,
) -> None:
    raw_inputs: list[str] = []

    async def format_release_word(_context: ToolContext[Any], raw_input: str) -> str:
        raw_inputs.append(raw_input)
        return raw_input.strip().upper()

    custom = CustomTool(
        name="format_release_word",
        description="Convert the raw release word to uppercase.",
        on_invoke_tool=format_release_word,
    )
    agent = Agent(
        name="Packaged raw custom tool agent",
        model=integration_model,
        instructions=(
            "Call format_release_word with exactly the raw string amber, "
            "then reply exactly CUSTOM:AMBER."
        ),
        tools=[custom],
        model_settings=ModelSettings(tool_choice="required", max_tokens=256),
    )
    config = RunConfig(tracing_disabled=True)
    result: RunResult | RunResultStreaming
    if streaming:
        result = Runner.run_streamed(agent, "Format the release word.", run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "Format the release word.", run_config=config)

    outputs = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert len(raw_inputs) == 1
    assert raw_inputs[0].strip() == "amber"
    assert result.final_output == "CUSTOM:AMBER"
    assert len(outputs) == 1
    assert isinstance(outputs[0].raw_item, dict)
    assert outputs[0].raw_item["type"] == "custom_tool_call_output"


@pytest.mark.nightly
@pytest.mark.parametrize("approved", [False, True], ids=["rejected", "approved"])
async def test_custom_tool_approval_survives_serialized_resume(
    integration_model: str,
    approved: bool,
) -> None:
    calls: list[str] = []

    async def publish_release(_context: ToolContext[Any], raw_input: str) -> str:
        calls.append(raw_input)
        return "CUSTOM_APPROVED"

    custom = CustomTool(
        name="publish_release_note",
        description="Publish the raw release note after operator approval.",
        on_invoke_tool=publish_release,
        needs_approval=True,
    )
    agent = Agent(
        name="Packaged approval-gated custom tool agent",
        model=integration_model,
        instructions=(
            "Call publish_release_note with the raw string amber. If approved reply exactly "
            "CUSTOM_APPROVED; if rejected reply exactly CUSTOM_REJECTED."
        ),
        tools=[custom],
        model_settings=ModelSettings(tool_choice="required", max_tokens=320),
    )
    config = RunConfig(tracing_disabled=True)
    first = await Runner.run(agent, "Publish the release note.", run_config=config, max_turns=5)
    assert len(first.interruptions) == 1

    state = await RunState.from_json(agent, first.to_state().to_json())
    if approved:
        state.approve(state.get_interruptions()[0])
    else:
        state.reject(state.get_interruptions()[0], rejection_message="Publication was declined.")

    resumed = await Runner.run(agent, state, run_config=config, max_turns=5)
    outputs = [item for item in resumed.new_items if isinstance(item, ToolCallOutputItem)]
    assert calls == (["amber"] if approved else [])
    assert resumed.final_output == ("CUSTOM_APPROVED" if approved else "CUSTOM_REJECTED")
    assert any(
        isinstance(item.raw_item, dict) and item.raw_item.get("type") == "custom_tool_call_output"
        for item in outputs
    )


@pytest.mark.parametrize(
    "streaming",
    [False, pytest.param(True, marks=pytest.mark.nightly)],
    ids=["nonstreaming", "streaming"],
)
async def test_local_shell_tools_execute_only_the_supplied_safe_harness(
    integration_model: str,
    streaming: bool,
) -> None:
    requested_commands: list[list[str]] = []

    def execute_shell(request: ShellCommandRequest) -> str:
        requested_commands.append(request.data.action.commands)
        return "SHELL_CHECKPOINT_READY"

    agent = Agent(
        name="Packaged local shell tool agent",
        model=integration_model,
        instructions=(
            "Call the shell tool with exactly the command echo release, "
            "then reply exactly SHELL_READY."
        ),
        tools=[ShellTool(executor=execute_shell)],
        model_settings=ModelSettings(tool_choice="required", max_tokens=256),
    )
    config = RunConfig(tracing_disabled=True)
    result: RunResult | RunResultStreaming
    if streaming:
        result = Runner.run_streamed(agent, "Check the release with shell.", run_config=config)
        async for _event in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, "Check the release with shell.", run_config=config)

    outputs = [item for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert requested_commands == [["echo release"]]
    assert result.final_output == "SHELL_READY"
    assert len(outputs) == 1
    assert isinstance(outputs[0].raw_item, dict)
    assert outputs[0].raw_item["type"] == "shell_call_output"


@pytest.mark.nightly
@pytest.mark.parametrize("approved", [False, True], ids=["rejected", "approved"])
async def test_local_shell_approval_survives_serialized_resume(
    integration_model: str,
    approved: bool,
) -> None:
    requested_commands: list[list[str]] = []

    def execute_shell(request: ShellCommandRequest) -> str:
        requested_commands.append(request.data.action.commands)
        return "SHELL_APPROVED"

    agent = Agent(
        name="Packaged approval-gated shell agent",
        model=integration_model,
        instructions=(
            "Call the shell tool with exactly the command echo release. If approved reply "
            "exactly SHELL_APPROVED; if rejected reply exactly SHELL_REJECTED."
        ),
        tools=[ShellTool(executor=execute_shell, needs_approval=True)],
        model_settings=ModelSettings(tool_choice="required", max_tokens=320),
    )
    config = RunConfig(tracing_disabled=True)
    first = await Runner.run(agent, "Check the release with shell.", run_config=config, max_turns=5)
    assert len(first.interruptions) == 1

    state = await RunState.from_json(agent, first.to_state().to_json())
    if approved:
        state.approve(state.get_interruptions()[0])
    else:
        state.reject(state.get_interruptions()[0], rejection_message="Shell access was declined.")

    resumed = await Runner.run(agent, state, run_config=config, max_turns=5)
    assert requested_commands == ([["echo release"]] if approved else [])
    assert resumed.final_output == ("SHELL_APPROVED" if approved else "SHELL_REJECTED")
    assert any(
        isinstance(item, ToolCallOutputItem)
        and isinstance(item.raw_item, dict)
        and item.raw_item.get("type") == "shell_call_output"
        for item in resumed.new_items
    )
