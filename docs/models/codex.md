# Codex provider

Use `CodexProvider` to run an Agents SDK workflow through the stock Codex CLI. The SDK still runs the agent loop, function tools, handoffs, approvals, guardrails, and output validation. Codex supplies the model connection and its native workspace tools.

This provider is experimental and is available on the `feat/simple-codex` branch. The instructions below use that checkout; installing a released `openai-agents` package does not install this branch's provider.

Choose the integration that matches your application:

| Goal | Integration |
| --- | --- |
| Switch an agent's model calls between the default provider and Codex | `RunConfig(model_provider=CodexProvider())`, described here |
| Keep the agent's model and delegate one workspace task to Codex | [`codex_tool`](../tools.md#experimental-codex-tool) |

## Set up the checkout and CLI

Check out the feature branch, then install the SDK and the stock CLI version used by this integration:

```sh
git clone --branch feat/simple-codex https://github.com/openai/openai-agents-python.git
cd openai-agents-python
uv sync
npm install --global @openai/codex@0.166.0
codex --version
codex login
```

Codex reads its configuration and saved login from `CODEX_HOME`, or its default configuration directory when that variable is unset. The process needs write access to that directory. A managed host may restrict the available authentication methods. If the host permits API-key login, `CodexProvider(api_key=...)` authenticates the child process without replacing the saved CLI login.

The default provider needs the application's OpenAI credentials, typically `OPENAI_API_KEY`. Codex authentication does not configure credentials for separate OpenAI clients such as the tracing exporter or the compaction client.

## Run one agent with either provider

Save this as `codex_switch.py` in the repository root:

```python
import argparse

from agents import Agent, ModelSettings, RunConfig, Runner, function_tool
from agents.models.codex import CodexProvider


@function_tool
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
config = RunConfig(model=args.model, tracing_disabled=True)
if args.codex:
    config.model_provider = CodexProvider()

result = Runner.run_sync(agent, "What is the square of 12?", run_config=config)
print(result.final_output)
```

Run both commands from the same directory:

```sh
uv run python codex_switch.py
uv run python codex_switch.py --codex
```

Each run should print `square called with 12` followed by an answer of `144`. The script disables hosted SDK tracing so Codex's saved login is sufficient for the Codex run. See [tracing](../tracing.md) to configure tracing credentials separately.

Omitting `--model` uses the selected provider's default model. A model name must be available through that provider. Changing only `model_provider` does not replace an agent's explicit model object, such as an `OpenAIChatCompletionsModel`. That object handles inference unless `RunConfig.model` overrides it.

For a longer example, run the [wildlife workflow](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/examples/model_providers/codex_provider.py). It sends a local photograph, hands off to a specialist, calls an SDK function, and streams a structured report. It checks the handoff, tool call, output type, and expected catalog ID.

```sh
uv run python -m examples.model_providers.codex_provider
uv run python -m examples.model_providers.codex_provider --codex
```

## Configure execution

`CodexProvider` accepts the following options. See the [API reference](../ref/models/codex.md) for types.

| Option | Purpose |
| --- | --- |
| `cwd` | Codex's workspace. Defaults to the application's current directory. |
| `codex_executable` | Path to the stock CLI. Otherwise the provider finds `codex` on `PATH`. |
| `config_overrides` | Sequence of Codex configuration overrides, such as `sandbox_mode="read-only"`. |
| `env` | Complete environment for the child process. When supplied, it replaces the inherited environment. Include any required `PATH`, authentication, and configuration variables. |
| `api_key` | API key for the child process, when the host allows that login method. |

Ordinary agents can use native Codex commands. These commands follow Codex's configured sandbox and approval policy. A host must allow the native sandbox to start; an outer container or filesystem policy can prevent workspace writes.

Native Codex approval prompts are separate from SDK tool approvals. This provider cannot answer native approval requests and fails the model call when one arrives. For a non-interactive read-only workflow, configure Codex to avoid approval prompts:

```python
provider = CodexProvider(
    config_overrides=('approval_policy="never"', 'sandbox_mode="read-only"'),
)
```

Commands that require more permission fail under this configuration. SDK tool approvals still use the Runner's human-in-the-loop flow.

A `SandboxAgent`, `ShellTool`, or `ApplyPatchTool` uses the SDK's executor or editor instead. The provider disables competing native workspace tools for these runs. Disable inherited Codex MCP servers when using isolated SDK execution tools; otherwise the provider rejects the configuration. SDK-managed MCP tools remain available through the SDK's own connection and approval policy. See [MCP](../mcp.md).

Input guardrails with `run_in_parallel=False` finish before model execution. Cancellation and output guardrails do not roll back native commands or tool side effects. Inspect the workspace before retrying a failed operation.

## Streaming, history, and cancellation

Each model call starts an ephemeral Codex thread. The SDK supplies the instructions, supported messages and tool results, tool schemas, and output schema for that call. Keep conversation history in an [SDK session](../sessions/index.md) or pass `result.to_input_list()` to the next run.

Use `Runner.run_streamed()` and consume `result.stream_events()` as with the default provider. Text deltas arrive during generation. When the model supplies a requested reasoning summary, the stream also includes `response.reasoning_summary_text.delta` events. SDK function calls supply their complete JSON arguments in one update, rather than an argument-by-argument stream.

Call `result.cancel()` to stop a streaming run immediately, or `result.cancel(mode="after_turn")` to stop after the current turn. Continue consuming the stream to observe completion. For a non-streaming async run, cancel its asyncio task. The provider closes the child App Server when the model call finishes or is cancelled; callers do not need to close the provider.

Strict structured output uses Codex's output schema. With `strict_json_schema=False`, the provider puts the schema in the instructions and the SDK validates the final JSON. The latter does not constrain generation on the server.

`OpenAIResponsesCompactionSession` can compact full input history and replay the encrypted result into Codex. It calls the separate OpenAI compact endpoint and needs an OpenAI client or credentials. `compaction_mode="previous_response_id"` cannot use Codex's local turn IDs as stored Responses IDs. See [session compaction](../sessions/index.md).

## Supported features and limits

Switching providers changes the model backend. It does not make every Responses API feature available through Codex.

| Feature | Codex behavior and alternative |
| --- | --- |
| SDK function tools, handoffs, approvals, guardrails, sessions, and structured output | The SDK manages these features in either mode. |
| Required tool use | `ModelSettings(tool_choice="required")` requires an exposed SDK tool call or handoff. Native Codex commands and hosted web search do not satisfy that requirement. It does not select a particular function by name. |
| Deferred tool discovery | Requires a model whose stock Codex catalog advertises tool search. An unknown catalog model can support ordinary tool calls while exposing no discovery tool. Set `CODEX_MODEL` to a capable model and run `uv run -m examples.tools.tool_search --codex` to check discovery and callback results. |
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
| CLI not found | Run `codex --version` in the application's environment, or pass `codex_executable`. |
| Login or configuration-directory failure | Run the CLI under the same user and `CODEX_HOME`; check its login and directory access. An SDK OpenAI key alone does not configure a saved Codex login. |
| Native shell cannot start | Check the host's native sandbox support and the configured sandbox mode. SDK tools and native commands use different executors. |
| A paused sandbox run cannot resume | The SDK needs a writable snapshot directory in either mode. On Linux, if your home directory is read-only, set `XDG_STATE_HOME` to an absolute, writable directory before you run the app. Keep that directory available when you resume. Changing `CODEX_HOME` does not move SDK snapshots. |
| Tools never load | Select a model whose Codex catalog declares tool search. The search examples fail if no tools load. |
| `CodexProvider does not support ModelSettings...` | Remove the named setting only if your workflow does not need it. Otherwise use a provider that implements it. |
| A third-party adapter no longer runs | Register Codex alongside that adapter with [`MultiProvider`](https://github.com/openai/openai-agents-python/blob/feat/simple-codex/examples/model_providers/README.md). Codex does not wrap LiteLLM or any-llm model clients. |
