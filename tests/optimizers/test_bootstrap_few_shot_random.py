from __future__ import annotations

from typing import Any

import pytest
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from agents import Agent
from agents.items import ModelResponse
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.optimizers import (
    BootstrapFewShotRandomSearch,
    LabeledExample,
    evaluate_agent,
    exact_match_metric,
)
from agents.tool import Tool
from agents.usage import Usage


class MappingModel(Model):
    """Deterministic model where assistant messages define outputs via few-shot."""

    async def get_response(
        self,
        system_instructions: str | None,
        input,
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

    def stream_response(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_bfs_random_search_improves_score():
    agent = Agent(name="RS", model=MappingModel())
    dataset = [
        LabeledExample(input="A?", expected="1"),
        LabeledExample(input="B?", expected="2"),
        LabeledExample(input="C?", expected="3"),
        LabeledExample(input="D?", expected="4"),
    ]

    baseline = await evaluate_agent(agent, dataset=dataset, metric=exact_match_metric)

    rs = BootstrapFewShotRandomSearch(max_examples=3, num_trials=8, seed=42)
    res = await rs.fit(agent, dataset)

    cfg = res.attach_to_runconfig()
    improved = await evaluate_agent(
        agent, dataset=dataset, metric=exact_match_metric, run_config=cfg
    )
    assert improved.average >= baseline.average


