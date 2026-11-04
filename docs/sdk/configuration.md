# Configure an agent

Construct an Agent with instructions, tools and an optional output type. Construction records settings. Opening a Session captures them and starts native resources. Agent.name is a diagnostic label, not an instruction or permission.

Agent fields are immutable. Construct a new Agent to change defaults for future Sessions, or use `agent.with_output_type(...)` to change the output type. Unknown constructor options and Python model objects fail before native resources open.

Pass `environment=...` to `agent.session()` or a one-shot method for a [local workspace](tools.md#local-execution) or a [remote workspace](tools.md#remote-execution). One Agent can open Sessions in different environments. Use `ToolContext[Application]` for callback application state, separate from model input and tool arguments.

## Model and Session settings

Set `model` to a native model identifier, or omit it for the pinned runtime's default. `ModelSettings` supports `reasoning_effort`, `reasoning_summary` and `service_tier`. Omitted settings keep native defaults. `service_tier=None` clears that override. These settings grant no tool or workspace access.

When opening a Session:

- `agent.session(model=...)` overrides the Agent model.
- Supplied `model_settings` keys override Agent settings. Omitted keys keep the Agent or native defaults.
- [Portable restore](sessions.md#compact-save-and-restore) keeps the saved model and settings instead.

An open Session keeps its captured configuration. Change instructions or native model settings with the idle [Session update methods](sessions.md#update-tools-instructions-and-model-settings). Another Agent cannot change it. Sessions opened with `NativeAgents` keep these settings fixed.

The Session records Python and manual tool names and pairs of MCP server names and raw tool names when it opens. These names limit later updates and turn selections. [Idle updates](tools.md#idle-replacement) can replace bindings, including MCP endpoints and credentials. Temporary selections cannot redefine them. Environment settings and saved state cannot add names.

## Provider connection

Model requests use an API key. Pass `NativeProvider(base_url=..., api_key=...)` to configure the native Responses connection. The SDK uses Codex's built-in model client, not an `openai.OpenAI` or `openai.AsyncOpenAI` instance. Custom endpoints must support Responses. Chat Completions compatibility alone is insufficient.

Connection defaults are:

- `base_url=None` sends model requests to `https://api.openai.com/v1`. The SDK does not read `OPENAI_BASE_URL`. To use it, import `os` and pass `base_url=os.environ["OPENAI_BASE_URL"]`.
- An omitted key reads `OPENAI_API_KEY` when the Session opens. Keep credentials in current application configuration, outside saved conversation state.
- `supports_websockets=None` enables WebSockets for `base_url=None` and disables them for any explicit `base_url`. Set `True` for a compatible custom provider or `False` to require HTTP. When enabled, the native runtime selects the final transport for the provider and model.

The pinned Codex bypasses HTTP proxies with `CODEX_SANDBOX=seatbelt`. To prevent unintended direct requests, the SDK rejects Session startup if `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, or their lowercase forms are nonempty in that mode. The check applies even with a `NO_PROXY` exemption or WebSockets enabled. Run outside that sandboxed environment, or remove unneeded proxy settings.

### Read API-key rate limits

Read `session.rate_limits()` for the latest snapshot from a response with usable rate-limit data. The snapshot starts with unknown values. Each reported window can contain a limit, a remaining count, and the number of seconds until reset as measured at the response. `requests`, `tokens`, and `project_tokens` can each be absent. The provider can also supply a retry delay or an absolute retry time. Treat every `None` as unknown.

For live updates, observe `ApiRateLimitsUpdated` from `agents.observation`. Use its `snapshot` field or read the current Session snapshot after an observation gap. Rate-limit updates can arrive independently of token usage. These snapshots describe the API-key connection. Account plan limits, credits, and billing are outside their scope.

Read a settled failure with [`wait_for_attention()`](sessions.md#wait-for-a-decision-or-a-result). A reported temporary rate limit yields `reason="rate_limited"`. A reported blocking limit, such as exhausted quota, yields `reason="quota_blocked"`. Base that distinction on the failure reason. A remaining count alone cannot tell you whether the next request will succeed.

### Provider lifetime

Each Session captures provider settings and owns its native connections. If neither the Agent nor the Session supplies a provider, the Session creates its own and attempts to close it automatically. Reusing an Agent does not share default providers.

To share a provider, pass the same `NativeProvider` to the Agents or Sessions. After all its Sessions close, call `await provider.aclose()`. Closing while a Session is open or opening raises an error. A successful close prevents reuse. Create a new provider for later Sessions.

### Recover a failed managed-environment opening

If opening a [managed Environment](tools.md#manage-the-executor-lifetime) fails and automatic cleanup cannot finish, the exception can carry the implicit provider in `error.provider`. It may still own an executor manager or native resources. Check this optional attribute and retry `aclose()` before discarding the owning event loop.

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

Set `output_type=Answer` to request a structured object and validate it in Python. This example runs inside an async application:

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

- Omit `output_type` or pass bare `str` for plain text.
- Pass a structured type or `NativeOutput(Answer)` for a native strict schema. `NativeOutput(str)` requests a native response object rather than plain text.
- Use native objects with declared fields or non-object roots such as `int` and `list[int]`. For a union or nullable root, use `RootModel[int | str]` or `RootModel[int | None]`. The static `Agent` constructor rejects bare unions such as `int | str`. Use `type(None)` for output that must be `null`.
- `PromptedOutput(Answer)` asks for JSON in the prompt and validates text in Python. The provider does not enforce a native schema. Its default `max_retries=0` fails on invalid output. Nonzero retries request separate corrections without tools, not a task replay. `LocalStorage` supports only `max_retries=0`.

For a native non-object root, the model returns an object with a required `response` field. The SDK unwraps that field. With `output_type=list[int]`, model output `{"response": [1, 2]}` becomes `result.final_output == [1, 2]`. A `RootModel` result keeps its class. Native history and observations retain the JSON object. Object outputs with their own `response` field remain unchanged.

### Native schema limits

These limits also apply inside non-object roots and `RootModel` types:

- Every nested object must reject extra fields. Open maps such as `dict[str, str]`, including maps inside Pydantic models, and models allowing extra fields are rejected.
- `Any`, sets, fixed-length tuples, and schemas using `oneOf`, `allOf`, `prefixItems` or `uniqueItems` are unsupported. The SDK rejects unsupported schemas when the Session opens.
- Every declared field is required, even with a Python default. The SDK removes defaults from the model request. Use nullable fields for explicit `null` values. A nullable `response` field is still required.

These restrictions do not automatically apply to prompted output or Python function parameters.

### Handle validation failures

Invalid final output raises `OutputValidationError`, including after prompted corrections exhaust their retries. Catch it around `agent.run()` or `turn.get_result()`. Its message omits the rejected text. Its `raw_output` contains that text and may contain private data, so keep it out of routine logs.

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

`Completed.usage` is cumulative for the original thread and excludes correction threads. Missing usage is `None`, not zero. Follow the [usage accounting limits](troubleshooting.md#read-usage-without-double-counting) when comparing successive snapshots.

Corrections receive the invalid output, schema and effective model connection, without original tools, application context or instructions. `final_output` may come from a correction. Original native history stays intact. `session.turns()` and `session.items()` retain correction records beside their parent.

Live manual replies and idle restore support structured output. Settled manual turns can be suspended with matching `PromptedOutput`, but pending `NativeOutput` or bare structured output cannot. See [saving a settled manual request](sessions.md#saving-a-settled-manual-request) for fingerprint, active repair and `before_step` limits.

## Telemetry

Import settings from `agents.telemetry`. Call `await configure(...)` once per process before the first Session opens. After all Sessions close, `await shutdown()` flushes and shuts down native telemetry. It does not close Sessions or application-owned Python OpenTelemetry exporters.

If native initialization fails after claiming the tracing subscriber, start a fresh process. Retrying configuration in that process is unsupported.

Place this fragment before any Session opens. Replace the endpoint with your collector's trace endpoint and configure `agent` as above:

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

Exporter settings are:

- `traces`, `logs` and `metrics` accept `OtlpHttp(...)`, `OtlpGrpc(...)` or `None`. Each defaults to `None`, disabling that signal, including native metrics.
- `OtlpHttp` defaults to `protocol="binary"` and also accepts `"json"`. Supply a collector endpoint and optional string-to-string `headers` mapping.
- Both exporters accept `tls=OtelTlsConfig(...)` with `ca_certificate`, `client_certificate` and `client_private_key` paths. Relative paths resolve against the application's current directory.
- `span_attributes` adds application-selected string attributes.

Content defaults are `log_user_prompt=False`, `log_agent_responses=False` and `tool_result_max_bytes=0`. The switches control only the named native records. They do not sanitize all telemetry. The byte limit bounds only result text. Select exporters and controls for the data your collector may receive.

## Programmatic tool calling

Set `programmatic_tool_calling=True` on `Agent` to let the model generate JavaScript cells that call declared Python, manual and MCP tools. Cells can combine calls with loops, conditions and result processing. Ordinary tool calls do not require this option, which defaults to off.

You write the application and function tools in Python. Model-generated cells run in the SDK's bundled Codex Code Mode host, which embeds V8. The cells have no Python-language option.

These limits apply:

- Python functions handle arguments and results. MCP result policies run before cells receive results.
- The host grants no shell tools or local workspace. Pass `Environment` to the Session or one-shot run for remote execution.
- A manual call waits for an application reply in the live Session. Pending code cells cannot be saved as portable checkpoints.
- `exec` and `wait` are reserved function names.

Restore uses the current Agent declaration. Set `programmatic_tool_calling=True` again if the restored conversation needs it. [Executor argument and result transforms](tools.md#executor-policies) also support direct calls without this option.
