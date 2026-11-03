# Configure an agent

> **Version:** These guides describe the v1 source branch. Build the SDK from the same commit as these guides. See [installation and version compatibility](versions.md) for v0 and v1.

Construct an Agent with instructions, tools and an optional output type. Construction records data; opening a Session captures it and starts native resources. Agent.name is a diagnostic label and does not change instructions or permissions.

Agent declaration fields are immutable. Construct a new Agent to change defaults for future Sessions. Use `agent.with_output_type(...)` to create a variant with a different output type.

Pass an `Environment` to `agent.session(environment=...)` when this conversation needs a remote workspace. One Agent can open separate Sessions in different environments. One-shot Agent methods accept the same `environment` argument. See [remote execution](tools.md#remote-execution).

Set `model` to a native model identifier when you want an explicit selection. Omission preserves the pinned runtime's default. `ModelSettings` supports `reasoning_effort`, `reasoning_summary` and `service_tier`; omission preserves native defaults, while `service_tier=None` clears that override. These settings describe model behavior and grant no tool or workspace access. On an ordinary opening, `agent.session(model=...)` overrides the Agent model. Present `model_settings` keys override Agent settings; omitted keys keep the Agent or native defaults. Portable restore instead keeps the saved model and settings; see [restore precedence](sessions.md#compact-save-and-restore).

An open Session captures its own configuration. Use the idle [Session update methods](sessions.md#update-tools-instructions-and-model-settings) to change its instructions or native model settings. Constructing another Agent does not alter a Session that is already open. A Session opened with `NativeAgents` keeps its instructions and model settings fixed.

For a native Responses connection, pass `NativeProvider(base_url=..., api_key=...)`. The SDK uses Codex's built-in model client; `provider` does not accept an `openai.OpenAI` or `openai.AsyncOpenAI` instance. A custom endpoint must support the Responses protocol. Chat Completions compatibility alone is insufficient.

With `base_url=None`, model requests go to `https://api.openai.com/v1`. The SDK does not read `OPENAI_BASE_URL` automatically. To use that environment variable, import `os` and pass `base_url=os.environ["OPENAI_BASE_URL"]` explicitly. An omitted API key is read from `OPENAI_API_KEY` when the Session opens. Keep credentials in current application configuration rather than saved conversation state.

With `supports_websockets=None`, the SDK enables WebSockets when `base_url=None` and disables them for any explicitly supplied `base_url`. Set `supports_websockets=True` for a compatible custom provider, or `False` to require HTTP. When WebSockets are enabled, the native runtime selects the final transport for the provider and model.

Each Session captures current provider settings and owns its native connections. After all Sessions using a custom provider have closed, call `await provider.aclose()` to finish cleanup. Calling it while a Session is open or opening raises an error. A successful close prevents further use; create a new provider for later Sessions.

The pinned Codex bypasses HTTP proxies when the process sets `CODEX_SANDBOX=seatbelt`.
The SDK rejects Session startup in that mode if `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, or their lowercase forms have a nonempty value. This prevents an
unintended direct request. The check also applies when `NO_PROXY` would exempt
the destination or the provider enables WebSockets. Run outside that sandboxed
environment, or remove proxy settings you do not need.

When neither the Agent nor the Session supplies a `provider`, each Session creates its own default provider and attempts to close it automatically. Reusing an Agent does not share that provider across its Sessions. To share an explicit provider, pass the same `NativeProvider` to the Agents or Sessions that need it. Close all Sessions using it before `provider.aclose()`.

### Recover a failed managed-environment opening

If opening with a [managed Environment](tools.md#manage-the-executor-lifetime) fails and automatic cleanup cannot finish, the exception can carry the implicit provider in `error.provider`. The provider may still own an executor manager or native resources. Inspect this optional attribute and retry `aclose()` before discarding the owning event loop. This applies to `agent.session()`, `agent.run()` and `agent.run_sync()` opening failures; it is not a general attribute of every SDK error.

This async fragment uses your `agent` and the `executor_lease()` manager from the linked example:

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

Run async cleanup on the same event loop as the failed opening. If the retry fails, this handler preserves the opening error and its `provider` attribute, with the cleanup failure as its cause. The caller must retain that provider and retry cleanup before discarding the owning event loop. After a synchronous `run_sync()` failure, a caller outside an event loop can use `asyncio.run(recovery.aclose())`; the provider routes cleanup to the retained run loop. An explicit provider remains application-owned and is not attached to this opening error; close it after its other Sessions have closed.

## Structured output

Set `output_type=Answer` to request a structured object and validate the returned
value in Python. The Agent preserves the output type:

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

This code runs inside an async application. Native structured output supports objects with declared fields and non-object roots such as `int` and `list[int]`. Plain text remains the default when `output_type` is omitted or is `str`. To return a typed union or nullable root, use a Pydantic `RootModel`, such as `RootModel[int | str]` or `RootModel[int | None]`. The current static `Agent` constructor does not accept a bare union such as `int | str`. Use `type(None)` when the output must be `null`.

A bare structured type or `NativeOutput(Answer)` uses a native strict schema. `NativeOutput(str)` explicitly requests a native response object, while bare `str` returns plain text. `PromptedOutput(Answer)` asks for JSON in the prompt and validates text in Python; the provider does not enforce a native schema. Its default `max_retries=0` fails on invalid output. Nonzero retries request separate tool-free corrections, not a replay of the task. LocalStorage supports only `max_retries=0`.

Invalid final output raises `OutputValidationError` from `agents`, including when prompted corrections exhaust their retry budget. Catch it around `agent.run()` or `turn.get_result()`. The exception's `raw_output` contains the rejected text and may contain private data. Keep that text out of routine logs. The exception message omits the rejected text.

This fragment reuses the typed `agent` above and reports a fixed message on validation failure:

```python
from agents import OutputValidationError

try:
    result = await agent.run("Greet Ada about astronomy.")
except OutputValidationError:
    print("The answer did not match the required format.")
else:
    print(result.final_output.greeting)
```

Validation failure does not undo completed tools or replay the task. Repeated waits on the same Turn raise its settled failure again. An explicit Session can accept a new turn after this failure. Inspect its history and application effect records before submitting input that could repeat a tool effect.

A successful result exposes separate correction receipts in `Completed.repair_attempts`. Each receipt is a `RecordedTurn` with its own native session and turn IDs, plus `parent_session_id` and `parent_turn_id` identifying the original task. Its `usage` covers that correction turn. `Completed.usage` remains a cumulative snapshot of the original thread and excludes these correction threads. Do not add cumulative snapshots from successive original turns as if each were a separate cost. Missing usage is `None`, not zero.

```python
for repair in result.repair_attempts:
    print(repair["id"], repair["parent_turn_id"], repair["usage"])
```

The correction turn receives the invalid output, schema and effective model connection. It does not receive the original tools, application context or instructions. `final_output` may come from a correction, while the original native task history stays intact. `session.turns()` and `session.items()` include the retained correction receipts beside their original parent.

Live manual replies and idle checkpoint restore work with structured output. Settled manual turns can also be suspended with matching `PromptedOutput`. Pending `NativeOutput` or bare structured output cannot. See [saving a settled manual request](sessions.md#saving-a-settled-manual-request) for fingerprint, active repair and `before_step` limits.

For a native non-object root, the SDK asks the model for an object with one required `response` field. It validates the response and returns the field as `result.final_output`. For example, the model returns `{"response": [1, 2]}` for `output_type=list[int]`, and the application receives `[1, 2]`. A `RootModel` result keeps its declared class. Native history and output observations retain the actual JSON object. An object output that declares its own `response` field keeps that field and is not unwrapped.

For native output, all nested objects must be closed. Open maps such as `dict[str, str]`, including maps inside a Pydantic model, and models that allow extra fields are rejected. Unconstrained values such as `Any`, sets and fixed-length tuples are also outside this contract. Schemas using `oneOf`, `allOf`, `prefixItems` or `uniqueItems` are rejected when the Session opens. These restrictions also apply inside non-object roots and `RootModel` types.

The native strict schema requires every declared field, even when the Python model gives a field a default. It removes defaults from the model request; a default does not let the model omit that field. Use a nullable field when the model must explicitly return `null`. The `response` field is also required when its value can be `null`. These native restrictions do not apply automatically to prompted output or Python function parameters.

Use `ToolContext[Application]` for callback application state; it is separate
from model input and tool arguments.

The `Agent` constructor accepts the listed options directly. Unknown options and Python model objects fail before opening native resources.

Opening Python/manual names and MCP server/raw-tool pairs limit later updates and turn selections. Idle updates can replace those bindings, including MCP endpoints and credentials; temporary selections cannot redefine them. Environment settings and saved state cannot add names. See [idle replacement](tools.md#idle-replacement).

## Telemetry

Import telemetry settings from `agents.telemetry`. Call `await configure(...)` once per process before opening the first Session. Close all Sessions before `await shutdown()` to flush and retire native telemetry. Shutdown does not close Sessions or application-owned Python OpenTelemetry exporters. If native initialization fails after claiming the tracing subscriber, start a fresh process; retrying configuration in that process is unsupported.

This fragment belongs inside an async application, before any Session opens. Replace the exporter endpoint with your collector's trace endpoint and configure `agent` as described above:

```python
from agents import telemetry

await telemetry.configure(
    traces=telemetry.OtlpHttp(endpoint="http://127.0.0.1:4318/v1/traces"),
    environment="dev",
)
try:
    result = await agent.run("Say hello.")
finally:
    await telemetry.shutdown()
```

`traces`, `logs` and `metrics` each accept `OtlpHttp(...)`, `OtlpGrpc(...)` or `None`. All three default to `None`, which disables that signal, including native metrics. `OtlpHttp` uses `protocol="binary"` by default and also accepts `"json"`. Supply the collector endpoint and an optional string-to-string `headers` mapping. Both exporters accept `tls=OtelTlsConfig(...)` with `ca_certificate`, `client_certificate` and `client_private_key` paths. Relative certificate paths resolve against the application's current directory. `span_attributes` adds application-selected string attributes.

`log_user_prompt=False`, `log_agent_responses=False` and `tool_result_max_bytes=0` are the defaults. The text switches control only the named native records; they do not sanitize all telemetry. The byte limit bounds result text only. Select exporters and data controls for the information your application permits the collector to receive.

## Programmatic tool calling

Programmatic tool calling is optional and off by default. Ordinary tool calls do not require JavaScript. Set `programmatic_tool_calling=True` on `Agent` to let the model generate JavaScript code cells that call declared Python, manual and MCP tools. These cells can combine tool calls with loops, conditions and result processing. You continue to write your application and Python function tools in Python; the model writes the JavaScript.

The cells run in the SDK's bundled Codex Code Mode host, which embeds the V8 JavaScript engine. JavaScript is the execution language for these cells; the SDK does not provide a Python-language option for them.

Python functions own their argument and result logic. MCP result policies run before the cells receive MCP results. The bundled host grants no shell tools or local workspace. Pass `Environment` to the Session or one-shot run for remote execution. A manual call waits for an application reply in the live Session; a pending code cell cannot be saved as a portable checkpoint. The function names `exec` and `wait` are reserved.

A restored Session uses the current Agent's declaration, so set `programmatic_tool_calling=True` again when restoring a conversation that needs it. [Executor result transforms](tools.md#executor-policies) require this option. An executor argument transform also supports direct calls.
