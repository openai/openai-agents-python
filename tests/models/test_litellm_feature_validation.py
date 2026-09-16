"""Responses-only features on the LiteLLM Chat Completions adapter.

`LitellmModel` speaks Chat Completions, so it cannot carry server-managed
conversation state, a reusable prompt, or the Responses-only reasoning settings.
These tests pin the same warn-once / strict-rejection behavior that
`OpenAIChatCompletionsModel` already has, so the two adapters cannot drift apart
again.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast

import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from agents import Agent, GuardrailFunctionOutput, Runner, function_tool, output_guardrail
from agents.exceptions import UserError
from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel


def _assistant_reply(content: str = "ok") -> ModelResponse:
    return ModelResponse(
        choices=[Choices(index=0, message=Message(role="assistant", content=content))],
        usage=Usage(0, 0, 0),
    )


@pytest.fixture
def recorded_acompletion(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace litellm.acompletion with a recorder that returns a plain reply."""
    calls: list[dict[str, Any]] = []

    async def fake_acompletion(model: str, messages: Any = None, **kwargs: Any) -> ModelResponse:
        calls.append({"model": model, "messages": messages, **kwargs})
        return _assistant_reply()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return calls


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_response_id", "conversation_id", "expected_param"),
    [
        ("resp_123", None, "previous_response_id"),
        (None, "conv_123", "conversation_id"),
    ],
)
async def test_warns_and_ignores_server_managed_conversation_state_by_default(
    recorded_acompletion: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    previous_response_id: str | None,
    conversation_id: str | None,
    expected_param: str,
) -> None:
    caplog.set_level(logging.WARNING, logger="openai.agents")

    await LitellmModel(model="test-model").get_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=previous_response_id,
        conversation_id=conversation_id,
        prompt=None,
    )

    assert "LitellmModel does not support server-managed conversation state" in caplog.text
    assert expected_param in caplog.text
    # Warn and ignore: the request still goes out.
    assert len(recorded_acompletion) == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_warns_once_per_model_instance(
    recorded_acompletion: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="openai.agents")
    model = LitellmModel(model="test-model")

    for _ in range(3):
        await model.get_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id="resp_123",
            conversation_id=None,
            prompt=None,
        )

    warnings = [
        record
        for record in caplog.records
        if "server-managed conversation state" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert len(recorded_acompletion) == 3


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_response_id", "conversation_id", "expected_param"),
    [
        ("resp_123", None, "previous_response_id"),
        (None, "conv_123", "conversation_id"),
    ],
)
async def test_rejects_server_managed_conversation_state_in_strict_mode(
    recorded_acompletion: list[dict[str, Any]],
    previous_response_id: str | None,
    conversation_id: str | None,
    expected_param: str,
) -> None:
    model = LitellmModel(model="test-model", strict_feature_validation=True)

    with pytest.raises(UserError, match="server-managed conversation state") as exc_info:
        await model.get_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=None,
        )

    assert expected_param in str(exc_info.value)
    # The request is refused before it reaches the provider.
    assert recorded_acompletion == []


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_warns_and_ignores_prompt_by_default(
    recorded_acompletion: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="openai.agents")

    await LitellmModel(model="test-model").get_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=cast(Any, {"id": "pmpt_123"}),
    )

    assert "Reusable prompts are only supported by the Responses API" in caplog.text
    assert "Ignoring `prompt`" in caplog.text
    assert "prompt" not in recorded_acompletion[0]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_rejects_prompt_in_strict_mode(
    recorded_acompletion: list[dict[str, Any]],
) -> None:
    model = LitellmModel(model="test-model", strict_feature_validation=True)

    with pytest.raises(UserError, match="Reusable prompts"):
        await model.get_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=cast(Any, {"id": "pmpt_123"}),
        )

    assert recorded_acompletion == []


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_warns_about_responses_only_reasoning_settings(
    recorded_acompletion: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="openai.agents")

    await LitellmModel(model="test-model").get_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(reasoning=Reasoning(effort="low", mode="think")),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )

    assert "LitellmModel does not support reasoning.mode" in caplog.text
    # reasoning.effort is the one setting Chat Completions does carry.
    assert recorded_acompletion[0]["reasoning_effort"] == "low"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_rejects_responses_only_reasoning_settings_in_strict_mode(
    recorded_acompletion: list[dict[str, Any]],
) -> None:
    model = LitellmModel(model="test-model", strict_feature_validation=True)

    with pytest.raises(UserError, match="reasoning.context"):
        await model.get_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(reasoning=Reasoning(effort="low", context="all_turns")),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )

    assert recorded_acompletion == []


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_stream_response_rejects_server_managed_conversation_state_in_strict_mode(
    recorded_acompletion: list[dict[str, Any]],
) -> None:
    model = LitellmModel(model="test-model", strict_feature_validation=True)

    with pytest.raises(UserError, match="server-managed conversation state"):
        async for _ in model.stream_response(
            system_instructions=None,
            input="",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id="resp_123",
            conversation_id=None,
            prompt=None,
        ):
            pass

    assert recorded_acompletion == []


@function_tool
def get_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"sunny in {city}"


@pytest.mark.asyncio
async def test_run_with_previous_response_id_warns_before_history_is_trimmed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The workflow the warning is for.

    With ``previous_response_id`` set the runner sends only the items the server
    is not assumed to have yet, so the second request carries the tool result
    without the user turn or the assistant tool call that produced it. LiteLLM has
    no such server, so that request is simply incomplete -- and it used to go out
    in silence.
    """
    requests: list[list[dict[str, Any]]] = []
    turn = 0

    async def fake_acompletion(model: str, messages: Any = None, **kwargs: Any) -> ModelResponse:
        nonlocal turn
        requests.append(list(messages or []))
        turn += 1
        if turn == 1:
            message = Message(
                role="assistant",
                content=None,
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": json.dumps({"city": "Paris"}),
                        },
                    }
                ],
            )
            finish_reason = "tool_calls"
        else:
            message = Message(role="assistant", content="It is sunny in Paris.")
            finish_reason = "stop"
        return ModelResponse(
            choices=[Choices(index=0, message=message, finish_reason=finish_reason)],
            usage=Usage(0, 0, 0),
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    caplog.set_level(logging.WARNING, logger="openai.agents")

    agent = Agent(
        name="a",
        instructions="be brief",
        tools=[get_weather],
        model=LitellmModel(model="test-model"),
    )
    await Runner.run(agent, "What is the weather in Paris?", previous_response_id="resp_abc")

    assert "LitellmModel does not support server-managed conversation state" in caplog.text

    # The trimming itself is the runner's server-managed behavior, unchanged here:
    # the second request holds the tool result with no assistant tool call before it.
    roles = [message["role"] for message in requests[1]]
    assert "tool" in roles
    assert "assistant" not in roles
    assert "user" not in roles


def _validate_guardrails(model: Any) -> None:
    from agents.run_config import RunConfig
    from agents.run_internal.agent_runner_helpers import (
        validate_output_guardrails_with_server_managed_conversation,
    )

    @output_guardrail
    async def never_trips(ctx: Any, agent: Any, output: Any) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    validate_output_guardrails_with_server_managed_conversation(
        Agent(name="a", model=model, output_guardrails=[never_trips]),
        RunConfig(),
        conversation_id=None,
        previous_response_id="resp_abc",
        auto_previous_response_id=False,
    )


def test_output_guardrails_are_allowed_on_a_chat_completions_adapter() -> None:
    """The rejection names history kept on a server, which neither adapter keeps.

    `OpenAIChatCompletionsModel` was already exempt; `LitellmModel` speaks the same
    API and was not, so the run was refused for a store that does not exist.
    """
    _validate_guardrails(LitellmModel(model="test-model"))
    _validate_guardrails(
        OpenAIChatCompletionsModel(model="gpt-4", openai_client=AsyncOpenAI(api_key="fake-key"))
    )


def test_output_guardrails_are_still_rejected_on_a_responses_model() -> None:
    with pytest.raises(UserError, match="Output guardrails cannot be combined"):
        _validate_guardrails(
            OpenAIResponsesModel(model="gpt-4", openai_client=AsyncOpenAI(api_key="fake-key"))
        )
