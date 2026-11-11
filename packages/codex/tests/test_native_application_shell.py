"""Application-managed Shell over the real native Responses transport."""

from __future__ import annotations

from typing import Any

import pytest

from agents import (
    Agent,
    SessionState,
    ShellCall,
    ShellCommandOutput,
    ShellExit,
    ShellOutput,
    ShellSkill,
    ToolContext,
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
    tool = shell_tool(execute, skills=[skill], needs_approval=True)
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


async def test_application_shell_ignores_an_explicit_provider_container_call(
    responses: ResponsesFixture,
) -> None:
    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        pytest.fail("The provider's container call reached the application callback")

    responses.respond(shell_call(container_id="cntr-hosted"), assistant_output("provider done"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        provider=provider(responses),
        tools=[shell_tool(execute)],
    )
    async with timeout(30):
        assert (await agent.run("run in the provider container")).final_output == "provider done"
    assert len(responses.requests) == 1
