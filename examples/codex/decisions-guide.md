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

`turn.decide()` in `before_step` or `prepare_request`, `ToolContext.decide()` in a function tool or native-agent approval, and `CompletionContext.decide()` inherit the captured provider. Use `asyncio.gather()` to overlap decisions on the live callback's event loop. On callback exit or cancellation, expect the SDK to cancel pending calls and wait for provider cleanup before settling the callback.

Decision usage stays separate from Agent model usage and run budgets. When tracing is configured and enabled, a `decision.usage` child span records safe token counts and request metadata. Missing usage stays unknown. Disabled tracing creates no recording sink; custom providers can still inspect `DecisionResponse.usage`. The helper does not append its question or answer to the conversation.

Completion feedback does enter the conversation. Already streamed text remains visible and recorded. See [completion checks](../../docs/sdk/configuration.md#check-a-proposed-completion) for timing and continuation limits.

## Route to an authorized specialist

The [routing recipe](recipes/decisions_routing.py) has been demonstrating application-side delegation through `ToolContext.decide()`. Its support workflow has been choosing between `billing` and `technical`; its research workflow has been choosing between `catalog` and `risk`. The orchestrator has been retaining its generative provider. The application has been supplying the request and specialist descriptions through `Routing`, with the Decisions provider receiving that request and only authorized candidate descriptions and IDs. The descriptions have been explaining each specialist's role independently of its ID.

The recipe has been using `ScriptedResponses` for generation and a synthetic `DecisionProvider` for routing. Both have been exercising the real SDK interfaces without credentials or live API calls. From the repository root, a Python environment with the matching SDK build has been supporting this command:

```sh
python -m examples.codex.recipes.decisions_routing
```

The application has been controlling these routing rules:

- `use_decisions=False` has been selecting the application default without calling Decisions.
- `use_decisions=True` has been passing only currently authorized, registered IDs as `choices`. The SDK has been validating the returned ID against those choices.
- The application has been checking authorization again after awaiting Decisions and immediately before specialist invocation. The synthetic revocation scenario has been removing an ID during the decision call.
- Invalid choices, refusals, and the decision timeout have been selecting the application default only while that default has been retaining authorization. Missing authorization has been raising `PermissionError` before specialist execution. Cancellation has been propagating rather than triggering fallback.

Each specialist has been receiving its own `Catalog` context and explicit `read_catalog` tool, following the [separate specialist Session recipe](recipes/specialist_agents.py). `Agent.run()` has been owning that separate Session and its cleanup. The tool has been returning a typed `RoutedSummary` to the orchestrator, which has been continuing its original conversation. These Python closures have been sharing the application process; the recipe has been relying on application-controlled context and tool construction for authority.

The runnable assertions have been checking the selected context, the candidate schema and descriptions, orchestration resumption, opt-in and default behavior, authorization changes, invalid output, and timeout cleanup. The script has been bounding decision, specialist, and overall run work. Its 1-second decision deadline has been serving the synthetic fixture. Production applications have been choosing deadlines for their provider and checking authorization at their actual dispatch boundary. The fixture has been establishing SDK wiring and application policy, without measuring live routing quality or validating a tracing backend.

## Replace the provider

Any object implementing `DecisionProvider.get_decision(request)` can replace `OpenAIDecisions`. It receives a `DecisionRequest` with `question`, `input`, and `output_schema`, and returns `DecisionResponse(value=..., usage=..., request_id=..., model=...)`. Only `value` is required. Return the application JSON value matching the supplied schema. For example, a boolean request has `output_schema={"type": "boolean"}` and accepts `DecisionResponse(value=True)`. The SDK checks the raw value against the advertised schema before Pydantic constructs the typed result. Missing required fields, extra fields, and strings such as `"yes"` cannot become an approval through coercion or defaults.

Use the same provider through `await agent.decide(...)` or the callback helpers. `Agent.decide()` does not open a run or use history.

Provider defaults are captured when opening starts. A callback uses its exact captured Agent profile's provider, then the captured root default. A helper's `provider=` overrides them for one call. Profile changes do not retarget an in-flight decision. Agent and Session neither serialize nor close borrowed providers. `OpenAIDecisions(client=...)` also leaves a supplied OpenAI client open.

## Handle unsupported schemas

`OpenAIDecisions` rejects free-form text and other unsupported schemas before sending a request. Use ordinary Agent generation separately when the task needs unbounded output. The adapter uses the generated `AsyncOpenAI.decisions` resource; install the matching build with its `decisions` extra, which requires `openai>=3.41.0,<4`.

## Handle errors and usage

A refusal, malformed service answer, or non-JSON provider value raises `DecisionError`. Its `reason` is `refusal` or `invalid_response`; it retains trustworthy usage and request metadata without retaining the response body. Valid JSON that fails the requested output type raises the existing `OutputValidationError`, as Agent output does. Transport errors keep the OpenAI client's exception types. Malformed HTTP 200 JSON raises a sanitized `DecisionError` with the available request ID; it does not expose the raw body or a body-bearing parser exception.

When tracing is enabled, the optional `decision.usage` span records completed metadata before output validation or a completed-response error. Recording is best effort: a normal failure in that span does not change the decision's result or original error. Cancellation still propagates. The main `decision` span uses the SDK's usual tracing behavior. Missing or invalid usage stays unknown.

## Selecting a target when a native handoff runs

On the matching source build, the [DecisionHandoff example](recipes/decision_handoff.py) has been letting the main model invoke one routing function before Decisions selects its target. `DecisionHandoff(name=..., description=..., question=..., targets={"billing": billing, "technical": technical})` has been declaring named target Agents. The source Agent has been listing that declaration in `handoffs` and supplying its existing `decisions` provider. The Session has been capturing that authorized target roster at opening. The example has been using synthetic Responses and Decisions without API credentials. It has been requiring the [matching source build](../../CONTRIBUTING.md#build-the-sdk-from-source).

The model's function call has been supplying an `input` string with at most 8,192 UTF-8 bytes. The SDK has been passing that explicit input to Decisions and constraining its choices to the captured target names. It has not been adding conversation history automatically. The generative model has been retaining orchestration. Decisions has been choosing only the captured target. The SDK has been rechecking the live source callback before admitting the chosen native transfer. A failed, invalid, or cancelled decision has not been activating a specialist.

The native handoff has been transferring the existing conversation within the same Session, using the selected target's captured Agent configuration. The chosen profile ID has been traveling in private callback metadata rather than model-visible output. Existing static handoffs have been retaining their behavior. `DecisionHandoff` has been supporting native text Agents and rejecting Realtime configurations before native acquisition. `RealtimeHandoff` has been remaining the Realtime routing option.

The captured roster has been representing the application's target authorization for that Session. Applications with independently changing target permissions have been continuing to use the [application routing pattern](#route-to-an-authorized-specialist) and a currently authorized graph. Pending callback-backed handoffs have not been gaining authorization from a restored checkpoint.
