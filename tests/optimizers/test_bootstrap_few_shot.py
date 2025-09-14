from __future__ import annotations

from typing import Any

import pytest
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from agents import Agent
from agents.items import ModelResponse
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.optimizers import BootstrapFewShot, LabeledExample, evaluate_agent, exact_match_metric
from agents.tool import Tool
from agents.usage import Usage


class RuleBasedEchoModel(Model):
    """A tiny deterministic model for testing optimization.

    Behavior:
    - If there are any assistant messages in the input, return the last assistant message text.
    - Otherwise, echo the last user message.
    This lets few-shot examples (assistant messages) influence outputs.
    """

    async def get_response(
        self,
        system_instructions: str | None,
        input: Any,
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema,
        handoffs,
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any | None,
    ) -> ModelResponse:
        last_user = None
        last_assistant = None
        for item in input:
            role = item.get("role")
            content = item.get("content", "")
            if role == "assistant":
                last_assistant = content
            elif role == "user":
                last_user = content

        text = last_assistant if last_assistant is not None else (last_user or "")
        msg = ResponseOutputMessage(
            id="m",
            content=[ResponseOutputText(annotations=[], text=str(text), type="output_text")],
            role="assistant",
            status="completed",
            type="message",
        )
        return ModelResponse(output=[msg], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):  # pragma: no cover - not needed in this test
        raise NotImplementedError


@pytest.mark.asyncio
async def test_evaluate_agent_and_bootstrap_few_shot_improves_score():
    agent = Agent(name="Test", model=RuleBasedEchoModel())

    # Dataset where the ideal behavior is to answer with a fixed mapping
    # We will rely on few-shot to inject assistant messages with the expected outputs.
    dataset = [
        LabeledExample(input="A?", expected="1"),
        LabeledExample(input="B?", expected="2"),
        LabeledExample(input="C?", expected="3"),
        LabeledExample(input="D?", expected="4"),
    ]

    # Baseline without few-shot: the model just echoes the user, so 0 EM
    baseline = await evaluate_agent(agent, dataset=dataset, metric=exact_match_metric)
    assert baseline.average == 0.0

    # Fit a few-shot optimizer; with greedy selection it should add examples that
    # force the model to output the correct mapping for the validation set.
    bfs = BootstrapFewShot(max_examples=3)
    result = await bfs.fit(agent, dataset)

    # Attach optimizer to a RunConfig and re-evaluate
    cfg = result.attach_to_runconfig()
    improved = await evaluate_agent(agent, dataset=dataset, metric=exact_match_metric, run_config=cfg)

    assert improved.average >= baseline.average
    assert len(result.selected_examples) > 0

