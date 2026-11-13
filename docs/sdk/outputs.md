# Structured output

Set `Agent.output_type` to return a validated Python value instead of plain text. Use [completion checks](#check-a-proposed-completion) to inspect an answer before acceptance, and [output guardrails](guardrails.md) to reject a converted value. Validation checks the output format; your application still checks whether the answer is correct for its task.

## Return a typed result

[Install the SDK](installation.md) and set `OPENAI_API_KEY` as described in [your first agent](index.md#run-your-first-agent). Save this complete script as `typed_output.py`, then run `python typed_output.py`. It makes live model requests that can incur charges.

```python
import asyncio

from pydantic import BaseModel

from agents import Agent


class Answer(BaseModel):
    greeting: str
    topics: list[str]


async def main() -> None:
    agent = Agent(
        instructions="Return a greeting and its topics.",
        model="gpt-5.5",
        output_type=Answer,
    )
    result = await agent.run("Greet Ada about astronomy.")
    print(result.final_output.greeting)
    print(result.final_output.topics)


if __name__ == "__main__":
    asyncio.run(main())
```

The script prints a greeting and a list of topics. The model chooses the wording and topic names. `result.final_output` is an `Answer` instance, so you can read its fields directly. The SDK requests native schema output and validates the result in Python.

## Choose an output type

Import `NativeOutput` and `PromptedOutput` from `agents` when using those wrappers. Import `RootModel` from `pydantic` for the union and nullable-root examples below.

| Desired output | Configuration | Behavior |
| --- | --- | --- |
| Plain text | Omit `output_type` or pass bare `str`. | Return the final text. |
| Native strict schema | Pass a structured type or `NativeOutput(Answer)`. | Request native schema output and validate it in Python. `NativeOutput(str)` requests a native response object rather than plain text. |
| Prompted JSON | Pass `PromptedOutput(Answer)`. | Ask for JSON in the prompt and validate the text in Python. The provider does not enforce a native schema. |

Native output supports objects with declared fields and non-object roots such as `int` and `list[int]`. For a union or nullable root, use `RootModel[int | str]` or `RootModel[int | None]`. The static `Agent` constructor rejects bare unions such as `int | str`. Use `type(None)` for output that must be `null`.

`PromptedOutput` defaults to `max_retries=0`, so invalid output fails without a correction attempt. Nonzero retries request separate corrections without tools; they do not replay the task. `LocalStorage` supports only `max_retries=0`.

For a native non-object root, the model returns an object with a required `response` field. The SDK unwraps that field. With `output_type=list[int]`, model output `{"response": [1, 2]}` becomes `result.final_output == [1, 2]`. A `RootModel` result keeps its class. Native history and observations retain the JSON object. Object outputs with their own `response` field remain unchanged.

## Native schema limits

These limits also apply inside non-object roots and `RootModel` types:

- Every nested object must reject extra fields. Open maps such as `dict[str, str]`, including maps inside Pydantic models, and models allowing extra fields are rejected.
- `Any`, sets, fixed-length tuples, and schemas using `oneOf`, `allOf`, `prefixItems` or `uniqueItems` are unsupported. The SDK rejects unsupported schemas when the Session opens.
- Every declared field is required, even with a Python default. The SDK removes defaults from the model request. Use nullable fields for explicit `null` values. A nullable `response` field is still required.

These restrictions do not automatically apply to prompted output or Python function parameters.

## Handle validation failures

Invalid final output raises `OutputValidationError`, including after prompted corrections exhaust their retries. Catch it around `agent.run()` or `turn.get_result()`. Its message omits the rejected text. Its `raw_output` contains that text and may contain private data, so keep it out of routine logs.

Access `error.issues` deliberately when diagnosing a failure. Each `OutputValidationIssue` contains a validator `code` and a tuple `path` into the application output. For example, `("items", 0, "count")` identifies the first item's count. Native non-object roots omit the SDK's `response` prefix. Failures in that wrapper use the root path `()`. Invalid JSON also uses the root path. The SDK captures these diagnostics during the original validation. It does not run application validators again. It retains no validator exception or message as a cause. Codes and field names can themselves contain private data, so treat `issues` with the same care as `raw_output`.

To handle invalid output in the example above, import `OutputValidationError` from `agents`. Inside `main()`, replace the `agent.run()` call and its print statements with this fragment:

```python
from agents import OutputValidationError

try:
    result = await agent.run("Greet Ada about astronomy.")
except OutputValidationError:
    print("The answer did not match the required format.")
else:
    print(result.final_output.greeting)
```

Validation does not undo completed tools or replay the task. Waiting on the same Turn raises the same failure. An explicit Session can accept a new turn, but inspect history and application effect records before submitting input that could repeat an effect.

## Inspect corrections and saved state

For `PromptedOutput` with `max_retries` greater than zero, the SDK can request a formatting correction after invalid output. To allow one correction in the example above, import `PromptedOutput` from `agents` and set `output_type=PromptedOutput(Answer, max_retries=1)` on the Agent. The original native strict-output example does not opt into these prompted corrections. On a successful run, `Completed.repair_attempts` records each correction attempt as a `RecordedTurn` value. Each has native session and turn IDs, `parent_session_id` and `parent_turn_id` for the original task, and usage for that correction turn. This fragment belongs inside `main()` after a successful `agent.run()` call:

```python
for repair in result.repair_attempts:
    print(repair["id"], repair["parent_turn_id"], repair["usage"])
```

`Completed.thread_usage` is cumulative for the original thread and excludes correction threads. Missing usage is `None`, not zero. Follow the [usage accounting limits](observability.md#read-usage-without-double-counting) when comparing successive snapshots.

Corrections receive the invalid output, schema and effective model connection. They receive none of the original tools, application context or instructions. `final_output` may come from a correction, while the original native history stays intact. `session.turns()` and `session.items()` retain correction records beside their parent.

Corrections retain the accepted model, reasoning effort, reasoning summary and service tier. Matching development builds also apply the accepted `invocation_timeout_ms` afresh to each correction. The alpha.7 preview does not include this timeout setting. Corrections do not inherit the other [development model controls](models.md#additional-controls-in-development-builds), including output caps, sampling controls, metadata, provider storage and prompt-cache settings.

Live manual replies and idle restore support structured output. An explicit Session can suspend a settled manual turn with `NativeOutput`, a bare structured output type, or `PromptedOutput`. Restore must match the saved output mode and schema, including the retry setting for prompted output.

Automatic one-shot checkpoints support native strict output but exclude `PromptedOutput`. See [saving a settled manual request](state.md#save-a-settled-manual-request) for output matching, active repair and `before_step` limits, and [automatic checkpoint limits](state.md#automatic-checkpoint-limits) for one-shot restrictions.

## Check a proposed completion

The complete [saved-report example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/completion.py) requires the agent to save a report before accepting its answer. It uses a request budget to bound additional work and runs with alpha.7 or later.

`before_complete` requires [`0.24.0-alpha.7` or later](installation.md). Set `Agent(before_complete=callback)` to check a proposed root or child answer before the turn completes. The callback receives `CompletionContext` from `agents`, including application `context`, proposed `text`, native identity, and `continuation_count`.

Return `None` to accept, `ContinueTurn("specific feedback")` to continue the same turn, or `StopTurn(...)` to stop it. Continuation feedback becomes model-visible conversation data and saved history. Bound root continuations with [`RunBudget`](observability.md#limit-a-runs-model-requests-and-usage). Root budgets exclude children, so use `continuation_count` and `StopTurn` to bound child continuations.

For model-generated answers, the callback receives proposed `text` before output-type validation. Rejected text may already have streamed and remains in history. The callback does not run inside the separate prompted-output repair agent. Repaired text returns through the original completion callback before conversion. Provider failures, cancellation, and compaction do not invoke this callback. A raised exception fails the turn. Async callbacks run on the Session loop; sync callbacks run in a worker. Cancellation joins started callbacks. A callback must not mutate its own Session.

In matching development builds, completions from a terminal tool or tool batch supply converted `final_output` instead of `text`. Use `tool_call` or `tool_batch_call_ids` to identify those completions. [Output guardrails](guardrails.md) check converted output across completion sources.

Matching development builds also supply `CompletionContext.decide()` for checks with the captured [decision provider](configuration.md#make-typed-decisions). The [completion-check example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/decisions-guide.md#bind-a-provider-to-an-agent) combines a typed decision with a bounded continuation.
