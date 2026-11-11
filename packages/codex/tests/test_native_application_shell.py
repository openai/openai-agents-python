"""Application-managed Shell over the real native Responses transport."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from agents import (
    Agent,
    Allow,
    NativeError,
    ReplaceArguments,
    SessionState,
    ShellCall,
    ShellCommandOutput,
    ShellExit,
    ShellOutput,
    ShellSkill,
    ToolContext,
    ToolOutput,
    shell_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_manual_tools import next_request, provider


def shell_call(**fields: Any) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "id": "sh_native_application",
            "type": "shell_call",
            "call_id": "shell-local",
            "action": {"commands": ["printf sample"], "max_output_length": 5, "timeout_ms": 250},
            **fields,
        },
    }


@pytest.mark.parametrize("approved", [True, False])
async def test_application_shell_gates_execution_and_replays_typed_output(
    responses: ResponsesFixture, approved: bool
) -> None:
    effects: list[tuple[str, ShellCall]] = []

    def execute(context: ToolContext[str], call: ShellCall) -> ShellOutput:
        effects.append((context.context, call))
        return ShellOutput(
            max_output_length=3,
            output=[
                ShellCommandOutput(
                    stdout="é🙂long", stderr="details", outcome=ShellExit(exit_code=0)
                )
            ],
        )

    skill: ShellSkill = {
        "name": "example",
        "description": "Synthetic test skill",
        "path": "/workspace/SKILL.md",
    }
    tool = shell_tool(execute, skills=[skill], needs_approval=True, before=lambda _: Allow())
    frame = shell_call()
    responses.respond(frame)
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        model="gpt-5",
        provider=provider(responses),
        tools=[tool],
    )

    async with timeout(30), agent.session(context="application-context") as session:
        turn = await session.submit("run the test command")
        request = await next_request(session.interactions(), tool)
        assert request.call_id == "shell-local"
        assert effects == [] and len(responses.requests) == 1
        if approved:
            await request.approve()
        else:
            await request.reject("deny")
        assert (await turn.get_result()).final_output == "done"
        state = SessionState.from_json((await session.save_state()).to_json())

    shell_specs = [spec for spec in responses.requests[0]["tools"] if spec["type"] == "shell"]
    assert shell_specs == [{"type": "shell", "environment": {"type": "local", "skills": [skill]}}]
    assert not any(
        spec.get("name") == "shell"
        for spec in responses.requests[0]["tools"]
        if spec["type"] == "function"
    )

    output = next(
        item for item in responses.requests[1]["input"] if item["type"] == "shell_call_output"
    )
    assert output["call_id"] == "shell-local"
    if approved:
        assert len(effects) == 1
        context, call = effects[0]
        assert context == "application-context"
        assert call.call_id == "shell-local" and call.status is None
        assert call.action.commands == ["printf sample"] and call.action.timeout_ms == 250
        assert output["max_output_length"] == 3
        assert output["output"] == [
            {"stdout": "é🙂l", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
        ]
    else:
        assert effects == []
        assert output["output"] == [
            {"stdout": "", "stderr": "deny", "outcome": {"type": "exit", "exit_code": 1}}
        ]

    responses.respond(assistant_output("replayed"))
    async with timeout(30), agent.session(context="application-context", state=state) as restored:
        turn = await restored.submit("continue")
        assert (await turn.get_result()).final_output == "replayed"
    assert len(effects) == int(approved)
    replayed = responses.requests[2]["input"]
    assert next(item for item in replayed if item["type"] == "shell_call_output") == output


@pytest.mark.parametrize(
    "receipt_case",
    [
        "paired",
        "typed-auto",
        "typed-reference",
        "unrelated",
        "duplicate-output",
        "duplicate-call",
        "empty-environment",
        "unknown-environment",
        "numeric-container",
        "empty-container",
        "blank-container",
    ],
)
async def test_application_shell_accepts_only_paired_provider_container_receipts(
    responses: ResponsesFixture, receipt_case: str
) -> None:
    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        pytest.fail("The provider's container call reached the application callback")

    item = {
        "id": "sh_hosted_output",
        "type": "shell_call_output",
        "call_id": "shell-other" if receipt_case == "unrelated" else "shell-local",
        "status": "completed",
        "output": [{"stdout": "hosted", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}],
    }
    hosted_markers = {
        "typed-auto": {"environment": {"type": "container_auto"}},
        "typed-reference": {"environment": {"type": "container_reference"}},
        "empty-environment": {"environment": {}},
        "unknown-environment": {"environment": {"type": "unknown"}},
        "numeric-container": {"container_id": 0},
        "empty-container": {"container_id": ""},
        "blank-container": {"container_id": " "},
    }
    frames = [
        shell_call(
            **hosted_markers.get(receipt_case, {"container_id": "cntr-hosted"}),
            internal_chat_message_metadata_passthrough={"turn_id": "provider-turn"},
        ),
        {"type": "response.output_item.done", "item": item},
    ]
    if receipt_case == "duplicate-call":
        frames.insert(1, shell_call(container_id="cntr-hosted", id="sh_duplicate_call"))
    if receipt_case == "duplicate-output":
        frames.append(
            {"type": "response.output_item.done", "item": {**item, "id": "sh_duplicate_output"}}
        )
    responses.respond(*frames, assistant_output("provider done"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        provider=provider(responses),
        tools=[shell_tool(execute)],
    )
    async with timeout(30):
        if receipt_case in {"paired", "typed-auto", "typed-reference"}:
            assert (
                await agent.run("run in the provider container")
            ).final_output == "provider done"
        else:
            with pytest.raises(
                NativeError, match="Model returned a receipt for an application shell call"
            ):
                await agent.run("run in the provider container")
    assert len(responses.requests) == 1


@pytest.mark.parametrize("raw_shape", [False, True])
async def test_application_shell_rejects_argument_replacement_before_execution(
    responses: ResponsesFixture,
    raw_shape: bool,
) -> None:
    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        pytest.fail("A rejected replacement reached the Shell executor")

    def before(context: ToolContext[None]) -> ReplaceArguments:
        if raw_shape:
            return ReplaceArguments(context.effective_arguments["call"].model_dump())
        return ReplaceArguments(dict(context.effective_arguments))

    responses.respond(shell_call())
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        provider=provider(responses),
        tools=[shell_tool(execute, before=cast(Any, before))],
    )
    async with timeout(15):
        with pytest.raises(TypeError, match="Shell before policies must return Allow or Deny"):
            await agent.run("Run the test command.")


def test_application_shell_rejects_generic_after_replacement() -> None:
    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        return ShellOutput(output=[])

    with pytest.raises(TypeError, match="Shell tools require typed ShellOutput from the executor"):
        replace(shell_tool(execute), after=lambda *_: ToolOutput.text("unstructured"))
