"""Recovering from a model that calls a tool that doesn't exist.

Large models occasionally "hallucinate" a tool name that isn't registered on the agent --
for example they call ``search_linkedin`` when only ``search_web`` is available. Without a
handler, the SDK raises ``ModelBehaviorError`` and the entire run is lost.

Registering a ``tool_not_found`` error handler lets you turn that crash into a recoverable
nudge: the handler returns a ``ToolNotFoundAction`` with an error message, the runner
injects that message as a synthetic tool output, and the model self-corrects on the next
turn.

This example uses a tiny scripted ``Model`` subclass so it runs offline -- no API key
needed. See issue #325 for the real-world report that motivated this API.

    $ python examples/basic/tool_not_found_handler.py
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage

from agents import (
    Agent,
    ModelResponse,
    Runner,
    ToolNotFoundAction,
    ToolNotFoundErrorHandlerInput,
    Usage,
    function_tool,
)
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import TResponseInputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool


@function_tool
def search_web(query: str) -> str:
    """The only real tool on the agent."""
    return f"results for: {query}"


class ScriptedModel(Model):
    """Plays back a fixed script of model responses so the example runs offline."""

    def __init__(self, scripted_outputs: list[list[Any]]) -> None:
        self._outputs = list(scripted_outputs)

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        output = self._outputs.pop(0) if self._outputs else []
        return ModelResponse(output=output, usage=Usage(), response_id="scripted")

    def stream_response(  # pragma: no cover - not exercised here
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        raise NotImplementedError("streaming not used in this example")


def on_tool_not_found(data: ToolNotFoundErrorHandlerInput[Any]) -> ToolNotFoundAction:
    """Build a model-visible error so the model can pick a valid tool on its next step."""
    available = ", ".join(data.available_tools) or "(none)"
    return ToolNotFoundAction(
        error_message=(
            f"Tool {data.tool_name!r} is not registered on this agent. "
            f"Available tools: [{available}]. Pick one of those and try again."
        )
    )


async def main() -> None:
    # Turn 1: the model hallucinates a tool that doesn't exist.
    # Turn 2: after the handler injects the error, the model recovers with a final answer.
    scripted_model = ScriptedModel(
        [
            [
                ResponseFunctionToolCall(
                    id="call-1",
                    call_id="call-1",
                    type="function_call",
                    name="search_linkedin",  # intentionally unknown
                    arguments='{"query": "Anthropic"}',
                )
            ],
            [
                ResponseOutputMessage.model_validate(
                    {
                        "id": "msg-1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Sorry, I used the wrong tool. Here's what I got from search_web instead.",
                                "annotations": [],
                                "logprobs": [],
                            }
                        ],
                    }
                )
            ],
        ]
    )

    agent = Agent(
        name="recoverable_agent",
        instructions="You are a helpful assistant.",
        model=scripted_model,
        tools=[search_web],
    )

    result = await Runner.run(
        agent,
        input="find me profiles related to Anthropic",
        error_handlers={"tool_not_found": on_tool_not_found},
    )

    print("Final output:")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
