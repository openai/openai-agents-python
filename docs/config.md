# Configuration

This page covers SDK-wide defaults that you usually set once during application startup, such as the default OpenAI key or client, the default OpenAI API shape, tracing export defaults, and logging behavior.

These defaults still apply to sandbox-based workflows, but sandbox workspaces, sandbox clients, and session reuse are configured separately.

If you need to configure a specific agent or run instead, start with:

-   [Agents](agents.md) for instructions, tools, output types, handoffs, and guardrails on a plain `Agent`.
-   [Running agents](running_agents.md) for `RunConfig`, sessions, and conversation-state options.
-   [Sandbox agents](sandbox/guide.md) for `SandboxRunConfig`, manifests, capabilities, and sandbox-client-specific workspace setup.
-   [Models](models/index.md) for model selection and provider configuration.
-   [Tracing](tracing.md) for per-run tracing metadata and custom trace processors.

## Configuration objects and dictionaries

Configuration parameters defined by the SDK generally accept either their typed settings object or a dictionary containing the same fields. This applies across agent, run, model, session, sandbox, and voice configuration boundaries whose type annotations include a dictionary. Nested settings types defined by the SDK can also use dictionaries.

```python
from agents import Agent

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings={
        "reasoning": {"effort": "high"},
        "verbosity": "low",
    },
)
```

The SDK normalizes these dictionaries into the corresponding settings objects. Unknown fields in dataclass configuration types defined by the SDK raise `TypeError`, which helps catch misspelled option names early. Check the parameter's type annotation or API reference to confirm whether a specific boundary accepts a dictionary.

## API keys and clients

By default, the SDK uses the `OPENAI_API_KEY` environment variable for LLM requests and tracing. The key is resolved when the SDK first creates an OpenAI client (lazy initialization), so set the environment variable before your first model call. If you are unable to set that environment variable before your app starts, you can use the [set_default_openai_key()][agents.set_default_openai_key] function to set the key.

```python
from agents import set_default_openai_key

set_default_openai_key("sk-...")
```

Alternatively, you can also configure an OpenAI client to be used. By default, the SDK creates an `AsyncOpenAI` instance, using the API key from the environment variable or the default key set above. You can change this by using the [set_default_openai_client()][agents.set_default_openai_client] function.

```python
from openai import AsyncOpenAI
from agents import set_default_openai_client

custom_client = AsyncOpenAI(base_url="...", api_key="...")
set_default_openai_client(custom_client)
```

When you pass an explicit client to [`OpenAIProvider`][agents.models.openai_provider.OpenAIProvider], that client owns its connection and account settings. Do not also pass `api_key`, `base_url`, `websocket_base_url`, `organization`, or `project` to `OpenAIProvider`; combining `openai_client` with any of those arguments raises [`UserError`][agents.exceptions.UserError] instead of silently ignoring the duplicate value. Set the intended values when constructing `AsyncOpenAI`.

When `openai_client` is omitted, `OpenAIProvider` reuses the SDK-wide default client only if `api_key`, `base_url`, `websocket_base_url`, `organization`, and `project` are all `None`. Passing any of those options, including an empty string, makes the provider create its own client and gives the provider option precedence over the SDK-wide default client. Leave every provider option as `None` when the provider should inherit the client installed by `set_default_openai_client()`.

[`OpenAIVoiceModelProvider`][agents.voice.models.openai_model_provider.OpenAIVoiceModelProvider] uses the same ownership and precedence rules for `api_key`, `base_url`, `organization`, and `project`. Its explicit `openai_client` cannot be combined with any of those four options.

### Custom HTTP clients with `openai` v3

Version 0.21.0 requires `openai>=3.0.0,<4`. The default OpenAI provider uses HTTPX2, so most applications do not need to configure an HTTP client directly. If your application passes `http_client=` to `AsyncOpenAI`, use HTTPX2 types for the custom client and its transport-facing options:

```python
import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from agents import set_default_openai_client

http_client = DefaultAsyncHttpx2Client(
    timeout=httpx2.Timeout(30.0, connect=5.0),
)
custom_client = AsyncOpenAI(
    api_key="...",
    http_client=http_client,
)
set_default_openai_client(custom_client)
```

The same migration applies to custom transports, authentication, event hooks, mock transports, URLs, requests, responses, and transport exception handling. Use their `httpx2` equivalents. The Agents SDK does not convert arbitrary legacy `httpx` objects to HTTPX2. The OpenAI Python SDK provides a temporary compatibility path for legacy clients when the application installs `httpx` explicitly, but new and migrated code should use HTTPX2.

This OpenAI client boundary is separate from local MCP transport customization. MCP Python SDK v1 uses its own legacy `httpx` dependency, while MCP Python SDK v2 uses `httpx2`; see [MCP Python SDK v1 and v2](mcp.md#mcp-python-sdk-v1-and-v2).

If you prefer environment-based endpoint configuration, the default OpenAI provider also reads `OPENAI_BASE_URL`. When you enable Responses websocket transport, it also reads `OPENAI_WEBSOCKET_BASE_URL` for the websocket `/responses` endpoint.

```bash
export OPENAI_BASE_URL="https://your-openai-compatible-endpoint.example/v1"
export OPENAI_WEBSOCKET_BASE_URL="wss://your-openai-compatible-endpoint.example/v1"
```

Finally, you can also customize the OpenAI API that is used. By default, we use the OpenAI Responses API. You can override this to use the Chat Completions API by using the [set_default_openai_api()][agents.set_default_openai_api] function.

```python
from agents import set_default_openai_api

set_default_openai_api("chat_completions")
```

## OpenAI provider defaults

Providers that use the SDK's OpenAI backend also read SDK-wide defaults when they map model-name strings to models. Use [`set_default_openai_responses_transport()`][agents.set_default_openai_responses_transport] to make OpenAI Responses models use websocket transport by default:

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

This affects OpenAI Responses models that result when the default OpenAI provider resolves a model name. For provider-level setup, connection reuse, keepalive options, and custom websocket endpoints, see [Responses WebSocket transport](models/index.md#responses-websocket-transport).

If your OpenAI setup expects provider-level agent registration metadata, configure a default harness ID once at startup:

```python
from agents import set_default_openai_harness

set_default_openai_harness("your-harness-id")
```

You can also pass the full registration object:

```python
from agents import OpenAIAgentRegistrationConfig, set_default_openai_agent_registration

set_default_openai_agent_registration(
    OpenAIAgentRegistrationConfig(harness_id="your-harness-id")
)
```

If no SDK default is set, providers that use the SDK's OpenAI backend fall back to the `OPENAI_AGENT_HARNESS_ID` environment variable. When a harness ID is configured, the SDK adds it to trace metadata as `agent_harness_id` unless that key is already present in `RunConfig.trace_metadata`.

## Tracing

Tracing is enabled by default. By default it uses the same OpenAI API key as your model requests from the section above (that is, the environment variable or the default key you set). You can specifically set the API key used for tracing by using the [`set_tracing_export_api_key`][agents.set_tracing_export_api_key] function.

```python
from agents import set_tracing_export_api_key

set_tracing_export_api_key("sk-...")
```

If your model traffic uses one key or client but tracing should use a different OpenAI key, pass `use_for_tracing=False` when setting the default key or client, then configure tracing separately. The same pattern works with [`set_default_openai_key()`][agents.set_default_openai_key] if you are not using a custom client.

```python
from openai import AsyncOpenAI
from agents import (
    set_default_openai_client,
    set_tracing_export_api_key,
)

custom_client = AsyncOpenAI(base_url="https://your-openai-compatible-endpoint.example/v1", api_key="provider-key")
set_default_openai_client(custom_client, use_for_tracing=False)

set_tracing_export_api_key("sk-tracing")
```

If you need to attribute traces to a specific organization or project when using the default exporter, set these environment variables before your app starts:

```bash
export OPENAI_ORG_ID="org_..."
export OPENAI_PROJECT_ID="proj_..."
```

You can also set a tracing API key per run without changing the global exporter.

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(tracing={"api_key": "sk-tracing-123"}),
)
```

You can also disable tracing entirely by using the [`set_tracing_disabled()`][agents.set_tracing_disabled] function.

```python
from agents import set_tracing_disabled

set_tracing_disabled(True)
```

If you want to keep tracing enabled but exclude potentially sensitive inputs/outputs from trace payloads, set [`RunConfig.trace_include_sensitive_data`][agents.run.RunConfig.trace_include_sensitive_data] to `False`:

```python
from agents import Runner, RunConfig

await Runner.run(
    agent,
    input="Hello",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

You can also change the default without code by setting this environment variable before your app starts:

```bash
export OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA=0
```

For full tracing controls, see the [tracing guide](tracing.md).

## Debug logging

The SDK defines two Python loggers (`openai.agents` and `openai.agents.tracing`) and does not attach handlers by default. Logs follow your application's Python logging configuration.

To enable verbose logging, use the [`enable_verbose_stdout_logging()`][agents.enable_verbose_stdout_logging] function.

```python
from agents import enable_verbose_stdout_logging

enable_verbose_stdout_logging()
```

Alternatively, you can customize the logs by adding handlers, filters, formatters, etc. You can read more in the [Python logging guide](https://docs.python.org/3/howto/logging.html).

```python
import logging

logger = logging.getLogger("openai.agents") # or openai.agents.tracing for the Tracing logger

# To make all logs show up
logger.setLevel(logging.DEBUG)
# To make info and above show up
logger.setLevel(logging.INFO)
# To make warning and above show up
logger.setLevel(logging.WARNING)
# etc

# You can customize this as needed, but this will output to `stderr` by default
logger.addHandler(logging.StreamHandler())
```

### Sensitive data in logs and diagnostics

Certain logs and diagnostic exceptions may contain sensitive data (for example, model or tool inputs and outputs).

By default, the SDK does **not** log LLM inputs/outputs or tool inputs/outputs. These protections are controlled by:

```bash
OPENAI_AGENTS_DONT_LOG_MODEL_DATA=1
OPENAI_AGENTS_DONT_LOG_TOOL_DATA=1
```

If you need to include this data temporarily for debugging, set either variable to `0` (or `false`) before your app starts:

```bash
export OPENAI_AGENTS_DONT_LOG_MODEL_DATA=0
export OPENAI_AGENTS_DONT_LOG_TOOL_DATA=0
```

These flags also control whether affected failures retain payload-bearing diagnostic details. For example, with tool-data redaction enabled, invalid arguments for a `FunctionTool` raise a generic `ModelBehaviorError` without chaining the underlying validation error. Setting either variable to `0` can expose raw model or tool data in logs, exception messages, exception chains, and other diagnostic context, so enable it only in a controlled development environment.
