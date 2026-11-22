# Codex provider

Pass `RunConfig(model_provider=CodexProvider())` to the Runner to use Codex Core inside your Python process. The SDK loads the native extension on the first Codex model call. The Python Runner manages the agent loop, function tools, handoffs, approvals, guardrails, and output validation. Codex supplies the model connection and native workspace tools.

| Goal | Integration |
| --- | --- |
| Switch an agent's model calls between the default provider and Codex | `RunConfig(model_provider=CodexProvider())`, described here |
| Keep the agent's model and delegate one workspace task to Codex | [`codex_tool`](../tools.md#experimental-codex-tool) |

## Install a GitHub prerelease wheel

The experimental `feat/simple-codex` branch packages the Python SDK, the compiled Codex extension, and its execution helpers in one `openai-agents` wheel. Use standard CPython 3.10 through 3.14. After publication, find **Agents SDK v1.0.0-alpha.1** in [GitHub Releases](https://github.com/openai/openai-agents-python/releases). Expand **Assets** and copy the link to the wheel for your platform. Its filename starts with `openai_agents-1.0.0a1-`.

| Platform | Wheel filename ending |
| --- | --- |
| Linux x86-64 with glibc 2.28 or newer | `manylinux_2_28_x86_64.whl` |
| Linux ARM64 with glibc 2.28 or newer | `manylinux_2_28_aarch64.whl` |
| macOS 11 or newer on Apple Silicon | `macosx_11_0_arm64.whl` |
| Windows x64 | `win_amd64.whl` |

In an activated virtual environment, replace `WHEEL_URL` with the asset link. This installs prebuilt binaries.

```sh
python -m pip install --only-binary=:all: "WHEEL_URL"
```

For repository examples, check out the preview tag. From the repository root, run `uv sync` with the extras required by the example. Then install the wheel in the project environment.

```sh
uv pip install --reinstall-package openai-agents "WHEEL_URL"
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

Pass a [`ResumableSession`](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/docs/sessions/index.md#use-your-own-storage-for-resumable-sessions) to `Runner.run(..., session=session)` to pause a built-in `exec_command` or `apply_patch` for approval. A new worker can load that session and approve or reject the original call. Recovery requires the same workspace and permissions, one local environment, and no tool hooks. Other native approval requests fail the model call.

For a non-interactive read-only workflow, disable approval prompts.

```python
provider = CodexProvider(
    config_overrides=('approval_policy="never"', 'sandbox_mode="read-only"'),
)
```

Commands that require more permission fail under this configuration. SDK tool approvals still use the Runner's human-in-the-loop flow.

A `SandboxAgent`, `ShellTool`, or `ApplyPatchTool` uses the SDK's executor or editor instead. The provider disables competing native workspace tools for these runs. Disable inherited Codex MCP servers when using isolated SDK execution tools; otherwise the provider rejects the configuration. SDK-managed MCP tools remain available through the SDK's own connection and approval policy. See [MCP](../mcp.md).

Input guardrails with `run_in_parallel=False` finish before model execution. Cancellation and output guardrails do not roll back native commands or tool side effects. Inspect the workspace before retrying a failed operation.

## Streaming, history, and cancellation

Reuse a `CodexProvider` across calls in the same workspace. The provider shares a native thread manager and starts a fresh, ephemeral Codex thread for each model call. The SDK supplies the instructions, messages, tool results, and schemas for that call. Keep conversation history in an [SDK session](../sessions/index.md) or pass `result.to_input_list()` to the next run. When your application finishes using the provider, call `await provider.aclose()` to release its shared resources.

Use `Runner.run_streamed()` and consume `result.stream_events()` as with the default provider. Text deltas arrive during generation. When the model supplies a requested reasoning summary, the stream also includes `response.reasoning_summary_text.delta` events. SDK function calls supply their complete JSON arguments in one update, rather than an argument-by-argument stream.

Call `result.cancel()` to stop a streaming run immediately, or `result.cancel(mode="after_turn")` to stop after the current turn. Continue consuming the stream to observe completion. For a non-streaming async run, cancel its asyncio task. The provider closes the ephemeral thread when the model call ends.

Strict structured output uses Codex's output schema. With `strict_json_schema=False`, the provider puts the schema in the instructions and the SDK validates the final JSON. The latter does not constrain generation on the server.

`OpenAIResponsesCompactionSession` can compact full input history and replay the encrypted result into Codex. It calls the separate OpenAI compact endpoint and needs an OpenAI client or credentials. `compaction_mode="previous_response_id"` cannot use Codex's local turn IDs as stored Responses IDs. See [session compaction](../sessions/index.md).

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
