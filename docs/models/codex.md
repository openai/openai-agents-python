# Codex provider

Pass `RunConfig(model_provider=CodexProvider())` to the Runner to use Codex Core inside your Python process. The SDK loads the native extension on the first Codex model call. The Python Runner manages the agent loop, function tools, handoffs, approvals, guardrails, and output validation. Codex supplies the model connection and native workspace tools.

| Goal | Integration |
| --- | --- |
| Switch an agent's model calls between the default provider and Codex | `RunConfig(model_provider=CodexProvider())`, described here |
| Keep the agent's model and delegate one workspace task to Codex | [`codex_tool`](../tools.md#experimental-codex-tool) |

## Install a GitHub prerelease wheel

Use standard CPython 3.10 through 3.14. The preview wheel includes the Python SDK, Codex Core, and its execution helpers. You don't need a Codex CLI installation.

!!! warning "Check the prerelease before installing"

    [`v1.0.0-alpha.1`](https://github.com/openai/openai-agents-python/releases/tag/v1.0.0-alpha.1) includes the provider but predates OpenTelemetry, operation tracing, native approval recovery, and Runner text redaction. For those features, wait for a tagged prerelease that lists them. Install wheels only from the SDK's GitHub Releases page. A passing CI run and its checksums do not establish release approval.

1. Open [SDK releases](https://github.com/openai/openai-agents-python/releases) and select a tagged prerelease that supports the features you need.
2. From that release's **Assets**, download `SHA256SUMS` and the wheel for your platform.
3. Compute the wheel's SHA-256 digest with `sha256sum` on Linux, `shasum -a 256` on macOS, or `Get-FileHash` in PowerShell. Install only if the digest matches the entry for the exact filename in `SHA256SUMS`.

| Platform | Wheel filename ending |
| --- | --- |
| Linux x86-64 with glibc 2.28 or newer | `manylinux_2_28_x86_64.whl` |
| Linux ARM64 with glibc 2.28 or newer | `manylinux_2_28_aarch64.whl` |
| macOS 11 or newer on Apple Silicon | `macosx_11_0_arm64.whl` |
| Windows x64 | `win_amd64.whl` |

For the tracing example, see **OpenTelemetry** in the English [tracing guide](../tracing.md) to install the wheel with the `opentelemetry` extra. Otherwise, activate a fresh virtual environment and replace `WHEEL_FILE` with the local path to your `.whl` file.

```sh
python -m pip install --only-binary=:all: "WHEEL_FILE"
```

For repository examples, check out the same release tag. From the repository root, run `uv sync` with the extras required by the example. Then install the verified wheel in the project environment.

```sh
uv pip install --reinstall-package openai-agents "WHEEL_FILE"
```

Run the examples with `uv run --no-sync` to keep the wheel installed. You can also set `UV_NO_SYNC=1` in your shell and use the repository's `uv run` commands as written. Each project sync reinstalls the pure Python SDK from the checkout.

## Authenticate

Codex reads the configuration and saved login under `CODEX_HOME`, which defaults to `~/.codex`. Python needs write access to that directory. To use your existing ChatGPT login, create `CodexProvider()` under the same account and Codex home that you used for `codex login`. For API-key authentication, supply `CodexProvider(api_key=...)` with a key from your application's secret store. The key belongs to that provider. Managed login policies apply to both methods.

Create a new provider after you switch Codex accounts or change the model provider in your Codex configuration. If your organization requires US data residency, Codex applies that requirement across the Python process. It applies to all Codex providers until the process exits.

The default provider needs the application's OpenAI credentials, typically `OPENAI_API_KEY`. Codex authentication does not configure credentials for separate OpenAI clients such as the tracing exporter or the compaction client.

## Run one agent with either provider

Save the example as `codex_switch.py`. For the Codex run, it uses `CODEX_API_KEY` when set or your saved login otherwise.

```python
import argparse
import asyncio
import os

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.decorators import tool
from agents.models.codex import CodexProvider


@tool
def square(value: int) -> int:
    """Return the square of an integer."""
    print(f"square called with {value}")
    return value * value


parser = argparse.ArgumentParser()
parser.add_argument("--codex", action="store_true")
parser.add_argument("--model", help="Optional model ID for the selected provider")
args = parser.parse_args()

agent = Agent(
    name="Calculator",
    instructions="Use square to calculate the answer. Reply with the number only.",
    tools=[square],
    model_settings=ModelSettings(tool_choice="required"),
)


async def main():
    config = RunConfig(model=args.model, tracing_disabled=True)
    if args.codex:
        config.model_provider = CodexProvider(api_key=os.environ.get("CODEX_API_KEY"))
    try:
        result = await Runner.run(agent, "What is the square of 12?", run_config=config)
        print(result.final_output)
    finally:
        await config.model_provider.aclose()


asyncio.run(main())
```

Run the script in the environment where you installed the wheel.

```sh
python codex_switch.py
python codex_switch.py --codex
```

Each run should print `square called with 12` followed by an answer of `144`. The script disables hosted SDK tracing so Codex's saved login is sufficient for the Codex run. See [tracing](../tracing.md) to configure tracing credentials separately.

Omitting `--model` uses the selected provider's default model. A model name must be available through that provider. Changing only `model_provider` does not replace an agent's explicit model object, such as an `OpenAIChatCompletionsModel`. That object handles inference unless `RunConfig.model` overrides it.

For a longer example, run the [wildlife workflow](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/examples/model_providers/codex_provider.py). It sends a local photograph, hands off to a specialist, calls an SDK function, and streams a structured report. It checks the handoff, tool call, output type, and expected catalog ID.

```sh
uv run --no-sync python -m examples.model_providers.codex_provider
uv run --no-sync python -m examples.model_providers.codex_provider --codex
```

## Configure execution

`CodexProvider` accepts the following options. See the [API reference](../ref/models/codex.md) for types.

| Option | Purpose |
| --- | --- |
| `cwd` | Codex's workspace. Defaults to the application's current directory. |
| `config_overrides` | Sequence of Codex configuration overrides, such as `sandbox_mode="read-only"`. |
| `api_key` | API key for this provider, when the host permits API-key login. |

Ordinary agents can use native Codex commands. Codex can start a separate helper process to execute a command in its configured sandbox. The host must allow that sandbox to start. An outer container or filesystem policy can limit workspace writes.

Native commands inherit Codex's `core` set of environment variables by default. This includes `PATH` and the variables for your platform's temporary directories. An explicit `shell_environment_policy.inherit` in your Codex configuration takes precedence. To supply an extra variable for commands from one provider, use `config_overrides`.

```python
provider = CodexProvider(
    config_overrides=('shell_environment_policy.set.BUILD_PROFILE="preview"',),
)
```

Set `shell_environment_policy.inherit="none"` to start with an empty set of inherited variables. Then supply any variables the command and its sandbox need, such as `PATH`. This policy controls the environment Codex passes to commands. Core runs inside Python, and configured hooks can access the application's environment. To limit the variables available to the entire runtime, set the environment before starting Python.

The provider disables shell snapshots by default so Codex can start each model call without launching a login shell. To capture your shell environment for native commands, pass `config_overrides=("features.shell_snapshot=true",)`.

The workspace sandbox protects the directory that contains the installed helpers. For example, if you install the wheel in `.venv` at the workspace root, native commands can edit project files but cannot write to or rename `.venv`. Use a separate virtual environment for dependencies that native commands need to install.

On Windows, the preview rejects a local Codex executor configured with the legacy elevated sandbox. That sandbox needs a setup helper that Codex cannot securely locate from a Python process. If your organization requires this sandbox, use an SDK-managed executor or configure a remote executor with `include_local = false` in `environments.toml`.

To pause a direct built-in `exec_command` or `apply_patch` for human approval, start the run with a [`ResumableSession`](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/docs/sessions/index.md#use-your-own-storage-for-resumable-sessions). On the replacement worker, load the saved state, approve or reject the original request, and pass the Codex run configuration again. Protect the session store. It can contain the submitted command or patch even with tracing disabled.

Recovery requires a single pending native call, one local environment, and the same workspace path and permissions. For edits, the affected files must still match the saved state. Other types of native approval requests fail the model call.

Keep required security hooks enabled. Configure the same SDK `prepare_tool_input`, `prepare_tool_output`, and lifecycle hooks on both workers. A missing required hook leaves approval pending. Other native hooks or hook warnings stop recovery. See the [recovery requirements for tool hooks](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/native/codex/patches/02-approval-recovery.md#use-a-scrubber-with-approvals).

Codex selects the sandbox for the first attempt. After a sandbox denial, Codex can retry an edit outside the sandbox only if the policy permits it without another approval and no edits have been committed. A partial edit stops recovery and leaves earlier writes on disk. After a partial edit or worker crash, inspect the workspace and reconcile any effects before retrying. See the [recovery limits](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/native/codex/patches/02-approval-recovery.md#safety-limits).

In a container, check that the worker can start the sandbox helper. On a hard timeout, terminate the worker's commands and their child processes as well.

For a non-interactive read-only workflow, disable approval prompts.

```python
provider = CodexProvider(
    config_overrides=('approval_policy="never"', 'sandbox_mode="read-only"'),
)
```

Commands that require more permission fail under this configuration. SDK tool approvals still use the Runner's human-in-the-loop flow.

A `SandboxAgent`, `ShellTool`, or `ApplyPatchTool` uses the SDK's executor or editor instead. The provider disables competing native workspace tools for these runs. Disable inherited Codex MCP servers when using isolated SDK execution tools; otherwise the provider rejects the configuration. SDK-managed MCP tools remain available through the SDK's own connection and approval policy. See [MCP](../mcp.md).

Codex blocks a native tool call if a synchronous `PreToolUse` hook fails or times out. After an approval pause, the replacement worker runs any configured `on_tool_start` callbacks before executing the call. The worker reuses the approved input, including any changes from the original `prepare_tool_input` callback.

Use [Runner text redaction](../tracing.md#runner-text-redaction) to protect completed SDK items and stored session data. Raw provider stream events and provider-managed history can contain the original arguments.

Input guardrails with `run_in_parallel=False` finish before model execution. Cancellation and output guardrails do not roll back native commands or tool side effects. Inspect the workspace before retrying a failed operation.

## Streaming, history, and cancellation

Reuse a `CodexProvider` across calls in the same workspace. The provider shares a native thread manager and starts a fresh, ephemeral Codex thread for each request from the Runner. One request can include several model calls and native tool executions. The SDK supplies the instructions, messages, tool results, and schemas for the request. Keep conversation history in an [SDK session](../sessions/index.md) or pass `result.to_input_list()` to the next run. When your application finishes using the provider, call `await provider.aclose()` to release its shared resources.

Use `Runner.run_streamed()` and consume `result.stream_events()` as with the default provider. Text deltas arrive during generation. When the model supplies a requested reasoning summary, the stream also includes `response.reasoning_summary_text.delta` events. SDK function calls supply their complete JSON arguments in one update, rather than an argument-by-argument stream.

Call `result.cancel()` to stop a streaming run immediately, or `result.cancel(mode="after_turn")` to stop after the current turn. Continue consuming the stream to observe completion. For a non-streaming async run, cancel its asyncio task. The provider has been closing the ephemeral thread at the end of each Runner request.

Strict structured output uses Codex's output schema. With `strict_json_schema=False`, the provider puts the schema in the instructions and the SDK validates the final JSON. The latter does not constrain generation on the server.

`OpenAIResponsesCompactionSession` can compact full input history and replay the encrypted result into Codex. It calls the separate OpenAI compact endpoint and needs an OpenAI client or credentials. `compaction_mode="previous_response_id"` cannot use Codex's local turn IDs as stored Responses IDs. See [session compaction](../sessions/index.md).

## Operation tracing

The SDK preview bundles [Codex patches](https://github.com/openai/openai-agents-python/tree/feat/simple-codex/native/codex/patches) for operation tracing, approval recovery, and blocking tools on synchronous pre-execution hook failure. Installing the wheel leaves any separate Codex CLI unchanged. Runner text redaction runs in Python.

Use the same SDK trace processors for Responses and embedded Codex. See **OpenTelemetry** in the English [tracing guide](../tracing.md) for a runnable example with a console exporter or collector. To capture metadata for a Codex run, pass this configuration to the Runner.

```python
from agents import RunConfig
from agents.models.codex import CodexProvider

config = RunConfig(
    model_provider=CodexProvider(),
    tracing={"operation_detail": "metadata"},
    trace_include_sensitive_data=False,
)
```

One Runner turn can contain several model calls and native tool executions. For example, if Codex reads a file, calls a Python tool, and then answers, look for three model spans and two execution spans across two Runner turns. A retry adds an event to the same model span. Python tools appear once, when the Runner executes them.

Nested native tool executions in the active Codex thread get their own spans. Tracing does not follow independently running Codex subagents onto other threads.

With `trace_include_sensitive_data=True`, SDK processors receive the following fields for each Runner request to Codex.

| `operation_detail` | Model spans | Native tool spans |
| --- | --- | --- |
| Omitted | Caller input on the first span (`.input`) and the raw returned response on the last successful span (`.response`). No `.output` or paused approval response. | Metadata |
| `"metadata"` | Metadata | Metadata |
| `"content"` | Bounded submitted messages (`.input`) and completed visible output items (`.output`) per call, without a raw `.response` | Bounded arguments and available results |

With detail omitted, native tool results such as file contents can reach SDK processors through the returned response or replayed history. OpenTelemetry exports payloads only in `"content"` mode. Set `trace_include_sensitive_data=False` to suppress payloads in all three modes.

`RunConfig.redact_text` restricts automatic traces to metadata even in content mode. Work inside Codex and native logs are outside its scope. See **Runner text redaction** in the English [tracing guide](../tracing.md).

In content mode, model spans include completed output items and omit deltas from unfinished items. Native tool spans use the result Codex accepts after output hooks. The SDK excludes paused approval responses and their private recovery checkpoints from model spans.

If a trace looks incomplete, check the following fields.

| Signal | Meaning |
| --- | --- |
| `reason="output_unavailable"` in tool content | The tool finished, but the SDK received no accepted result. |
| `reason="unsupported_or_oversized_content"` | The SDK omitted an unsupported value or one that exceeded the capture limit. |
| `outcome="interrupted"` and `estimated_end=True` | Python estimated the end of an operation without a completion timestamp. |
| `codex.tracing.coverage` with `coverage="partial"` | Check `reason`. For example, `missing_operations` means no model observations arrived, and `dropped_operations` can indicate a full queue. In OpenTelemetry, use `openai.agents.operation.coverage` and `openai.agents.operation.reason`. |

Set `RunConfig(tracing_disabled=True)` to disable SDK tracing for the run.

## Supported features and limits

Switching providers changes the model backend. It does not make every Responses API feature available through Codex.

| Feature | Codex behavior and alternative |
| --- | --- |
| SDK function tools, handoffs, approvals, guardrails, sessions, and structured output | The SDK manages these features in either mode. |
| Required tool use | `ModelSettings(tool_choice="required")` requires an exposed SDK tool call or handoff. Native Codex commands and hosted web search do not satisfy that requirement. It does not select a particular function by name. |
| Deferred tool discovery | Requires a model whose Codex catalog advertises tool search. Set `CODEX_MODEL` to a capable model and run `uv run --no-sync -m examples.tools.tool_search --codex` to check discovery and callback results. |
| Local images | Supply an absolute local path, a `file:` URL, or inline image bytes as a data URL. The [basic examples](https://github.com/openai/openai-agents-python/tree/feat/simple-codex/examples/basic) show local input and app-managed downloads. Codex does not fetch remote image URLs supplied as model input. |
| Audio input | Requires a Codex catalog model that declares audio input. Unsupported input is rejected. |
| PDFs and uploaded files | Direct file inputs and hosted file IDs are unsupported. The basic PDF examples extract text in the app with Poppler. They do not perform OCR or interpret page layout. |
| Hosted web search | Requires a supported model and host policy. `external_web_access=False` restricts search to cached results. A host may restrict all searches to cached results even when live access is requested. The [filtered-search example](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/examples/tools/web_search_filters.md) verifies opened source URLs, without claiming the Responses API's retrieved-source fields. |
| Raw system messages | Use `Agent.instructions` or developer messages. Raw input messages with `role="system"` are rejected. |
| Stored response IDs, conversation IDs, and prompt IDs | Unsupported. Use local session history or app-owned prompt text. Codex turn IDs are not Responses IDs. |
| Hosted file search, code interpreter, image generation, computer use, container shell, and hosted connectors | Their Responses API contracts are unsupported. Use an SDK function, SDK-managed MCP server, or a local executor when it provides the task you need. These alternatives do not reproduce hosted IDs, event types, persistence, or isolation. |
| Sampling controls, output-token limits, named or disabled tool choice, parallel-tool settings, and programmatic tool callers | Unsupported settings produce an error. Keep the default provider for requests that require them. |
| Reasoning and response controls | Reasoning effort and summaries are supported. `response_include` supports only `reasoning.encrypted_content`; other inclusion flags, explicit cache controls, and Responses reasoning replay controls are unsupported. |
| Realtime, voice, Decisions, and explicit model objects | These clients select their own models or transports. Changing the Runner's provider does not reroute them. |

## Diagnose a failed switch

| Symptom | What to check |
| --- | --- |
| Provider asks for a prebuilt SDK wheel | Install the wheel for your Python platform into the environment that runs your application. In a checkout, use `uv run --no-sync` after installing the wheel. |
| Login or configuration-directory failure | Confirm that the process can write to `CODEX_HOME` and that the host permits your login method. Check the saved login or pass a provider key. |
| Native shell cannot start | Check the host's native sandbox support and the configured sandbox mode. SDK tools and native commands use different executors. |
| A paused sandbox run cannot resume | The SDK needs a writable snapshot directory in either mode. On Linux, if your home directory is read-only, set `XDG_STATE_HOME` to an absolute, writable directory before you run the app. Keep that directory available when you resume. Changing `CODEX_HOME` does not move SDK snapshots. |
| Tools never load | Select a model whose Codex catalog declares tool search. The search examples fail if no tools load. |
| `CodexProvider does not support ModelSettings...` | Remove the named setting only if your workflow does not need it. Otherwise use a provider that implements it. |
| A third-party adapter no longer runs | Register Codex alongside that adapter with [`MultiProvider`](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/examples/model_providers/README.md). Codex does not wrap LiteLLM or any-llm model clients. |
