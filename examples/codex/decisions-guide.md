# Make typed decisions

Use `Agent.decide()` for a boolean, a choice, or a structured value. A decision provider sends the request. The SDK validates the result as it validates Agent output. Configure the provider on an Agent and use its decision method or its callback contexts.

Decisions support is not in the latest published preview. [Build the matching source](../../CONTRIBUTING.md), then install that build with its `decisions` extra to use `OpenAIDecisions`. Custom providers do not require the OpenAI client dependency. Set `OPENAI_API_KEY` before running these examples.

## Return a typed value

```python
import asyncio
from typing import Literal

from pydantic import BaseModel

from agents import Agent, OpenAIDecisions


class TicketDecision(BaseModel):
    category: Literal["billing", "technical", "other"]
    urgent: bool


async def main() -> None:
    async with OpenAIDecisions() as decisions:
        agent = Agent(instructions="Assess the supplied evidence.", decisions=decisions)
        result = await agent.decide(
            "Classify this support ticket.",
            input="I was charged twice for my subscription.",
            output_type=TicketDecision,
        )
        print(result.category, result.urgent)


asyncio.run(main())
```

Omit `output_type` for a boolean. Pass `choices=["billing", "technical", "other"]` to return one of those values. `choices` and `output_type` are mutually exclusive. Every call requires explicit `input`: text or a sequence of model input items. The SDK does not add conversation history or Agent instructions implicitly.

`output_type` also accepts Pydantic schema expressions such as `Annotated[float, Field(ge=0, le=1)]` and `Literal["yes", "no"]`. Type checkers may infer `Any` for these expressions; use an explicit result annotation when you need a narrower type. Class schemas keep their inferred result type.

The built-in provider uses the supported Decisions backend without a model-selection option. A boolean answer uses a probability threshold of `0.5`. Typed objects can contain supported finite choices, booleans, bounded scores, and nested records. The provider rejects unsupported schemas before sending a request.

## Bind a provider to an Agent

The `decisions` setting is separate from the Agent's generative `model` and `provider`. Set `OPENAI_MODEL` to the generative model to use. This example checks a proposed answer before accepting it. A failed check requests one continuation of the same turn.

```python
import asyncio
import os

from agents import Agent, CompletionContext, ContinueTurn, OpenAIDecisions


async def check_answer(ctx: CompletionContext[None]):
    accepted = await ctx.decide(
        "Does the answer explain both the cause and a practical next step?",
        input=ctx.text or "",
    )
    if accepted:
        return None
    if ctx.continuation_count >= 1:
        raise ValueError("The answer did not pass its completion check")
    return ContinueTurn("Explain the cause and give a practical next step.")


async def main() -> None:
    async with OpenAIDecisions() as decisions:
        agent = Agent(
            instructions="Help the user troubleshoot their connection.",
            model=os.environ["OPENAI_MODEL"],
            decisions=decisions,
            before_complete=check_answer,
        )
        result = await agent.run("Why does my Wi-Fi keep disconnecting?")
        print(result.final_output)


asyncio.run(main())
```

`turn.decide()` in `before_step` or `prepare_request`, `ToolContext.decide()` in a function tool or native-agent approval, and `CompletionContext.decide()` inherit the captured provider. Await each call directly in that callback's task. Calls from another task or after the callback returns are rejected, including calls started with `asyncio.gather()`, `asyncio.create_task()`, or `TaskGroup`. Completed usage contributes to the owning turn; child usage stays with the child. Run budgets check limits before admitting a root model step. An admitted request may finish after reaching a limit. Decision calls do not add root request admissions. Cancellation joins started work. The helper does not append its question or answer to the conversation.

Completion feedback does enter the conversation. Already streamed text remains visible and recorded. See [completion checks](../../docs/sdk/configuration.md#check-a-proposed-completion) for timing and continuation limits.

## Replace the provider

Any object implementing `DecisionProvider.get_decision(request)` can replace `OpenAIDecisions`. It receives a `DecisionRequest` with `question`, `input`, and `output_schema`, and returns `DecisionResponse(value=..., usage=..., request_id=..., model=...)`. Only `value` is required. Return JSON matching the supplied schema; scalar outputs use the schema's `response` wrapper. The SDK validates and unwraps that value for the caller.

Use the same provider through `await agent.decide(...)` or the callback helpers. `Agent.decide()` does not open a run or use history.

Provider defaults are captured when opening starts. A callback uses its exact captured Agent profile's provider, then the captured root default. An explicit `decisions=` on `agent.run()` or `agent.session()` overrides those defaults across profiles for that opening. A helper's `provider=` overrides them for one call. Profile changes do not retarget an in-flight decision. Agent and Session neither serialize nor close borrowed providers. `OpenAIDecisions(client=...)` also leaves a supplied OpenAI client open.

## Choose an explicit generative fallback

Free-form text and other unbounded schemas need a generative request. Configure `OpenAIDecisions(fallback_model=...)` to send the whole unsupported schema through Responses. The provider chooses the API before inference. It does not first make a Decisions request or retry a low-confidence answer. Ordinary client transport retries still follow the OpenAI client's settings.

## Handle errors and usage

A refusal, malformed service answer, or non-JSON provider value raises `DecisionError`. Its `reason` is `refusal` or `invalid_response`; it retains trustworthy usage and request metadata without retaining the response body. Bound helpers record that completed usage before raising. Valid JSON that fails the requested output type raises the existing `OutputValidationError`, as Agent output does. Both failures remain available if recording a completed error also fails. Missing or invalid usage stays unknown. Transport errors keep the OpenAI client's exception types. If a schema is unsupported and no fallback is configured, the call fails before any request.
