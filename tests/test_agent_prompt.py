from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from agents import Agent, ModelSettings, Prompt, RunConfig, RunContextWrapper, Runner
from agents.models.openai_provider import OpenAIProvider
from agents.prompts import GenerateDynamicPromptData
from agents.testing import ScriptedModel
from tests.model_test_helpers import get_response_obj

from .test_responses import get_text_message


class PromptCaptureScriptedModel(ScriptedModel):
    """Subclass of ScriptedModel that records the prompt passed to the model."""

    def __init__(self):
        super().__init__()
        self.last_prompt = None

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id,
        conversation_id,
        prompt,
    ):
        # Record the prompt that the agent resolved and passed in.
        self.last_prompt = prompt
        return await super().get_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
            prompt=prompt,
        )


@pytest.mark.asyncio
async def test_static_prompt_is_resolved_correctly():
    static_prompt: Prompt = {
        "id": "my_prompt",
        "version": "1",
        "variables": {"some_var": "some_value"},
    }

    agent = Agent(name="test", prompt=static_prompt)
    context_wrapper = RunContextWrapper(context=None)

    resolved = await agent.get_prompt(context_wrapper)

    assert resolved == {
        "id": "my_prompt",
        "version": "1",
        "variables": {"some_var": "some_value"},
    }


@pytest.mark.asyncio
async def test_dynamic_prompt_is_resolved_correctly():
    dynamic_prompt_value: Prompt = {"id": "dyn_prompt", "version": "2"}

    def dynamic_prompt_fn(_data):
        return dynamic_prompt_value

    agent = Agent(name="test", prompt=dynamic_prompt_fn)
    context_wrapper = RunContextWrapper(context=None)

    resolved = await agent.get_prompt(context_wrapper)

    assert resolved == {"id": "dyn_prompt", "version": "2", "variables": None}


@pytest.mark.asyncio
async def test_prompt_is_passed_to_model():
    static_prompt: Prompt = {"id": "model_prompt"}

    model = PromptCaptureScriptedModel()
    agent = Agent(name="test", model=model, prompt=static_prompt)

    # Ensure the model returns a simple message so the run completes in one turn.
    model.enqueue([get_text_message("done")])

    await Runner.run(agent, input="hello")

    # The model should have received the prompt resolved by the agent.
    expected_prompt = {
        "id": "model_prompt",
        "version": None,
        "variables": None,
    }
    assert model.last_prompt == expected_prompt


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream", "model", "run_settings", "expected_fields"),
    [
        pytest.param(False, None, None, {}, id="prompt-model"),
        pytest.param(True, None, None, {}, id="streamed-prompt-model"),
        pytest.param(
            False,
            None,
            ModelSettings(reasoning=Reasoning(effort="low")),
            {"reasoning": {"effort": "low"}},
            id="explicit-run-effort",
        ),
        pytest.param(
            False,
            "gpt-6-astra",
            None,
            {"model": "gpt-6-astra", "reasoning": {"effort": "low"}, "text": {"verbosity": "low"}},
            id="explicit-agent-model",
        ),
    ],
)
async def test_saved_prompt_preserves_model_ownership_and_explicit_settings(
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
    model: str | None,
    run_settings: ModelSettings | None,
    expected_fields: dict[str, Any],
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-6-astra")
    bodies: list[dict[str, Any]] = []

    # Capture the serialized request because a scripted model cannot observe omitted fields.
    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        response = get_response_obj([get_text_message("done")]).model_dump(mode="json")
        if stream:
            event = {"type": "response.completed", "response": response, "sequence_number": 0}
            return httpx2.Response(
                200,
                content=f"event: response.completed\ndata: {json.dumps(event)}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(200, json=response)

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    ) as client:
        run_config = RunConfig(
            model_provider=OpenAIProvider(openai_client=client),
            model_settings=run_settings,
            tracing_disabled=True,
        )
        agent = Agent(name="prompt-agent", model=model, prompt={"id": "pmpt_agent"})
        if stream:
            result = Runner.run_streamed(agent, input="hi", run_config=run_config)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, input="hi", run_config=run_config)

    (body,) = bodies
    assert body["prompt"] == {"id": "pmpt_agent", "version": None, "variables": None}
    assert "tools" not in body
    assert {
        key: body[key] for key in ("model", "reasoning", "text") if key in body
    } == expected_fields


@pytest.mark.asyncio
async def test_run_cancels_sibling_instructions_when_prompt_resolution_fails() -> None:
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()
    slow_finished = asyncio.Event()

    async def slow_instructions(_ctx: RunContextWrapper[Any], _agent: Agent[Any]) -> str:
        slow_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            slow_cancelled.set()
            raise
        finally:
            slow_finished.set()
        return "unreachable"

    async def failing_prompt(_data: GenerateDynamicPromptData) -> Prompt:
        await slow_started.wait()
        raise RuntimeError("prompt resolution failed")

    agent = Agent(
        name="prompt-agent",
        model=ScriptedModel(),
        instructions=slow_instructions,
        prompt=failing_prompt,
    )

    with pytest.raises(RuntimeError, match="prompt resolution failed"):
        await Runner.run(agent, input="hi")

    assert slow_cancelled.is_set()
    assert slow_finished.is_set()


@pytest.mark.asyncio
async def test_run_streamed_cancels_sibling_instructions_when_prompt_resolution_fails() -> None:
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()
    slow_finished = asyncio.Event()

    async def slow_instructions(_ctx: RunContextWrapper[Any], _agent: Agent[Any]) -> str:
        slow_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            slow_cancelled.set()
            raise
        finally:
            slow_finished.set()
        return "unreachable"

    async def failing_prompt(_data: GenerateDynamicPromptData) -> Prompt:
        await slow_started.wait()
        raise RuntimeError("prompt resolution failed")

    agent = Agent(
        name="prompt-agent",
        model=ScriptedModel(),
        instructions=slow_instructions,
        prompt=failing_prompt,
    )

    with pytest.raises(RuntimeError, match="prompt resolution failed"):
        result = Runner.run_streamed(agent, input="hi")
        async for _event in result.stream_events():
            pass

    assert slow_cancelled.is_set()
    assert slow_finished.is_set()
