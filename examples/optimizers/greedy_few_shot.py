from __future__ import annotations

import asyncio
from typing import Any

from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from agents import (
    Agent,
    BootstrapFewShot,
    LabeledExample,
    evaluate_agent,
    exact_match_metric,
)
from agents.items import ModelResponse
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool
from agents.usage import Usage


class RuleBasedEchoModel(Model):
    """Toy model used to demonstrate few-shot optimization.

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

    def stream_response(self, *args, **kwargs):  # pragma: no cover - not needed here
        raise NotImplementedError


async def main() -> None:
    agent = Agent(name="GreedyFewShotDemo", model=RuleBasedEchoModel())

    # Dataset where the ideal behavior is to answer with a fixed mapping
    # We will rely on few-shot to inject assistant messages with the expected outputs.
    dataset = [
        LabeledExample(input="A?", expected="1"),
        LabeledExample(input="B?", expected="2"),
        LabeledExample(input="C?", expected="3"),
        LabeledExample(input="D?", expected="4"),
    ]

    # Baseline without few-shot
    baseline = await evaluate_agent(agent, dataset=dataset, metric=exact_match_metric)
    print("Baseline avg:", baseline.average)

    # Optimize few-shot greedily
    bfs = BootstrapFewShot(max_examples=3)
    result = await bfs.fit(agent, dataset)
    run_config = result.attach_to_runconfig()

    # Re-evaluate with optimized config
    improved = await evaluate_agent(
        agent, dataset=dataset, metric=exact_match_metric, run_config=run_config
    )
    print("Improved avg:", improved.average)
    print("Selected examples:", len(result.selected_examples))


if __name__ == "__main__":
    asyncio.run(main())


