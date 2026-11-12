"""SDK-only executor metadata and native Shell replay."""

import asyncio
import json
from pathlib import Path

import pytest

from agents import Agent, NativeProvider, SessionState, ShellOutput, shell_tool
from agents.observation import ToolFinished
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_application_shell import shell_call


def selected_model():
    catalog = json.loads(
        (Path(__file__).parent / "fixtures/request-control-models.json").read_text()
    )
    return next(
        row["slug"]
        for row in catalog["models"]
        if row.get("supported_in_api")
        and not row.get("use_responses_lite")
        and row.get("tool_mode") != "code_mode_only"
    )


@pytest.mark.parametrize("as_model", [False, True])
async def test_native_shell_metadata_survives_restore_without_model_replay(responses, as_model):
    effects = []
    value = {
        "output": [
            {
                "stdout": "public result",
                "stderr": "",
                "outcome": {"type": "exit", "exit_code": 0},
                "command": "sdk-only-command",
                "provider_data": {"same": "sdk-only-entry"},
            }
        ],
        "provider_data": {"same": "sdk-only-top"},
    }
    expected = {
        "provider_data": {"same": "sdk-only-top"},
        "shell_output": [
            {"command": "sdk-only-command", "provider_data": {"same": "sdk-only-entry"}}
        ],
    }

    def execute(context, call):
        effects.append(call.call_id)
        return ShellOutput.model_validate(value) if as_model else value

    responses.respond(shell_call(call_id="metadata-call", action={"commands": ["public command"]}))
    responses.respond(assistant_output("done"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use the declared executor.",
            model=selected_model(),
            provider=provider,
            tools=[shell_tool(execute)],
        )
        async with agent.session() as session:
            turn = await session.submit("execute")
            result = await asyncio.wait_for(turn.get_result(), 10)
            assert result.final_output == "done"
            (record,) = result.application_tool_outputs
            assert (record.call_id, record.name) == ("metadata-call", "shell")
            assert record.application_data == expected
            record.application_data["provider_data"] = "mutated result"
            assert turn.application_tool_outputs[0].application_data == expected
            detailed = [
                event
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, ToolFinished)
            ]
            assert detailed[0].output.application_data == expected
            default = [event async for event in turn.events() if isinstance(event, ToolFinished)]
            assert default[0].output is None
            saved = SessionState.from_json((await session.save_state()).to_json())
            assert saved.application_tool_outputs[0].application_data == expected
            assert "sdk-only" not in json.dumps(saved.history)
        value["provider_data"]["same"] = "mutated executor value"
        responses.respond(assistant_output("restored"))
        async with agent.session(state=saved) as restored:
            result = await asyncio.wait_for((await restored.submit("continue")).get_result(), 10)
            assert result.final_output == "restored"
            assert (await restored.save_state()).application_tool_outputs[
                0
            ].application_data == expected
    assert effects == ["metadata-call"]
    assert "sdk-only" not in json.dumps(responses.requests)


@pytest.mark.parametrize("location", ["top", "entry"])
async def test_native_shell_empty_provider_metadata_keeps_its_owner(responses, location):
    value = {
        "output": [
            {"stdout": "public result", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
        ]
    }
    if location == "top":
        value["provider_data"] = {}
        expected = {"provider_data": {}}
    else:
        value["output"][0]["provider_data"] = {}
        expected = {"shell_output": [{"provider_data": {}}]}

    def execute(context, call):
        return value

    responses.respond(shell_call(call_id="metadata-call", action={"commands": ["public command"]}))
    responses.respond(assistant_output("done"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use the declared executor.",
            model=selected_model(),
            provider=provider,
            tools=[shell_tool(execute)],
        )
        async with agent.session() as session:
            result = await asyncio.wait_for((await session.submit("execute")).get_result(), 10)
            assert result.application_tool_outputs[0].application_data == expected
