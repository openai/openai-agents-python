# Configure agent behavior

<a id="configure-an-agent"></a>

Configure an `Agent` with instructions, tools, and an optional output type. The constructor records those defaults without starting the runtime. Opening a Session captures its configuration and starts native resources. `Agent.name` supplies a diagnostic label; it grants no permissions and adds no instructions.

Agent fields are immutable. Construct a new Agent to change defaults for future Sessions, or use `agent.with_output_type(...)` to change the output type. Unknown constructor options fail before native resources open.

| Task | Guide or section |
| --- | --- |
| Select a model, credentials, or custom endpoint | [Models and authentication](models.md) |
| Return a validated Python value or check completion | [Structured output](outputs.md) |
| Choose tools and workspace permissions | [Tools and workspace access](#choose-tools-and-workspace-access) |
| Ask a separate typed decision question | [Typed decisions](#make-typed-decisions) |
| Filter input sent to the model | [Model-context hooks](#choose-a-model-context-hook) |
| Edit an advanced HTTP sampling request | [Request editing](#edit-the-next-sampling-request) |
| Let generated JavaScript coordinate tools | [Programmatic tool calling](#programmatic-tool-calling) |
| Manage ownership, persistence, and shutdown | [Application lifecycle](#plan-the-application-lifecycle) |

## Choose tools and workspace access

Declare tools on the Agent or when opening the Session. The Session records Python and manual tool names, together with each MCP server name and raw tool name. Those names limit later updates and turn selections. [Idle updates](tools.md#replace-tools-between-turns) can replace the registered callbacks or MCP connections, including endpoints and credentials. Temporary selections use the accepted bindings and cannot redefine them. The fixed Apps registration from `HostedVerificationMCP` or `ChatGPTAppsMCP` requires a new Session for replacement; see [service-specific integrations](mcp.md#service-specific-integrations).

Pass `environment=...` to `agent.session()` or a one-shot method for a [local workspace](execution.md#local-execution) or a [remote workspace](execution.md#remote-execution). One Agent can open Sessions in different environments. Environment settings and saved state cannot add tool names.

Pass application state through `context=...` and read it in a callback with `ToolContext[Application]`. This state stays separate from model input and tool arguments. See [application context](tools.md#application-context).

## Make typed decisions

Matching development builds support `Agent.decide()` for booleans, choices and structured values. The `0.24.0-alpha.7` preview does not include this API. The [typed decision guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/decisions-guide.md) provides matching-source setup, complete examples and supported output types.

Applications select a decision provider through `Agent(decisions=provider)`. This provider answers explicit decision questions; it is separate from the [generative model connection](models.md#provider-connection) used for Agent runs. Setting `Agent.provider` does not configure `Agent.decide()`. Each `await agent.decide(question, input=...)` call requires explicit input and returns a boolean by default. Calls with `choices=[...]` return one of those values; calls with `output_type=...` return a validated value of that type. The SDK rejects calls that combine `choices` and `output_type`.

Decision calls use only their explicit input, without starting an Agent run or adding conversation history. A per-call `provider=` overrides the Agent's `decisions` default. Calls without either provider raise `ValueError`. Applications own decision-provider cleanup; Agent and Session borrow that provider. The [provider guide](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/decisions-guide.md#replace-the-provider) describes `DecisionProvider` and the optional `OpenAIDecisions` adapter.

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
| Select or narrow admitted tools before a step | [`before_step`](sessions.md#select-tools-before-each-model-step). Tool authority remains bounded by the Session's opening selection. |
| Edit advanced HTTP request presentation in a restricted profile | [`prepare_request`](#edit-the-next-sampling-request). |
| Transform execution arguments or results | [Tool hooks](tools.md#inspect-or-change-tool-values). |

These hooks have separate owners. A context projection does not change admitted tools or execution policy.

## Edit the next sampling request

`prepare_request` requires `0.24.0-alpha.7` or later. Install a [matching preview wheel](installation.md) before using it.

Pass `prepare_request=callback` to `agent.run()`, `agent.run_sync()`, or `session.submit()` for advanced edits to a detached copy of the next HTTP model request. Use it to change sampling instructions or selected tool descriptions and parameter schemas within the profile below. Prefer [`transform_context`](#choose-a-model-context-hook) for model input, [`before_step`](sessions.md#select-tools-before-each-model-step) for tool selection, and [tool policies](tools.md#inspect-or-change-tool-values) for execution arguments or results.

The synchronous or asynchronous callback receives `callback(turn, request, *, preparation_index, recovery_attempt)`. `request` is a `PreparedRequest` imported from `agents`, with `instructions`, `input`, and `tools`. Return the complete edited value, or `None` to use the original request. Mutating the copy and returning `None` discards the edits. The SDK captures returned JSON when the callback settles.

`input` contains native ResponseItem JSON, including JSON strings for function arguments. Preserve call identities when processing tool results. Optional fields that native serialization omits must remain omitted; explicit nulls or unsupported schema keywords can fail validation before dispatch. Each selected tool keeps its namespace, name, kind, strictness, and loading mode. Only its `description` and `parameters` can change.

These edits affect sampling. Canonical conversation history, admitted tool routing, Python validators, and approval policies remain authoritative. Expanding a sampled enum does not authorize the new value. Ordinary MCP tools still rely on the server or application policy for argument validation.

### Choose a supported request-editing profile

Use an explicitly HTTP provider, such as `NativeProvider(supports_websockets=False)`, and a standard Responses model such as `gpt-5.5`. Lite models and WebSockets are unsupported. Select only eager ordinary Python, manual, or MCP tools with `defer_loading=False`. Request editing does not support native children, hosted tools, an Environment, or programmatic tool calling.

The callback must tolerate repeated preparation and avoid business effects. HTTP retries reuse the captured edit. Native stream recovery invokes the callback again; `preparation_index` can advance without a tool result, and `recovery_attempt` identifies recovery. Inspect input and call identities instead of using the index as evidence that a tool completed. Cancellation joins callback settlement before later work starts, so a blocking callback can delay cancellation.

Native output and `PromptedOutput(max_retries=0)` are supported. Separate prompted-output corrections cannot carry this callback. One-shot manual tools require `on_interaction`; without it the run cannot create an automatic pending checkpoint.

An active edited operation cannot suspend or export portable state. Save after it settles. Automatic compaction needed before or during sampling fails before the hidden compaction request; earlier tool effects remain. Shorten the input or start a new operation without the editor after checking those effects. Compaction due after completion does not discard the completed answer.

The complete synthetic [prepared-request recipe](https://github.com/openai/openai-agents-python/blob/feat/codex-harness/examples/codex/recipes/prepared_request.py) edits a request after a real Python tool result and checks the outgoing instructions, input, and schema. From the matching source root, run `python -m examples.codex.recipes.prepared_request`. It needs no model key and uses a local HTTP Responses peer.

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

## Telemetry

Configure exporters, startup, shutdown, and content controls in the [observability guide](observability.md#configure-telemetry).

### Continue a trace across workers

Use [trace context and worker propagation](observability.md#continue-a-trace-across-workers) to connect Session and tool work across workers.

## Plan the application lifecycle

The SDK runs inside your application, which manages its workers, storage and credentials. Saving a checkpoint does not provide a hosted recovery service.

| Application decision | Supported path |
| --- | --- |
| When to initialize diagnostics | Configure [telemetry](observability.md#configure-telemetry) before the first Session opens. |
| Which work belongs to a conversation | Own each [Session](sessions.md) explicitly and choose queueing or steering when submissions overlap. |
| Where history survives a restart | Choose [a local profile, application database, or portable checkpoint](state.md#choose-how-to-store-a-conversation). Retain the required state or conversation ID in application storage. |
| What tools may do | Supply current [tools and execution permissions](tools.md). Restored history does not supply current credentials or grant new authority. |
| Whether work can be retried | Inspect [effect metadata and history](sessions.md#failures-and-recovery), together with your application's records of external effects. |
| How to shut down | Close owned Sessions, then close any [explicit providers](models.md#provider-lifetime) after their Sessions are closed. Shut down [telemetry](observability.md#configure-telemetry) after all Sessions close. |

<a id="model-and-session-settings"></a>
<a id="additional-controls-in-development-builds"></a>
<a id="provider-connection"></a>
<a id="use-an-application-owned-model-client"></a>
<a id="buffer-model-responses"></a>
<a id="reuse-a-codex-login-experimental"></a>
<a id="set-up-a-login-on-a-headless-host"></a>
<a id="run-a-coding-turn-without-an-api-key"></a>
<a id="account-identity-refresh-and-usage"></a>
<a id="read-codex-plan-usage"></a>
<a id="read-api-key-rate-limits"></a>
<a id="provider-lifetime"></a>
<a id="recover-a-failed-managed-environment-opening"></a>

## Models and authentication

Choose credentials and a connection in [provider setup](models.md#provider-connection), select [model settings](models.md#model-and-session-settings), and manage [provider lifetime](models.md#provider-lifetime). The model guide also covers [borrowed clients](models.md#use-an-application-owned-model-client), [experimental Codex login](models.md#reuse-a-codex-login-experimental), and [rate limits](models.md#read-api-key-rate-limits).

<a id="structured-output"></a>
<a id="choose-an-output-type"></a>
<a id="native-schema-limits"></a>
<a id="handle-validation-failures"></a>
<a id="inspect-corrections-and-saved-state"></a>
<a id="check-a-proposed-completion"></a>

## Structured output and completion checks

[Return a typed result](outputs.md#return-a-typed-result), compare [output modes and schema limits](outputs.md#choose-an-output-type), and [handle validation failures](outputs.md#handle-validation-failures). Use [completion checks](outputs.md#check-a-proposed-completion) to accept an answer or request more work before the turn finishes.
