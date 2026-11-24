# Decisions in the Python Agents SDK

## Ask a typed question

Set `Agent.decisions` once. Each call sends only the question, explicit input and requested output schema. This example makes paid requests when invoked.

```python
from typing import Literal

from pydantic import BaseModel

from agents import Agent, AgentHooks, OpenAIDecisions, Runner, function_tool
from agents.tool_context import ToolContext


class Ticket(BaseModel):
    urgent: bool
    team: Literal["billing", "support"]


@function_tool
async def classify(ctx: ToolContext[None], report: str) -> str:
    return await ctx.decide("Which team?", input=report, choices=["billing", "support"])


async def check(run, proposal):
    ready = await proposal.decide("Does the reply include a next step?", input=proposal.output)
    return "finish" if ready else "continue"


async def answer_ticket(text: str):
    async with OpenAIDecisions() as provider:
        agent = Agent(
            name="Help",
            decisions=provider,
            tools=[classify],
            hooks=AgentHooks(final_output_policy=check),
        )
        ticket = await agent.decide("Classify this request.", input=text, output_type=Ticket)
        reply = await Runner.run(agent, text, max_turns=3)
        return ticket, reply.final_output
```

Without `choices` or `output_type`, the result is a boolean. The built-in provider uses a probability threshold of 0.5. Choose one output form per call. Supported types include finite scalar choices, required nested records, integers spanning up to 255 values and floats with integer bounds up to 9 apart. Unsupported schemas fail before dispatch. Input can be text or model input items. The SDK adds no conversation history or instructions.

Within one run, a handoff target inherits the root provider unless it declares its own. Nested runs, including agents as tools, start with their own provider. Within a function tool, a model input filter or a final output policy, call `decide` on the live `ToolContext`, `CallModelData` or `ProposedFinalOutput` respectively. Use `provider=...` for a single call or implement `DecisionProvider.get_decision` for another backend. An answer grants no permissions.

Use `async with OpenAIDecisions()` or call `aclose()` for the default client. With `OpenAIDecisions(client=client)`, the caller owns the client. The built-in adapter needs an OpenAI client version with Decisions support and disables retries. Decision calls and tokens are outside Runner usage and budgets. With tracing enabled, the `decision.usage` span records available metadata without the question, input or answer. Missing usage is unknown.

## Let the primary model request a specialist

Use `agents.decisions.decision_handoff` when the primary model should decide **when** to transfer. Decisions chooses **which** specialist from the labels your application currently allows. If the primary model answers directly, there is no Decisions request.

[Run the offline comparison](compare_handoffs.py) from the repository root without credentials.

```bash
uv run python examples/decisions/compare_handoffs.py
```

The example compares direct and Decisions handoffs on the same three labeled inputs. Both paths use the same scripted specialists and settings. Route accuracy measures the fraction routed correctly. Task completion measures the fraction with the expected final answer. Each row also reports total elapsed time inside Runner and request counts. Estimated cost is unknown (`null`). The predictions include deliberate mistakes. These fixtures check the workflow and scoring. They provide no evidence of live accuracy, latency or cost.

For live requests, configure a provider on the source agent. This function makes paid calls when invoked.

```python
from openai import AsyncOpenAI

from agents import Agent, OpenAIDecisions, Runner
from agents.decisions import decision_handoff


async def answer(client: AsyncOpenAI, text: str):
    source = Agent(
        name="Triage",
        instructions="Transfer requests that need a specialist.",
        decisions=OpenAIDecisions(client=client),
    )
    targets = {
        "billing": Agent(name="Billing", instructions="Help with invoices."),
        "account": Agent(name="Account", instructions="Help with account settings."),
    }
    route = decision_handoff(
        source,
        question="Which team can help?",
        targets=targets,
    )
    source.handoffs.append(route)
    return await Runner.run(source, text)
```

Use a client with Decisions support. The caller owns its lifetime. Give each clone its own handoff list and selector. For one fixed target, use an ordinary `handoff(target)`.

- **Evidence.** The model supplies one nonblank `input` string of up to 8,192 UTF-8 bytes. The provider receives that string, the question and the allowed choices. The model can copy sensitive text into the string. The specialist receives the usual handoff history. Set the handoff's `input_filter` when the specialist needs less context.
- **Authority.** The handoff belongs to the source agent. Labels bind to the original agent objects and names. Removing, replacing or renaming a target makes it unavailable. Use a synchronous, side-effect-free `is_available(ctx, label)` to check application permissions. The handoff checks the selected route again before transfer. Runner keeps the run context, call IDs, tool restrictions and approvals.
- **Failures.** Refusals and invalid answers raise `DecisionError`. Revoked routes raise `ValueError`. The provider deadline defaults to 30 seconds. On timeout or cancellation, the SDK waits for provider cleanup and discards late answers. Timeout raises `asyncio.TimeoutError`. If the provider cancels its own request, the SDK raises `RuntimeError`. Transport errors propagate. There is no automatic fallback or retry. To return an error to the primary model, explicitly set the handoff's `failure_error_function`. For immediate stream cancellation, call `result.cancel()` and drain `result.stream_events()`.
- **Error privacy.** When either model or tool data logging is disabled, the handoff removes routing traceback frames and exception chains before propagating a failure. Provider errors and metadata can still contain sensitive data. Keep custom error messages and recovery callbacks safe to share.
- **Scope.** Both Runner modes support specialist selection. Decisions cannot invoke a tool, grant permissions or tell the source to continue. Realtime and dynamic agent factories are outside this integration.
- **Available labels.** The model sees the handoff only when 2 to 255 labels are available. If the count falls outside that range before invocation, the handoff raises `ValueError` without asking the provider or transferring. The SDK never transfers by default.

### Resume a specialist after a tool approval

The handoff registers its targets for `RunState` automatically. Save `result.to_state().to_string()` when the run pauses. Protect the snapshot from edits. Restore only trusted snapshots with the same source, handoff names, labels and specialists. When rebuilding the graph, you authorize the agents, tools and permission checks you supply under those names.

If you use `is_available`, pass fresh application permissions through `context_override`. The callback should read them from `ctx.context`. Saved permissions alone cannot authorize the active specialist. For a nested `Agent.as_tool()` run, pass the override when restoring the outer run. The nested specialist shares the application context and keeps its own approvals.

```python
from agents import RunState

state = await RunState.from_string(source, saved, context_override=current_context)
```

Use a separate specialist instance for each label and ordinary handoff. A saved specialist identifies one route. Both loading and resuming check the active specialist, including pending agent tools. Missing bindings or denied access stop the resume before dispatch. Decisions does not run again. Review each pending call in `state.get_interruptions()`, then approve or reject it and resume with `await Runner.run(source, state)`.

Keep the graph and grants current. If selection fails after a tool has run, keep the updated `RunState` and inspect completed results before retrying.

### Evaluate routing separately

Compare ordinary and Decisions handoffs on held-out labels with the same specialists. Record wrong transfers, refusals, end-task outcomes, latency and any available usage. The offline comparison cannot establish which route is better for your workload.

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
