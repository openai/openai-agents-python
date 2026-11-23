# Decisions in the Python Agents SDK

## Let the primary model request a specialist

Use `agents.decisions.decision_handoff` when the primary model should decide **when** to transfer. Decisions chooses **which** specialist from the labels your application currently allows. If the primary model answers directly, there is no Decisions request.

[Run the offline comparison](compare_handoffs.py) from the repository root without credentials.

```bash
uv run python examples/decisions/compare_handoffs.py
```

The example compares direct and Decisions handoffs on the same three labeled inputs. Both paths use the same scripted specialists and settings. Route accuracy measures the fraction routed correctly. Task completion measures the fraction with the expected final answer. Each row also reports total elapsed time inside Runner and request counts. Estimated cost is unknown (`null`). The predictions include deliberate mistakes. These fixtures check the workflow and scoring. They provide no evidence of live accuracy, latency or cost.

For live requests, pass a caller-owned `AsyncOpenAI` client and a supported Decisions model. This function makes paid calls when invoked.

```python
from openai import AsyncOpenAI

from agents import Agent, Runner
from agents.decisions import OpenAIDecisions, decision_handoff


async def answer(client: AsyncOpenAI, decisions_model: str, text: str):
    source = Agent(name="Triage", instructions="Transfer requests that need a specialist.")
    targets = {
        "billing": Agent(name="Billing", instructions="Help with invoices."),
        "account": Agent(name="Account", instructions="Help with account settings."),
    }
    source.handoffs.append(
        decision_handoff(
            source,
            provider=OpenAIDecisions(client, model=decisions_model),
            question="Which team can help?",
            targets=targets,
            timeout=10,
        )
    )
    return await Runner.run(source, text)
```

- **Evidence.** The model supplies one nonblank `input` string of up to 8,192 UTF-8 bytes. The provider receives that string, the question and the allowed choices. The model can copy sensitive text into the string. The specialist receives the usual handoff history. Set the handoff's `input_filter` when the specialist needs less context.
- **Authority.** The handoff belongs to the source agent. Create a new handoff for a clone. Labels bind to the original agent objects and names. Removing, replacing or renaming a target makes it unavailable. Use a synchronous, side-effect-free `is_available(ctx, label)` to check application permissions. The handoff checks the selected route again before transfer. Runner keeps the run context, call IDs, tool restrictions and approvals.
- **Failures.** Refusals and invalid answers raise `DecisionError`. Revoked routes raise `ValueError`. The provider deadline defaults to 30 seconds. On timeout or cancellation, the SDK waits for provider cleanup and discards late answers. Timeout raises `asyncio.TimeoutError`. If the provider cancels its own request, the SDK raises `RuntimeError`. Transport errors propagate. There is no automatic fallback or retry. To return an error to the primary model, explicitly set the handoff's `failure_error_function`. For immediate stream cancellation, call `result.cancel()` and drain `result.stream_events()`.
- **Error privacy.** When either model or tool data logging is disabled, the handoff removes routing traceback frames and exception chains before propagating a failure. Provider errors and metadata can still contain sensitive data. Keep custom error messages and recovery callbacks safe to share.
- **Scope.** Both Runner modes support specialist selection. Decisions cannot invoke a tool, grant permissions or tell the source to continue. Realtime and dynamic agent factories are outside this integration.
- **Available labels.** With `OpenAIDecisions`, the model sees the handoff only when 2 to 255 labels are available. If the count falls outside that range before invocation, the handoff raises `ValueError` without asking the provider or transferring. A custom provider can select a single available label. The SDK never transfers by default.

### Resume a specialist after a tool approval

The handoff registers its targets for `RunState` automatically. Save `result.to_state().to_string()` when the run pauses. Protect the snapshot from edits. Restore only trusted snapshots with the same source, handoff names, labels and specialists. When rebuilding the graph, you authorize the agents, tools and permission checks you supply under those names.

If you use `is_available`, pass fresh application permissions through `context_override`. The callback should read them from `ctx.context`. Saved permissions alone cannot authorize the active specialist. For a nested `Agent.as_tool()` run, pass the override when restoring the outer run. The nested specialist shares the application context and keeps its own approvals.

```python
from agents import RunState

state = await RunState.from_string(source, saved, context_override=current_context)
```

Restore checks each Decisions label bound to the active specialist. All must still be available, even after a direct handoff to the same agent. Use separate agents if routes need independent permissions. A missing binding or denied label stops restore. Decisions does not run again. Review each pending tool call in `state.get_interruptions()` before calling `state.approve(item)` or `state.reject(item)`. Resume with `await Runner.run(source, state)`.

If a handoff fails during approval resume, retain the updated `RunState`. It contains completed tool results that the session may lack. Keep the graph and permissions current through execution. When resuming an in-memory state directly, recheck access yourself. The restore checks run only when loading serialized state.

## Ask for a value without transferring

Call `await decide(question, provider=provider, input=evidence)` from `agents.decisions`. Supply nonempty text or a nonempty sequence of finite JSON objects. The call copies them before the first await. It never reads run history. The call returns a `DecisionResult`. By default, `result.value` is a boolean. Testing the wrapper itself with `if result` or `bool(result)` raises `TypeError`.

```python
from agents.decisions import decide

result = await decide("Is the number even?", provider=provider, input="2")
if result.value:
    print("Even")
```

Use `choices` for distinct finite JSON scalars or `output_type` for a Pydantic-compatible type. The SDK validates the returned JSON against the schema and the application parser.

A trusted `DecisionProvider` implements `async get_decision(request)` and returns `DecisionResult(value, request_id, model, usage)`. Both the result and `DecisionError` expose observed metadata. Missing metadata is `None`. Error reasons are `refusal` and `invalid_response`. The handoff adds reported usage to the run. Standalone calls leave accounting to the application.

`OpenAIDecisions` requires `openai>=3.26.0` with the Decisions resource. It uses a predicate threshold of 0.5 or 2 to 255 string or boolean choices. Standalone calls reject other schemas before inference. Use a custom provider for other contracts. The adapter disables client retries and leaves the client open.

In an async callback, use `async with decision_scope(ctx)` and pass `owner=ctx` to `decide`. Only the exact owner in that live scope can use the bound call. Scope exit cancels and joins outstanding provider work. The application remains responsible for awaiting its own child tasks.

## Direct client recipes

These examples compose the native `AsyncOpenAI.decisions.create()` client with existing Agents SDK APIs. They do not add a Decisions model adapter or a generic risk policy.

From the repository root, set `OPENAI_API_KEY` in your environment and run:

```bash
uv run --with 'openai>=3.26.0,<4' python -m examples.decisions.classify
uv run --with 'openai>=3.26.0,<4' python -m examples.decisions.model_router
uv run --with 'openai>=3.26.0,<4' python -m examples.decisions.tool_risk_gate
uv run --with 'openai>=3.26.0,<4' python -m examples.decisions.tool_risk_gate --require-approval
```

The dependency override supplies the native Decisions resource without modifying the repository lockfile. These commands make paid API calls. The publication tool is a simulation; it only returns text.

The repository example runner (`make examples-run`) applies the same dependency override when it launches these recipes.

- `classify.py` asks predicate, choice, and score questions, indexes answers by name, and uses the predicate in a blocking input guardrail. A refusal or missing scope answer blocks the run. Other questions may refuse independently.
- `model_router.py` classifies a single text request before the run and passes the selected model to `agent.clone()`. A refusal or missing route uses the application's `powerful` fallback. API errors propagate.
- `tool_risk_gate.py` applies an illustrative editorial policy to announcement arguments. The default mode rejects content that needs review before the tool runs. `--require-approval` instead pauses for a human decision; it does not automatically approve interruptions. Refusals require review, and API errors stop the attempted operation.

Choose thresholds using labeled data from your application. A classifier is not an authorization mechanism. Enforce actual permissions in trusted application code.

The tool recipe evaluates only the announcement arguments, not conversation history, attachments, or user authorization. Conditional approval callbacks are skipped when decorated tool arguments require conversion or defaults; the SDK requires manual approval instead. Resuming an explicitly approved call does not re-run the conditional approval callback. Tool input guardrails can run again before invocation, including after an approval interruption.

These recipes accept text rather than implementing general conversation or media conversion. Pre-run routing does not re-route handoffs or resumed runs and cannot inspect server-held conversation history. See the [Decisions guide](https://developers.openai.com/api/docs/guides/decisions) for the native API's input formats and [SDK model documentation](https://openai.github.io/openai-agents-python/models/) for integration guidance.
