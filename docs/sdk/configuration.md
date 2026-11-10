# Configure an agent

Configure an `Agent` with instructions, tools and an optional output type. The constructor records those defaults without starting the runtime. Opening a Session captures its configuration and starts native resources. `Agent.name` supplies a diagnostic label; it grants no permissions and adds no instructions.

Agent fields are immutable. Construct a new Agent to change defaults for future Sessions, or use `agent.with_output_type(...)` to change the output type. Unknown constructor options and Python model objects fail before native resources open.

## Model and Session settings

Set `model` to a native model identifier, or omit it for the pinned runtime's default. `ModelSettings` supports `reasoning_effort`, `reasoning_summary` and `service_tier`. Omitted settings keep native defaults. `service_tier=None` clears that override. These settings grant no tool or workspace access.

When opening a Session:

- `agent.session(model=...)` overrides the Agent model.
- Supplied `model_settings` keys override Agent settings. Omitted keys keep the Agent or native defaults.
- [Portable restore](state.md#save-and-restore-a-portable-checkpoint) keeps the saved model and settings instead.

SDK version `0.24.0-alpha.7` or later also supports `tool_choice` in `ModelSettings`, for example `model_settings={"tool_choice": "auto"}`. The default is `"auto"`. `"auto"` lets the model choose whether to call tools; `"none"` disables model tool calls; `"required"` requires a call to an offered tool. These string modes do not select a specific tool or add tool permissions. Use the [matching preview wheel](installation.md) for this option.

An open Session keeps its captured configuration. Change instructions or native model settings with the idle [Session update methods](sessions.md#update-tools-instructions-and-model-settings). Another Agent cannot change it. Sessions opened with `NativeAgents` keep these settings fixed.

### Additional controls in development builds

The controls in this section require a [build from matching development source](installation.md#preview-availability). The alpha.7 wheel does not include these controls.

Pass these fields through `model_settings` on the Agent, when opening a Session, or in an idle `session.update(model_settings=...)`. Omitted keys preserve the accepted settings. `None` clears the corresponding SDK override. Providers still determine which settings their models support and which numeric ranges they accept.

| Setting | Value and effect |
| --- | --- |
| `max_output_tokens` | An integer from `0` through `2**64 - 1` for the output cap on an ordinary model request. Internal compaction and reviewer requests do not use this cap. This is separate from a run's total usage budget. |
| `temperature`, `top_p` | Finite numbers forwarded as sampling controls. The provider owns their supported ranges and combinations. |
| `top_logprobs` | An integer from `0` through `255`, requesting token alternatives where the provider supports them. |
| `parallel_tool_calls` | A bool controlling model-side parallel tool calls. It does not set Python callback concurrency. |
| `verbosity` | `"low"`, `"medium"`, or `"high"`, forwarded as response verbosity. |
| `metadata` | A mapping of strings to strings attached to model requests. Keep credentials and private application data out of this mapping. |
| `store` | A bool requesting provider-side response storage. `True` requires API-key authentication. Provider policy determines retention. |
| `prompt_cache_retention` | `"in_memory"` or `"24h"`. |
| `prompt_cache_ttl` | `"30m"` or `"12h"`, sent as `prompt_cache_options.ttl`. TTL and retention are independent; omitting one does not set the other. The provider determines cache behavior. |
| `invocation_timeout_ms` | A positive integer through `4_294_967_295` setting a local deadline for each model invocation, including its internal waits and retries. |

For example, this declaration requests a response cap and a local invocation deadline. It starts no model work:

```python
from agents import Agent

agent = Agent(
    instructions="Answer briefly.",
    model="gpt-5.5",
    model_settings={"max_output_tokens": 512, "invocation_timeout_ms": 30_000},
)
```

Use a standard Responses model for the additional wire controls; known Responses Lite combinations fail locally rather than silently dropping settings. A provider rejection remains a failed request. The SDK does not remove a rejected setting and replay the work.

An invocation deadline raises `NativeError` with `code="model_invocation_deadline_exceeded"`. It is separate from an application deadline for an entire turn: Python tool execution and application waits can outlast a model invocation. Cleanup can also finish after the deadline. An accepted terminal response disarms that invocation's clock before Python result settlement. Check earlier tool effects before retrying. To limit the whole wait, see [bound a wait and stop owned work](troubleshooting.md#bound-a-wait-and-stop-owned-work).

Development builds also accept a `ToolRef` for `tool_choice`, such as a declared function tool's `tool.ref`. This narrows sampling to one currently admitted eager tool and requires it. It cannot add a tool, restore one removed by `tools` or `before_step`, or select a deferred tool. Set `tool_choice="auto"` or `None` to clear a named target. A required choice can request the same tool on later sampling steps, so bound repeated work with a [run budget](observability.md#limit-a-runs-model-requests-and-usage) or change the selection when idle.

## Make typed decisions

Matching development builds have been supporting `Agent.decide()` for booleans, choices and structured values. The `0.24.0-alpha.7` preview has not been including this API. The [typed decision guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/decisions-guide.md) has been providing matching-source setup, complete examples and supported output types.

Applications have been selecting a decision provider through `Agent(decisions=provider)`, separately from the generative `model` and `provider`. Each `await agent.decide(question, input=...)` call has been requiring explicit input and returning a boolean by default. Calls with `choices=[...]` have been returning one of those values; calls with `output_type=...` have been returning a validated value of that type. The SDK has been rejecting calls that combine `choices` and `output_type`.

Decision calls have been using only their explicit input, without starting an Agent run or adding conversation history. A per-call `provider=` has been overriding the Agent's `decisions` default. Calls without either provider have been raising `ValueError`. Applications have been owning decision-provider cleanup; Agent and Session have been borrowing that provider. The [provider guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/decisions-guide.md#replace-the-provider) has been describing `DecisionProvider` and the optional `OpenAIDecisions` adapter.

## Choose tools and workspace access

Declare tools on the Agent or when opening the Session. The Session records Python and manual tool names, together with each MCP server name and raw tool name. Those names limit later updates and turn selections. [Idle updates](tools.md#replace-tools-between-turns) can replace the registered callbacks or MCP connections, including endpoints and credentials. Temporary selections use the accepted bindings and cannot redefine them. The fixed Apps registration from `HostedVerificationMCP` or `ChatGPTAppsMCP` requires a new Session for replacement; see [service-specific integrations](mcp.md#service-specific-integrations).

Pass `environment=...` to `agent.session()` or a one-shot method for a [local workspace](execution.md#local-execution) or a [remote workspace](execution.md#remote-execution). One Agent can open Sessions in different environments. Environment settings and saved state cannot add tool names.

Pass application state through `context=...` and read it in a callback with `ToolContext[Application]`. This state stays separate from model input and tool arguments. See [application context](tools.md#application-context).

## Provider connection

Pass `NativeProvider(base_url=..., api_key=...)` to configure the native Responses connection for model requests. The SDK uses Codex's built-in model client. It does not accept an `openai.OpenAI` or `openai.AsyncOpenAI` instance as the model connection.

Custom endpoints must support Responses. Chat Completions compatibility alone is insufficient. Use the [endpoint checks](providers.md) to test the features your application needs.

API-key connections use these defaults.

- `base_url=None` sends model requests to `https://api.openai.com/v1`. The SDK does not read `OPENAI_BASE_URL`. To use it, import `os` and pass `base_url=os.environ["OPENAI_BASE_URL"]`.
- An omitted key reads `OPENAI_API_KEY` when the Session opens. Keep credentials in current application configuration, outside saved conversation state.
- `response_mode="streaming"` delivers model progress as it arrives.
- In streaming mode, `supports_websockets=None` enables WebSockets for `base_url=None` and disables them for any explicit `base_url`. Set `True` for a compatible custom provider or `False` to require HTTP. When enabled, the native runtime selects the final transport for the provider and model.

The pinned Codex bypasses HTTP proxies with `CODEX_SANDBOX=seatbelt`. To prevent unintended direct requests, the SDK rejects Session startup if `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, or their lowercase forms are nonempty in that mode. The check applies even with a `NO_PROXY` exemption or WebSockets enabled. Run outside that sandboxed environment, or remove unneeded proxy settings.

### Buffer model responses

Use `NativeProvider(response_mode="buffered")` when the endpoint supports nonstreaming Responses and your application can wait for each complete model response. The SDK requests JSON over HTTP and delivers that response's output events after reading its full body. Tool calls start after that model response completes. Tool events remain available while tools execute, and subsequent model requests follow the normal tool loop.

Each buffered HTTP response body is limited to 64 MiB and one million structural characters (`{}[],:`) outside strings. Exceeding either limit fails without retry. Use streaming mode for larger responses.

Set the agent's `model` explicitly to a model that uses standard Responses. The current default model uses Responses Lite and cannot use buffered mode.

If the provider reports failure or an incomplete response that cannot continue, such as a token-limit error, that response's output is not delivered. An undecodable response body also yields no partial output. Any validated response metadata remains in the turn record.

Buffering avoids processing individual streaming fragments. The option applies to every model request in sessions opened with that provider, including restored sessions. Saved state does not choose the mode; the current provider does. Repeat the [endpoint checks](providers.md) with `response_mode="buffered"` for the features your application uses.

Buffered mode requires API-key authentication. It does not support `CodexLogin`, Responses Lite, or native child agents declared with `NativeAgents`. An explicit `supports_websockets=True` conflicts with buffered mode. Unsupported combinations fail before model dispatch; the SDK does not retry the request as a stream.

### Reuse a Codex login (experimental)

Pass `NativeProvider(auth=CodexLogin())` to reuse an existing Codex-managed ChatGPT OAuth login. API-key authentication remains the default when `auth` is omitted.

!!! warning "Local experimental use only"
    This feature may change or be removed. The public beta and general availability of the Agents SDK may ship without this feature. Follow the official [Codex authentication guidance](https://learn.chatgpt.com/docs/auth) and [application authentication policy](https://learn.chatgpt.com/docs/app-server#auth-endpoints) for permitted uses.

Complete `codex login` separately before opening an SDK Session. This mode reads the ChatGPT login saved in `auth.json`. If Codex uses the operating system credential store, configure file storage and sign in again. See [Codex credential storage](https://learn.chatgpt.com/docs/auth#credential-storage) to configure file storage. Protect the login file and keep its tokens out of logs and source control.

`CodexLogin()` looks in `CODEX_HOME` if set and in `~/.codex` otherwise. Pass `CodexLogin(codex_home=...)` to select another existing Codex directory. Pass `account_id=...` to require the expected ChatGPT workspace ID. The Session fails to open if the saved login has a different workspace ID or has no workspace ID.

#### Run a coding turn without an API key

This complete script makes a live model request using the saved login. Run it with an SDK build that includes this experimental API:

```python
import asyncio

from agents import Agent, CodexLogin, NativeProvider


async def main() -> None:
    async with NativeProvider(auth=CodexLogin()) as provider:
        agent = Agent(
            instructions="Write concise Python code with unit tests.",
            provider=provider,
        )
        async with agent.session() as session:
            account = session.codex_account
            if account is None:
                raise RuntimeError("Expected a saved Codex login")
            print("Saved workspace ID:", account.account_id)
            print("Saved plan:", account.plan_type)
            turn = await session.submit(
                "Write an is_prime(n: int) -> bool function and three unit tests."
            )
            result = await turn.get_result()
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

Save the script as `coding_turn.py`. On macOS or Linux, run it with both API-key variables unset.

```bash
env -u OPENAI_API_KEY -u CODEX_API_KEY python coding_turn.py
```

The example asks for code and tests in the answer. To execute commands or edit workspace files, configure an [execution Environment](execution.md).

#### Account identity, refresh, and usage

`session.codex_account` returns a `CodexAccount` with `account_id` and `plan_type` for a Session opened with `CodexLogin`, or `None` in API-key mode. Check `account_id` to identify the saved ChatGPT workspace before submitting work. Some personal logins have no workspace ID and report `account_id=None`. Pass the expected ID to `CodexLogin(account_id=...)` to check it when opening the Session. The saved `plan_type` may be `"unknown"`. For actual usage on that account, check the [Codex usage dashboard](https://chatgpt.com/codex/settings/usage).

The native runtime refreshes the saved login for the same user and workspace. Portable restore and local conversation resume must use that same identity. If you switch users or workspaces, start a new conversation. See [save and restore](state.md#save-and-restore-a-portable-checkpoint).

This mode uses HTTPS on the standard Codex backend. Workspaces that require a separate backend need their own integration. Omit `api_key` and `base_url`, and leave `supports_websockets` unset or set it to `False`. If the saved login fails, the SDK stops even when API keys or other credentials are set.

### Read API-key rate limits

Read `session.rate_limits()` for the latest snapshot from a response with usable rate-limit data. The snapshot starts with unknown values. Each reported window can contain a limit, a remaining count, and the number of seconds until reset as measured at the response. `requests`, `tokens`, and `project_tokens` can each be absent. The provider can also supply a retry delay or an absolute retry time. Treat every `None` as unknown.

For live updates, observe `ApiRateLimitsUpdated` from `agents.observation`. Use its `snapshot` field or read the current Session snapshot after an observation gap. Rate-limit updates can arrive independently of token usage. These snapshots describe the API-key connection. Account plan limits, credits, and billing are outside their scope.

Read a settled failure with [`wait_for_attention()`](sessions.md#wait-for-a-decision-or-a-result). A reported temporary rate limit yields `reason="rate_limited"`. A reported blocking limit, such as exhausted quota, yields `reason="quota_blocked"`. Base that distinction on the failure reason. A remaining count alone cannot tell you whether the next request will succeed.

### Provider lifetime

Each Session captures provider settings and owns its native connections. If neither the Agent nor the Session supplies a provider, the Session creates its own and attempts to close it automatically. Reusing an Agent does not share default providers.

To share a provider, pass the same `NativeProvider` to the Agents or Sessions. After all its Sessions close, call `await provider.aclose()`. Closing while a Session is open or opening raises an error. A successful close prevents reuse. Create a new provider for later Sessions.

### Recover a failed managed-environment opening

If opening a [managed Environment](execution.md#manage-the-executor-lifetime) fails and automatic cleanup cannot finish, the exception can carry the implicit provider in `error.provider`. It may still own an executor manager or native resources. Check this optional attribute and retry `aclose()` before discarding the owning event loop.

This applies only to opening failures from `agent.session()`, `agent.run()` and `agent.run_sync()`. An explicit provider remains application-owned and is not attached to the error. Close it after its other Sessions close.

This fragment uses your `agent` and the linked example's `executor_lease()` manager:

```python
from agents import NativeProvider

try:
    result = await agent.run("Inspect the workspace.", environment=executor_lease())
except BaseException as error:
    recovery = getattr(error, "provider", None)
    if isinstance(recovery, NativeProvider):
        try:
            await recovery.aclose()
        except BaseException as cleanup_error:
            raise error from cleanup_error
    raise
```

Run async cleanup on the failed opening's event loop. If cleanup fails again, this handler keeps the opening error and its provider, with the cleanup error as its cause. Retain the provider and retry cleanup before discarding the loop.

After `run_sync()` fails, a caller outside an event loop can use `asyncio.run(recovery.aclose())`. The provider routes cleanup to the retained run loop.

## Structured output

Set `output_type` on the Agent to choose the final output format. For example, `output_type=Answer` requests a structured object and validates it in Python. This example runs inside an async application:

```python
from pydantic import BaseModel

from agents import Agent


class Answer(BaseModel):
    greeting: str
    topics: list[str]


agent = Agent(instructions="Return a greeting and its topics.", output_type=Answer)
result = await agent.run("Greet Ada about astronomy.")
print(result.final_output.greeting)
```

### Choose an output type

| Desired output | Configuration | Behavior |
| --- | --- | --- |
| Plain text | Omit `output_type` or pass bare `str`. | Return the final text. |
| Native strict schema | Pass a structured type or `NativeOutput(Answer)`. | Request native schema output and validate it in Python. `NativeOutput(str)` requests a native response object rather than plain text. |
| Prompted JSON | Pass `PromptedOutput(Answer)`. | Ask for JSON in the prompt and validate the text in Python. The provider does not enforce a native schema. |

Native output supports objects with declared fields and non-object roots such as `int` and `list[int]`. For a union or nullable root, use `RootModel[int | str]` or `RootModel[int | None]`. The static `Agent` constructor rejects bare unions such as `int | str`. Use `type(None)` for output that must be `null`.

`PromptedOutput` defaults to `max_retries=0`, so invalid output fails without a correction attempt. Nonzero retries request separate corrections without tools; they do not replay the task. `LocalStorage` supports only `max_retries=0`.

For a native non-object root, the model returns an object with a required `response` field. The SDK unwraps that field. With `output_type=list[int]`, model output `{"response": [1, 2]}` becomes `result.final_output == [1, 2]`. A `RootModel` result keeps its class. Native history and observations retain the JSON object. Object outputs with their own `response` field remain unchanged.

### Native schema limits

These limits also apply inside non-object roots and `RootModel` types:

- Every nested object must reject extra fields. Open maps such as `dict[str, str]`, including maps inside Pydantic models, and models allowing extra fields are rejected.
- `Any`, sets, fixed-length tuples, and schemas using `oneOf`, `allOf`, `prefixItems` or `uniqueItems` are unsupported. The SDK rejects unsupported schemas when the Session opens.
- Every declared field is required, even with a Python default. The SDK removes defaults from the model request. Use nullable fields for explicit `null` values. A nullable `response` field is still required.

These restrictions do not automatically apply to prompted output or Python function parameters.

### Handle validation failures

Invalid final output raises `OutputValidationError`, including after prompted corrections exhaust their retries. Catch it around `agent.run()` or `turn.get_result()`. Its message omits the rejected text. Its `raw_output` contains that text and may contain private data, so keep it out of routine logs.

Access `error.issues` deliberately when diagnosing a failure. Each `OutputValidationIssue` contains a validator `code` and a tuple `path` into the application output. For example, `("items", 0, "count")` identifies the first item's count. Native non-object roots omit the SDK's `response` prefix. Failures in that wrapper use the root path `()`. Invalid JSON also uses the root path. The SDK captures these diagnostics during the original validation. It does not run application validators again. It retains no validator exception or message as a cause. Codes and field names can themselves contain private data, so treat `issues` with the same care as `raw_output`.

This fragment reuses the typed `agent` above:

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

### Inspect corrections and saved state

On a successful run, `Completed.repair_attempts` records each correction attempt as a `RecordedTurn` value. Each has native session and turn IDs, `parent_session_id` and `parent_turn_id` for the original task, and usage for that correction turn:

```python
for repair in result.repair_attempts:
    print(repair["id"], repair["parent_turn_id"], repair["usage"])
```

`Completed.thread_usage` is cumulative for the original thread and excludes correction threads. Missing usage is `None`, not zero. Follow the [usage accounting limits](observability.md#read-usage-without-double-counting) when comparing successive snapshots.

Corrections receive the invalid output, schema and effective model connection. They receive none of the original tools, application context or instructions. `final_output` may come from a correction, while the original native history stays intact. `session.turns()` and `session.items()` retain correction records beside their parent.

Corrections have been retaining the accepted model, reasoning effort, reasoning summary and service tier. Matching development builds have also been applying the accepted `invocation_timeout_ms` afresh to each correction. The alpha.7 preview has not been including this timeout setting. Corrections have not been inheriting the other [development model controls](#additional-controls-in-development-builds), including output caps, sampling controls, metadata, provider storage and prompt-cache settings.

Live manual replies and idle restore support structured output. An explicit Session can suspend a settled manual turn with `NativeOutput`, a bare structured output type, or `PromptedOutput`. Restore must match the saved output mode and schema, including the retry setting for prompted output.

Automatic one-shot checkpoints support native strict output but exclude `PromptedOutput`. See [saving a settled manual request](state.md#save-a-settled-manual-request) for output matching, active repair and `before_step` limits, and [automatic checkpoint limits](state.md#automatic-checkpoint-limits) for one-shot restrictions.

## Choose a model-context hook

Use `Agent(transform_context=callback)` to change the input items sent to the model. The callback receives a `ModelRequestContext` from `agents` before each sampling request. It runs for root turns, native children, model-based compaction, and output repair. This is the starting point for filtering or replacing model context.

`request.items` contains detached Responses-shaped input in native context order. Return replacement items, or `None` to keep the original input. Mutating items and returning `None` discards the edits. Keep tool-call identities and their matching results together. Request-only changes leave canonical history and other logs intact; they do not redact stored data.

This example replaces a synthetic private value in ordinary function results before the model receives them:

```python
from typing import Any

from agents import Agent, ModelRequestContext


def project_context(request: ModelRequestContext[None]) -> list[dict[str, Any]]:
    items = list(request.items)
    for item in items:
        if item.get("type") == "function_call_output" and isinstance(item.get("output"), str):
            item["output"] = item["output"].replace("synthetic-private-value", "[redacted]")
    return items


agent = Agent(instructions="Answer using the available context.", transform_context=project_context)
```

Use `purpose` (`"turn"`, `"compaction"`, or `"output_repair"`) and `subagent_id` to select which requests to change. The request also supplies application `context`, `session_id`, `turn_id`, `request_id`, and `model`. Native children use the parent Python Session's `session_id` with their own `turn_id` and `subagent_id`. Output repair reports its separate internal Session's identifiers.

Transport retries reuse the captured replacement. A new sampling request calls the callback again. Async callbacks run on the Session event loop; sync callbacks run in a worker. Cancellation joins admitted callback work and discards late results. Keep callbacks free of business effects.

Context items may already contain truncated tool results. Use [tool hooks](tools.md#inspect-or-change-tool-values), such as `function_tool(before=..., after=...)` and MCP `transform_result`, to change execution arguments or full results before native history records them.

Choose the hook by what it owns:

| Desired change | Supported hook |
| --- | --- |
| Replace model input for roots, children, compaction, or repair | `transform_context` with `ModelRequestContext`. |
| Select or narrow admitted tools before a step | [`before_step`](sessions.md#update-tools-instructions-and-model-settings). Tool authority remains bounded by the Session's opening selection. |
| Edit advanced HTTP request presentation in a restricted profile | [`prepare_request`](#edit-the-next-sampling-request). |
| Transform execution arguments or results | [Tool hooks](tools.md#inspect-or-change-tool-values). |

These hooks have separate owners. A context projection does not change admitted tools or execution policy.

## Check a proposed completion

`before_complete` requires [`0.24.0-alpha.7` or later](installation.md). Set `Agent(before_complete=callback)` to check a proposed root or child answer before the turn completes. The callback receives `CompletionContext` from `agents`, including application `context`, proposed `text`, native identity, and `continuation_count`.

Return `None` to accept, `ContinueTurn("specific feedback")` to continue the same turn, or `StopTurn(...)` to stop it. Continuation feedback becomes model-visible conversation data and saved history. Bound root continuations with [`RunBudget`](observability.md#limit-a-runs-model-requests-and-usage). Root budgets exclude children, so use `continuation_count` and `StopTurn` to bound child continuations.

The callback sees proposed text before output-type validation. Rejected text may already have streamed and remains in history. The callback does not run for provider failures, cancellation, compaction, or separate prompted-output repairs. A raised exception fails the turn. Async callbacks run on the Session loop; sync callbacks run in a worker. Cancellation joins started callbacks. A callback must not mutate its own Session.

Matching development builds have also been supplying `CompletionContext.decide()` for checks with the captured [decision provider](#make-typed-decisions). The [completion-check example](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/decisions-guide.md#bind-a-provider-to-an-agent) has been combining a typed decision with a bounded continuation.

## Edit the next sampling request

`prepare_request` requires `0.24.0-alpha.7` or later. Install a [matching preview wheel](installation.md) before using it.

Pass `prepare_request=callback` to `agent.run()`, `agent.run_sync()`, or `session.submit()` for advanced edits to a detached copy of the next HTTP model request. Use it to change sampling instructions or selected tool descriptions and parameter schemas within the profile below. Prefer [`transform_context`](#choose-a-model-context-hook) for model input, [`before_step`](sessions.md#update-tools-instructions-and-model-settings) for tool selection, and [tool policies](tools.md#inspect-or-change-tool-values) for execution arguments or results.

The synchronous or asynchronous callback receives `callback(turn, request, *, preparation_index, recovery_attempt)`. `request` is a `PreparedRequest` imported from `agents`, with `instructions`, `input`, and `tools`. Return the complete edited value, or `None` to use the original request. Mutating the copy and returning `None` discards the edits. The SDK captures returned JSON when the callback settles.

`input` contains native ResponseItem JSON, including JSON strings for function arguments. Preserve call identities when processing tool results. Optional fields that native serialization omits must remain omitted; explicit nulls or unsupported schema keywords can fail validation before dispatch. Each selected tool keeps its namespace, name, kind, strictness, and loading mode. Only its `description` and `parameters` can change.

These edits affect sampling. Canonical conversation history, admitted tool routing, Python validators, and approval policies remain authoritative. Expanding a sampled enum does not authorize the new value. Ordinary MCP tools still rely on the server or application policy for argument validation.

### Choose a supported request-editing profile

Use an explicitly HTTP provider, such as `NativeProvider(supports_websockets=False)`, and a standard Responses model such as `gpt-5.5`. Lite models and WebSockets are unsupported. Select only eager ordinary Python, manual, or MCP tools with `defer_loading=False`. Request editing does not support native children, hosted tools, an Environment, or programmatic tool calling.

The callback must tolerate repeated preparation and avoid business effects. HTTP retries reuse the captured edit. Native stream recovery invokes the callback again; `preparation_index` can advance without a tool result, and `recovery_attempt` identifies recovery. Inspect input and call identities instead of using the index as evidence that a tool completed. Cancellation joins callback settlement before later work starts, so a blocking callback can delay cancellation.

Native output and `PromptedOutput(max_retries=0)` are supported. Separate prompted-output corrections cannot carry this callback. One-shot manual tools require `on_interaction`; without it the run cannot create an automatic pending checkpoint.

An active edited operation cannot suspend or export portable state. Save after it settles. Automatic compaction needed before or during sampling fails before the hidden compaction request; earlier tool effects remain. Shorten the input or start a new operation without the editor after checking those effects. Compaction due after completion does not discard the completed answer.

The complete synthetic [prepared-request recipe](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/prepared_request.py) edits a request after a real Python tool result and checks the outgoing instructions, input, and schema. From the matching source root, run `PYTHONPATH=. python -m examples.codex.recipes.prepared_request`. It needs no model key and uses a local HTTP Responses peer.

## Telemetry

Configure exporters, startup, shutdown, and content controls in the [observability guide](observability.md#configure-telemetry).

### Continue a trace across workers

Use [trace context and worker propagation](observability.md#continue-a-trace-across-workers) to connect Session and tool work across workers.

## Programmatic tool calling

Set `programmatic_tool_calling=True` on `Agent` to let the model generate JavaScript cells that call declared Python, manual and MCP tools. Cells can combine calls with loops, conditions and result processing. Ordinary tool calls do not require this option, which defaults to off.

You write the application and function tools in Python. Model-generated cells run in the SDK's bundled Codex Code Mode host, which embeds V8. The cells have no Python-language option.

These limits apply:

- Python functions handle arguments and results. MCP result policies run before cells receive results.
- The Code Mode host grants no shell tools or workspace access by itself. For native workspace execution, pass an [Environment](execution.md) with executor tools selected to the Session or one-shot run.
- A manual call waits for an application reply in the live Session. Pending code cells cannot be saved as portable checkpoints.
- `exec` and `wait` are reserved function names.

In `0.24.0-alpha.7` or later, `LocalStorage` rejects `programmatic_tool_calling=True` before opening. Use an ephemeral Session for code mode; see [storage support limits](state.md#choose-how-to-store-a-conversation) and the [matching preview wheel](installation.md).

Restore uses the current Agent declaration. Set `programmatic_tool_calling=True` again if the restored conversation needs it. [Executor argument and result transforms](execution.md#executor-policies) also support direct calls without this option.

## Plan the application lifecycle

The SDK runs inside your application, which manages its workers, storage and credentials. Saving a checkpoint does not provide a hosted recovery service.

| Application decision | Supported path |
| --- | --- |
| When to initialize diagnostics | Configure [telemetry](observability.md#configure-telemetry) before the first Session opens. |
| Which work belongs to a conversation | Own each [Session](sessions.md) explicitly and choose queueing or steering when submissions overlap. |
| Where history survives a restart | Choose [portable checkpoints or a local profile](state.md#choose-how-to-store-a-conversation). Retain the required state or conversation ID in application storage. |
| What tools may do | Supply current [tools and execution permissions](tools.md). Restored history does not supply current credentials or grant new authority. |
| Whether work can be retried | Inspect [effect metadata and history](sessions.md#failures-and-recovery), together with your application's records of external effects. |
| How to shut down | Close owned Sessions, then close any [explicit providers](#provider-lifetime) after their Sessions are closed. Shut down [telemetry](observability.md#configure-telemetry) after all Sessions close. |
