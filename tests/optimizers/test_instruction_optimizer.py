from __future__ import annotations

import pytest
from typing import Any
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from agents import Agent
from agents.items import ModelResponse
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.optimizers import InstructionOptimizer, LabeledExample, evaluate_agent, exact_match_metric
from agents.tool import Tool
from agents.usage import Usage


class PrefixSensitiveModel(Model):
    """Toy model that outputs "OK" only when a specific system prompt is set.

    If system instructions equal "Answer with OK.", it outputs "OK"; otherwise it echoes the
    last user message. Lets us test instruction optimization.
    """

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
        text = None
        if system_instructions == "Answer with OK.":
            text = "OK"
        else:
            # echo the last user
            for item in input:
                if item.get("role") == "user":
                    text = item.get("content")
        msg = ResponseOutputMessage(
            id="m",
            content=[ResponseOutputText(annotations=[], text=str(text or ""), type="output_text")],
            role="assistant",
            status="completed",
            type="message",
        )
        return ModelResponse(output=[msg], usage=Usage(), response_id=None)

    def stream_response(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_instruction_optimizer_improves_accuracy():
    agent = Agent(name="Instr", model=PrefixSensitiveModel())
    dataset = [
        LabeledExample(input="hello", expected="OK"),
        LabeledExample(input="world", expected="OK"),
    ]

    base = await evaluate_agent(agent, dataset=dataset, metric=exact_match_metric)
    assert base.average < 1.0

    opt = InstructionOptimizer(candidates=["Answer with OK.", "You are helpful."])
    res = await opt.fit(agent, dataset)

    cfg = res.attach_to_runconfig()
    improved = await evaluate_agent(agent, dataset=dataset, metric=exact_match_metric, run_config=cfg)
    assert improved.average >= base.average


