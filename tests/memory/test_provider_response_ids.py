from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents import Agent, RunConfig, Runner
from agents.memory import OpenAIResponsesCompactionSession
from agents.testing import ModelStep, ScriptedModel
from tests.test_responses import get_final_output_message
from tests.utils.simple_session import SimpleListSession


class _ClientHistoryModel(ScriptedModel):
    # Provider IDs identify the exchange but do not name stored Responses API resources.
    _supports_stored_response_id = False


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_provider_ids_compact_full_input_and_clear_prior_stored_response_id(
    streamed: bool,
) -> None:
    client = MagicMock()
    summary = {"type": "compaction", "id": "cmp-test", "encrypted_content": "summary"}
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session = OpenAIResponsesCompactionSession(
        "provider-ids",
        SimpleListSession(),
        client=client,
        should_trigger_compaction=lambda _: True,
    )
    session._response_id = "resp-older-openai-response"
    model = _ClientHistoryModel(
        steps=[
            ModelStep(
                output=[get_final_output_message("done")],
                response_id="provider-turn-id",
            )
        ]
    )
    agent = Agent(name="worker", model=model)
    if streamed:
        result = Runner.run_streamed(
            agent, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
        )
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(
            agent, "hello", session=session, run_config=RunConfig(tracing_disabled=True)
        )
    assert result.final_output == "done"
    client.responses.compact.assert_awaited_once()
    payload = client.responses.compact.call_args.kwargs
    assert "previous_response_id" not in payload
    assert payload["input"][0] == {"role": "user", "content": "hello"}
    assert payload["input"][1]["content"][0]["text"] == "done"
    assert session._response_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_explicit_stored_compaction_waits_for_a_stored_response(streamed: bool) -> None:
    client = MagicMock()
    client.responses.compact = AsyncMock()
    session = OpenAIResponsesCompactionSession(
        "stored-only",
        SimpleListSession(),
        client=client,
        compaction_mode="previous_response_id",
        should_trigger_compaction=lambda _: True,
    )
    session._response_id = "resp_from_prior_provider"
    model = _ClientHistoryModel(steps=[ModelStep(output=[get_final_output_message("done")])])
    agent = Agent(name="worker", model=model)
    config = RunConfig(tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(agent, "hello", session=session, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        await Runner.run(agent, "hello", session=session, run_config=config)
    client.responses.compact.assert_not_awaited()
    assert session._response_id is None
    assert len(await session.get_items()) == 2
    with pytest.raises(ValueError, match="requires a response_id"):
        await session.run_compaction({"force": True})


@pytest.mark.asyncio
async def test_stored_response_model_keeps_previous_response_compaction() -> None:
    client = MagicMock()
    summary = {"type": "compaction", "id": "cmp-test", "encrypted_content": "summary"}
    client.responses.compact = AsyncMock(return_value=SimpleNamespace(output=[summary]))
    session = OpenAIResponsesCompactionSession(
        "stored-ids",
        SimpleListSession(),
        client=client,
        should_trigger_compaction=lambda _: True,
    )
    model = ScriptedModel(
        steps=[
            ModelStep(
                output=[get_final_output_message("done")],
                response_id="resp-stored",
            )
        ]
    )
    await Runner.run(
        Agent(name="worker", model=model),
        "hello",
        session=session,
        run_config=RunConfig(tracing_disabled=True),
    )
    payload = client.responses.compact.call_args.kwargs
    assert payload["previous_response_id"] == "resp-stored"
    assert "input" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_client_history_provider_approval_metadata_survives_serialized_resume(
    streamed: bool,
) -> None:
    from agents import RunState, StopAtTools
    from agents.testing import assistant_message, function_call
    from tests.deferred_session_writes.helpers import always_fine, write_thing

    observed = []
    session = OpenAIResponsesCompactionSession(
        "approval-provider",
        SimpleListSession(),
        should_trigger_compaction=lambda context: observed.append(context) or False,
    )
    model = _ClientHistoryModel(
        steps=[
            ModelStep(
                output=[function_call("write_thing", {"query": "x"}, call_id="call_write")],
                response_id="provider-parked",
            ),
            ModelStep(output=[assistant_message("done")], response_id="provider-final"),
        ]
    )
    agent = Agent(
        name="worker",
        model=model,
        tools=[write_thing],
        output_guardrails=[always_fine],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["finish"]),
    )
    config = RunConfig(tracing_disabled=True)
    if streamed:
        first = Runner.run_streamed(agent, "hello", session=session, run_config=config)
        async for _ in first.stream_events():
            pass
    else:
        first = await Runner.run(agent, "hello", session=session, run_config=config)
    payload = first.to_state().to_json()
    exchange = payload["pending_session_write"]["compaction_model_exchange"]
    assert exchange["supports_stored_response_id"] is False
    state = await RunState.from_json(agent, payload)
    state.approve(state.get_interruptions()[0])
    if streamed:
        result = Runner.run_streamed(agent, state, session=session, run_config=config)
        async for _ in result.stream_events():
            pass
    else:
        result = await Runner.run(agent, state, session=session, run_config=config)
    assert result.final_output == "done"
    assert observed
    assert all(context["compaction_mode"] == "input" for context in observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("continued", [False, True])
async def test_auto_previous_response_id_rejects_client_history_models_before_call(
    streamed: bool,
    continued: bool,
) -> None:
    from agents import UserError, function_tool
    from tests.test_responses import get_function_tool_call

    executions = []

    @function_tool(needs_approval=True)
    def perform_action() -> str:
        executions.append("performed")
        return "done"

    model = _ClientHistoryModel(
        steps=[
            [
                get_function_tool_call("perform_action", "{}")
                if continued
                else get_final_output_message("first")
            ],
            [get_final_output_message("second")],
        ]
    )
    agent = Agent(name="worker", model=model, tools=[perform_action])
    config = RunConfig(tracing_disabled=True)
    input = "hello"
    expected_calls = 0
    if continued:
        first = await Runner.run(agent, input, run_config=config)
        input = first.to_state()
        input.approve(first.interruptions[0])
        input.add_input("continue")
        expected_calls = 1
    with pytest.raises(UserError, match="does not support auto_previous_response_id"):
        if streamed:
            result = Runner.run_streamed(
                agent, input, run_config=config, auto_previous_response_id=True
            )
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, input, run_config=config, auto_previous_response_id=True)
    assert len(model.calls) == expected_calls
    assert executions == []


@pytest.mark.asyncio
async def test_manual_history_replay_remains_available_for_client_history_models() -> None:
    model = _ClientHistoryModel(
        steps=[
            [get_final_output_message("first")],
            [get_final_output_message("second")],
        ]
    )
    agent = Agent(name="worker", model=model)
    config = RunConfig(tracing_disabled=True)
    first = await Runner.run(agent, "hello", run_config=config)
    second = await Runner.run(
        agent, [*first.to_input_list(), {"role": "user", "content": "continue"}], run_config=config
    )
    assert second.final_output == "second"
    assert model.calls[1].input[1]["content"][0]["text"] == "first"
