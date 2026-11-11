"""Provider-hosted shell wire, observations and cold replay."""

from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, HostedShell, NativeProvider, SessionState
from agents._events import ShellCallItem, ShellCallOutputItem
from agents.observation import (
    HostedToolCallProgress,
    HostedToolCallSummary,
    ModelOutputItemCompleted,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses


@pytest.mark.parametrize("include_text", [False, True])
async def test_native_hosted_shell_returns_produced_call_and_output(
    responses: ResponsesFixture, include_text: bool
) -> None:
    call = {
        "id": "sh_produced_call",
        "type": "shell_call",
        "call_id": "shell-produced",
        "status": "completed",
        "action": {"commands": ["printf provider"]},
    }
    output = {
        "id": "sh_produced_output",
        "type": "shell_call_output",
        "call_id": "shell-produced",
        "status": "completed",
        "output": [
            {"stdout": "provider", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
        ],
    }
    frames = [
        {"type": "response.output_item.done", "output_index": 0, "item": call},
        {"type": "response.output_item.done", "output_index": 1, "item": output},
    ]
    if include_text:
        frames.append(assistant_output("provider complete"))
    responses.respond(*frames)
    agent = Agent(
        instructions="Use the hosted shell.",
        model="gpt-5.5",
        tools=[HostedShell(environment={"type": "container_auto"})],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-produced-shell"),
    )
    async with timeout(30):
        completed = await agent.run("run in the provider container", return_items=True)
    assert completed.items is not None
    assert len(completed.items) == 2 + int(include_text)
    assert all(entry.agent is agent for entry in completed.items)
    call_item, output_item = completed.items[0].item, completed.items[1].item
    assert call_item["type"] == "shell_call"
    assert output_item["type"] == "shell_call_output"
    assert call_item["call_id"] == "shell-produced"
    assert call_item["action"] == call["action"]
    assert output_item["call_id"] == "shell-produced"
    assert output_item["output"] == output["output"]
    if include_text:
        assert completed.items[2].item["type"] == "message"
    assert len(responses.requests) == 1


@pytest.mark.parametrize(
    "environment",
    [
        {
            "type": "container_auto",
            "file_ids": ["file-script"],
            "memory_limit": "4g",
            "skills": [{"type": "skill_reference", "skill_id": "skill-test", "version": "2"}],
            "network_policy": {"type": "disabled"},
        },
        {"type": "container_reference", "container_id": "cntr-shell"},
    ],
)
async def test_native_hosted_shell_preserves_wire_progress_and_cold_replay(
    responses: ResponsesFixture,
    environment: Any,
) -> None:
    call = {
        "id": "sh_command",
        "type": "shell_call",
        "call_id": "shell-call",
        "status": "completed",
        "action": {"commands": ["printf provider"], "timeout_ms": 1000},
        "future_call": {"keep": True},
    }
    output = {
        "id": "sh_receipt",
        "type": "shell_call_output",
        "call_id": "shell-call",
        "status": "completed",
        "output": [
            {"stdout": "provider", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
        ],
        "future_output": [1, None],
    }
    progress = [
        {
            "type": "response.shell_call_command.delta",
            "item_id": "sh_command",
            "output_index": 0,
            "command_index": 0,
            "delta": "printf provider",
        },
        {
            "type": "response.shell_call_output_content.delta",
            "item_id": "sh_receipt",
            "output_index": 1,
            "content_index": 0,
            "delta": {"stdout": "provider", "stderr": ""},
        },
    ]
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**call, "status": "in_progress"},
        },
        progress[0],
        {"type": "response.output_item.done", "output_index": 0, "item": call},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**output, "status": "in_progress"},
        },
        progress[1],
        {"type": "response.output_item.done", "output_index": 1, "item": output},
        assistant_output("provider complete"),
    )
    api_key = "synthetic-hosted-shell-private-key"
    tool = HostedShell(
        environment=environment, allowed_callers=["direct"], provider_data={"future_option": True}
    )
    agent = Agent(
        instructions="Use the hosted shell.",
        model="gpt-5.5",
        tools=[tool],
        provider=NativeProvider(base_url=responses.url, api_key=api_key),
    )
    async with timeout(30), agent.session() as session:
        turn = await session.submit("run in the provider container")
        assert (await turn.get_result()).final_output == "provider complete"
        detailed = [event async for event in turn.events(include_execution_details=True)]
        ordinary = [event async for event in turn.events()]
        shell_call: ShellCallItem | None = None
        shell_output: ShellCallOutputItem | None = None
        for event in detailed:
            if isinstance(event, ModelOutputItemCompleted):
                item = event.item
                assert not isinstance(item, HostedToolCallSummary)
                if item["type"] == "shell_call":
                    shell_call = item
                elif item["type"] == "shell_call_output":
                    shell_output = item
        assert shell_call is not None and shell_output is not None
        assert [shell_call["provider_data"], shell_output["provider_data"]] == [call, output]
        assert shell_call["action"] == call["action"]
        assert shell_output["output"] == output["output"]
        assert [
            event.event for event in detailed if isinstance(event, HostedToolCallProgress)
        ] == progress
        summaries = [
            event.item for event in ordinary if isinstance(event, ModelOutputItemCompleted)
        ]
        assert [item.type for item in summaries if isinstance(item, HostedToolCallSummary)] == [
            "shell_call",
            "shell_call_output",
        ]
        assert all(
            set(event.event) == {"type"}
            for event in ordinary
            if isinstance(event, HostedToolCallProgress)
        )
        state = (await session.save_state()).to_json()
        assert api_key not in state
    assert next(spec for spec in responses.requests[0]["tools"] if spec["type"] == "shell") == {
        "type": "shell",
        "environment": environment,
        "allowed_callers": ["direct"],
        "future_option": True,
    }
    responses.respond(assistant_output("replayed"))
    async with timeout(30), agent.session(state=SessionState.from_json(state)) as restored:
        assert (await (await restored.submit("continue")).get_result()).final_output == "replayed"
    replayed = [
        item
        for item in responses.requests[1]["input"]
        if item.get("type") in {"shell_call", "shell_call_output"}
    ]
    assert replayed == [call, output]
    assert len(responses.requests) == 2


@pytest.mark.parametrize("kind", ["shell_call", "shell_call_output"])
async def test_native_hosted_shell_can_complete_without_assistant_text(
    responses: ResponsesFixture,
    kind: str,
) -> None:
    raw: dict[str, Any] = {
        "id": "shell-only",
        "type": kind,
        "call_id": "shell-only-call",
        "status": "completed",
        **(
            {"action": {"commands": ["printf provider"]}}
            if kind == "shell_call"
            else {
                "output": [
                    {
                        "stdout": "provider",
                        "stderr": "",
                        "outcome": {"type": "exit", "exit_code": 0},
                    }
                ]
            }
        ),
    }
    responses.respond({"type": "response.output_item.done", "output_index": 0, "item": raw})
    agent = Agent(
        instructions="Use the hosted shell without a text answer.",
        model="gpt-5.5",
        tools=[HostedShell(environment={"type": "container_auto"})],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-shell-only"),
    )
    async with timeout(30), agent.session() as session:
        turn = await session.submit("run in the provider")
        await turn.get_result()
        items = [item async for item in session.items()]
        matched = [
            item["provider_data"]
            for item in items
            if item["type"] == "shell_call" or item["type"] == "shell_call_output"
        ]
        assert matched == [raw]
    assert len(responses.requests) == 1
