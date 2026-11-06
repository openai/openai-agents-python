# Run v0 agents with the native SDK

Use the experimental `agents.migration.SDKRunner` to run a supported Classic Agent through the native Codex loop. Keep its existing Model, function tools, and application context. The selected Classic Model still makes model requests; Codex decides when to request another response and execute the declared tools.

This guide applies to the source on `feat/codex-harness`. `SDKRunner` is not included in the previously published `v0.24.0-alpha.6` preview wheels or the v0 `openai-agents` package on PyPI. Follow the [contributor setup](CONTRIBUTING.md) to build this source, then install its Classic dependencies from the repository root:

```sh
python -m pip install '.[classic]'
```

The same distribution contains both APIs and requires the matching native runtime. See [Classic installation](CLASSIC.md#install-from-the-matching-source) for dependency extras and platform constraints.

## Allow the private loopback connection

The adapter passes model responses to the native loop through a private local connection. On macOS and Windows, add `127.0.0.1` to `NO_PROXY` before starting your application, because the native transport can load system proxy settings. On other platforms, this is required when any `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY` variable, including a lowercase variant, is set to a nonempty value.

When a bypass is required, define `NO_PROXY`, `no_proxy`, or both. Every defined variable must contain the exact comma-separated entry `127.0.0.1`. Preserve your existing entries. The adapter requires this explicit entry; it does not interpret wildcard or subnet exclusions. Keep proxy and bypass variables unchanged during a run. An incompatible configuration fails before provider resolution or model calls. SDKRunner does not change your proxy environment.

## Choose a migration step

| Your goal | API | Execution |
| --- | --- | --- |
| Upgrade the package while preserving existing v0 behavior | `agents.classic.Agent` and `agents.classic.Runner` | Original Python loop |
| Move a supported v0 workflow to native execution | `agents.classic.Agent` and `agents.migration.SDKRunner` | Native loop with your Classic Model and tools |
| Use the native API directly | `agents.Agent` and its `run` or `session` methods | Native loop with native declarations |

`agents.classic` retains the v0.24.0 compatibility baseline and later fixes from its pinned main commit. Start with the [Classic import migration](CLASSIC.md#change-imports-before-changing-behavior) and test your application there. Libraries that import the old root `agents` API internally also need to select Classic.

## Replace the runner for one supported workflow

Keep the Agent and tools together in the Classic namespace. For a workflow with an explicit Model object, change the runner call:

```python
# Before: existing v0 or Classic execution.
from agents.classic import Runner

result = await Runner.run(agent, "Greet Ada.", context=app_context)
```

```python
# After: native execution of the same supported Agent.
from agents.migration import SDKRunner

result = await SDKRunner.run(agent, "Greet Ada.", context=app_context)
print(result.final_output)
```

These fragments assume you already created `agent` and `app_context`. Do not pass a Classic Agent directly to the native `agents.Agent` API or mix native tools into the Classic Agent.

Set `agent.model` explicitly. If it is a model name rather than a Model object, pass its Classic ModelProvider through `model_provider=provider`. You can reuse the provider from your Classic `RunConfig`. For example, with your existing OpenAI credentials:

```python
from agents.classic import OpenAIProvider

provider = OpenAIProvider()
try:
    result = await SDKRunner.run(agent, "Greet Ada.", model_provider=provider)
finally:
    await provider.aclose()
```

The adapter does not choose a provider or change your model credentials. It borrows the Model and ModelProvider. Your application remains responsible for their resources, including any client you supply.

Run the [complete scripted example](packages/codex/examples/migration.py) from the matching source tree:

```sh
python packages/codex/examples/migration.py
```

It needs no model credentials and makes no external model requests. Its scripted Model requests the existing Classic `greet` tool, the native loop executes that tool with application context, and the next response supplies the final answer. The example prints `Hello, Ada!` and the native session ID. It verifies the execution path, not model quality.

## Check the supported features

| Feature | `SDKRunner` support |
| --- | --- |
| One base Classic Agent | Supported; Agent subclasses are unsupported |
| Static instructions and string input | Supported |
| Explicit Classic Model, or model name plus explicit ModelProvider | Supported within the response limits below |
| Eager Classic function tools with text results | Supported; the original tool validates arguments and invokes its callback |
| Application context | Supported; pass `context=...` |
| Text final output | Supported |
| Model request limit | `max_turns=10` by default; a positive integer counts Model calls |
| Ordinary model tuning | Passed to the selected Model within the supported settings contract |
| Runner-managed retries, timeouts, or forced tool choices | Unsupported; `tool_choice` must be `None` or `"auto"` |
| Structured final output and Agent/tool guardrails | Unsupported |
| Dynamic instructions, prompts, lifecycle hooks, or tool enablement | Unsupported |
| Tool approvals, timeouts, deferred loading, or namespaces | Unsupported |
| Handoffs and agents used as tools | Unsupported |
| Custom tool-use behavior | Unsupported; keep `tool_use_behavior="run_llm_again"` and `reset_tool_choice=True` |
| MCP, hosted tools, sandbox tools, voice, or realtime | Unsupported |
| Streaming or synchronous runner methods | Unsupported; use the asynchronous `run` method |
| Classic Session stores, conversation history input, RunState, or continuation IDs | Unsupported |

Unsupported Agent, tool, and model settings fail before provider resolution or model/tool execution. Fix the reported configuration or keep that workflow on `agents.classic.Runner`. Do not remove a guardrail or approval policy merely to make a workflow eligible.

Tool callbacks receive the application context, cumulative usage, original tool call and raw argument string, and active Agent. `ToolContext.turn_input` contains the input supplied to `Model.get_response` for the response that requested the tool. The context does not carry a Classic `RunConfig` or approval state.

Ordinary `ModelSettings`, such as temperature, maximum output tokens, reasoning, verbosity, and provider response storage, pass to your selected Model. The adapter rejects `retry`, `timeout`, `context_management`, `extra_query`, `extra_body`, `extra_headers`, and `extra_args` when they are not `None`. These settings can require runner behavior or replace request fields that the adapter must control.

The adapter validates each Model response before delivering it to native execution. A custom Model must return completed text messages, function calls, or both; reasoning records may accompany them. Unsupported output fails the run; earlier Model calls and tool effects may already have occurred.

Response items with `provider_data` are unsupported. Providers can use this field for replay data, such as Gemini thought signatures or provider-native thinking blocks, that the native loop cannot preserve. The adapter rejects the whole response before any of its tools run. Keep workflows that require this metadata on `agents.classic.Runner`.

Keep `OpenAIChatCompletionsModel` workflows on Classic, including plain text runs: that adapter adds `provider_data` to its responses for provider provenance. Standard Responses reasoning fields, including `encrypted_content`, remain supported.

The private transport allows at most 4 MiB for each native request body and each encoded response event sequence. This is a transport limit, not a model token allowance. Keep larger workflows on Classic or port them to the native API.

## Read the result and handle failures

The returned `SDKRunResult` exposes `final_output`, the original `last_agent`, and the settled `native_result`. Read native identifiers from `result.native_result.turn.session_id` and `result.native_result.turn.turn_id`. The owned Session is closed before the runner returns.

This result does not implement Classic RunResult, streaming events, `to_input_list()`, or serialized RunState. It does not provide a continuation handle. Each `SDKRunner.run` starts a fresh native conversation. Keep existing saved conversations on their original runtime; no data conversion occurs.

`max_turns` limits model requests, not individual tool calls or wall-clock time. Native request and stream retries are disabled for the adapter. The selected Model's underlying client can still apply its own transport retry policy. The adapter does not add another retry loop.

Classic Runner traces are not preserved. The adapter calls the selected Model with `ModelTracing.DISABLED`.

To cancel, cancel the task awaiting `SDKRunner.run` and await that task's completion. The runner closes its response endpoint and native Session. During cancellation or failure, it waits for every started tool invocation to finish, including asynchronous tools. Runner cancellation does not cancel those tool invocations. A tool that never finishes can prevent cleanup from completing. A Model that suppresses cancellation can also delay cleanup.

Your application remains responsible for background work that its callbacks start independently. Cancellation and failure do not undo tool effects. Reconcile those effects before restarting a failed workflow.

## Move a workflow fully to the native API

When you need native conversations, streaming, approvals, or other native capabilities, port that workflow's declarations to `agents.Agent` and native tools. Follow the [native quickstart](docs/sdk/index.md), [tool guide](docs/sdk/tools.md), and [session guide](docs/sdk/sessions.md) from your installed SDK version.

Test model inputs, tool effects, output handling, and failure behavior at each migration step. You can keep some workflows on Classic while others use `SDKRunner` or native declarations. Choose the runner explicitly for each workflow; the adapter never falls back to Classic after starting native execution.
