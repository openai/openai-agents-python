# Models

The Agents SDK comes with out-of-the-box support for OpenAI models in two flavors:

-   **Recommended**: the [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel], which calls OpenAI APIs using the new [Responses API](https://platform.openai.com/docs/api-reference/responses).
-   The [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel], which calls OpenAI APIs using the [Chat Completions API](https://platform.openai.com/docs/api-reference/chat).

## Choosing a model setup

Start with the simplest path that fits your setup:

| If you are trying to... | Recommended path | Read more |
| --- | --- | --- |
| Use OpenAI models only | Use the default OpenAI provider with the Responses model path | [OpenAI models](#openai-models) |
| Use OpenAI Responses API over websocket transport | Keep the Responses model path and enable websocket transport | [Responses WebSocket transport](#responses-websocket-transport) |
| Use OpenAI-hosted subagents | Use the experimental hosted multi-agent model | [Hosted multi-agent](#hosted-multi-agent-experimental) |
| Use one non-OpenAI provider | Start with the built-in provider integration points | [Non-OpenAI models](#non-openai-models) |
| Mix models or providers across agents | Select providers per run or per agent and review feature differences | [Mixing models in one workflow](#mixing-models-in-one-workflow) and [Mixing models across providers](#mixing-models-across-providers) |
| Tune advanced OpenAI Responses request settings | Use `ModelSettings` on the OpenAI Responses path | [Advanced OpenAI Responses settings](#advanced-openai-responses-settings) |
| Use a third-party adapter for non-OpenAI or mixed-provider routing | Compare the supported beta adapters and validate the provider path you plan to ship | [Third-party adapters](#third-party-adapters) |

## OpenAI models

For most OpenAI-only apps, the recommended path is to use string model names with the default OpenAI provider and stay on the Responses model path.

When you don't specify a model when initializing an `Agent`, the default model will be used. The default is currently [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini) with `reasoning.effort="none"` and `verbosity="low"` for low-latency agent workflows. If you have access, we recommend setting your agents to `gpt-5.6-sol` for higher quality while keeping explicit `model_settings`.

If you want to switch to other models like `gpt-5.6-sol`, there are two ways to configure your agents.

### Default model

First, if you want to consistently use a specific model for all agents that do not set a custom model, set the `OPENAI_DEFAULT_MODEL` environment variable before running your agents.

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.6-sol
python3 my_awesome_agent.py
```

Second, you can set a default model for a run via `RunConfig`. If you don't set a model for an agent, this run's model will be used.

```python
from agents import Agent, RunConfig, Runner

agent = Agent(
    name="Assistant",
    instructions="You're a helpful agent.",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model="gpt-5.6-sol"),
)
```

#### GPT-5 models

When you use any GPT-5 model such as `gpt-5.6-sol` in this way, the SDK applies default `ModelSettings`. It sets the ones that work the best for most use cases. To adjust the reasoning effort for the default model, pass your own `ModelSettings`:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.6-sol is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.6-sol",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

For lower latency, using `reasoning.effort="none"` with GPT-5 models is recommended.

GPT-5.6 also supports reasoning mode, persisted reasoning context, and the `"max"` effort level through the existing `reasoning` setting. These controls are available on the Responses API path:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

agent = Agent(
    name="Deep research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        reasoning=Reasoning(
            mode="pro",
            effort="max",
            context="all_turns",
        ),
    ),
)
```

`reasoning.mode` and `reasoning.context` are Responses-only settings. Chat Completions uses only `reasoning.effort`, and the supported effort levels depend on the model and API surface. Use the Responses API for GPT-5.6 `"max"` effort. The Chat Completions adapter ignores mode and context with a warning; set `strict_feature_validation=True` on the OpenAI provider to turn that warning into an error.

When using `context="all_turns"`, preserve the conversation through `previous_response_id`, a server-side conversation, or by replaying prior reasoning items. For stateless `store=False` calls, include `reasoning.encrypted_content` in the response and replay those reasoning items on the next request.

#### ComputerTool model selection

If an agent includes [`ComputerTool`][agents.tool.ComputerTool], the effective model on the actual Responses request determines which computer-tool payload the SDK sends. Explicit `gpt-5.5` requests use the GA built-in `computer` tool, while explicit `computer-use-preview` requests keep the older `computer_use_preview` payload.

Prompt-managed calls are the main exception. If a prompt template owns the model and the SDK omits `model` from the request, the SDK defaults to the preview-compatible computer payload so it does not guess which model the prompt pins. To keep the GA path in that flow, either make `model="gpt-5.5"` explicit on the request or force the GA selector with `ModelSettings(tool_choice="computer")` or `ModelSettings(tool_choice="computer_use")`.

With a registered [`ComputerTool`][agents.tool.ComputerTool], `tool_choice="computer"`, `"computer_use"`, and `"computer_use_preview"` are normalized to the built-in selector that matches the effective request model. If no `ComputerTool` is registered, those strings continue to behave like ordinary function names.

Preview-compatible requests must serialize `environment` and display dimensions up front, so prompt-managed flows that use a [`ComputerProvider`][agents.tool.ComputerProvider] factory should either pass a concrete `Computer` or `AsyncComputer` instance or force the GA selector before sending the request. See [Tools](../tools.md#computertool-and-the-responses-computer-tool) for the full migration details.

#### Non-GPT-5 models

If you pass a non–GPT-5 model name without custom `model_settings`, the SDK reverts to generic `ModelSettings` compatible with any model.

### Responses-only tool search features

The following tool features are supported only with OpenAI Responses models:

-   [`ToolSearchTool`][agents.tool.ToolSearchTool]
-   [`tool_namespace()`][agents.tool.tool_namespace]
-   `@function_tool(defer_loading=True)` and other deferred-loading Responses tool surfaces

These features are rejected on Chat Completions models and on non-Responses backends. When you use deferred-loading tools, add `ToolSearchTool()` to the agent and let the model load tools through `auto` or `required` tool choice instead of forcing bare namespace names or deferred-only function names. See [Tools](../tools.md#hosted-tool-search) for the setup details and current constraints.

### Responses WebSocket transport

By default, OpenAI Responses API requests use HTTP transport. You can opt in to websocket transport when using OpenAI-backed models.

#### Basic setup

```python
from agents import set_default_openai_responses_transport

set_default_openai_responses_transport("websocket")
```

This affects OpenAI Responses models resolved by the default OpenAI provider (including string model names such as `"gpt-5.6-sol"`).

Transport selection happens when the SDK resolves a model name into a model instance. If you pass a concrete [`Model`][agents.models.interface.Model] object, its transport is already fixed: [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] uses websocket, [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] uses HTTP, and [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] stays on Chat Completions. If you pass `RunConfig(model_provider=...)`, that provider controls transport selection instead of the global default.

#### Provider or run-level setup

You can also configure websocket transport per provider or per run:

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
    # Optional low-level websocket keepalive settings.
    responses_websocket_options={"ping_interval": 20.0, "ping_timeout": 60.0},
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

OpenAI-backed providers also accept optional agent registration config. This is an advanced option for cases where your OpenAI setup expects provider-level registration metadata such as a harness ID.

```python
from agents import (
    Agent,
    OpenAIAgentRegistrationConfig,
    OpenAIProvider,
    RunConfig,
    Runner,
)

provider = OpenAIProvider(
    use_responses_websocket=True,
    agent_registration=OpenAIAgentRegistrationConfig(harness_id="your-harness-id"),
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

#### Advanced routing with `MultiProvider`

If you need prefix-based model routing (for example mixing `openai/...` and `any-llm/...` model names in one run), use [`MultiProvider`][agents.MultiProvider] and set `openai_use_responses_websocket=True` there instead.

`MultiProvider` keeps two historical defaults:

-   `openai/...` is treated as an alias for the OpenAI provider, so `openai/gpt-4.1` is routed as model `gpt-4.1`.
-   Unknown prefixes raise `UserError` instead of being passed through.

When you point the OpenAI provider at an OpenAI-compatible endpoint that expects literal namespaced model IDs, opt into the pass-through behavior explicitly. In websocket-enabled setups, keep `openai_use_responses_websocket=True` on the `MultiProvider` as well:

```python
from agents import Agent, MultiProvider, RunConfig, Runner

provider = MultiProvider(
    openai_base_url="https://openrouter.ai/api/v1",
    openai_api_key="...",
    openai_use_responses_websocket=True,
    openai_prefix_mode="model_id",
    unknown_prefix_mode="model_id",
)

agent = Agent(
    name="Assistant",
    instructions="Be concise.",
    model="openai/gpt-4.1",
)

result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

Use `openai_prefix_mode="model_id"` when a backend expects the literal `openai/...` string. Use `unknown_prefix_mode="model_id"` when the backend expects other namespaced model IDs such as `openrouter/openai/gpt-4.1-mini`. These options also work on `MultiProvider` outside websocket transport; this example keeps websocket enabled because it is part of the transport setup described in this section. The same options are also available on [`responses_websocket_session()`][agents.responses_websocket_session].

If you need the same provider-level registration metadata while routing through `MultiProvider`, pass `openai_agent_registration=OpenAIAgentRegistrationConfig(...)` and it will be forwarded to the underlying OpenAI provider.

If you use a custom OpenAI-compatible endpoint or proxy, websocket transport also requires a compatible websocket `/responses` endpoint. In those setups you may need to set `websocket_base_url` explicitly.

#### Notes

-   This is the Responses API over websocket transport, not the [Realtime API](../realtime/guide.md). It does not apply to Chat Completions or non-OpenAI providers unless they support the Responses websocket `/responses` endpoint.
-   Install the `websockets` package if it is not already available in your environment.
-   You can use [`Runner.run_streamed()`][agents.run.Runner.run_streamed] directly after enabling websocket transport. For multi-turn workflows where you want to reuse the same websocket connection across turns (and nested agent-as-tool calls), the [`responses_websocket_session()`][agents.responses_websocket_session] helper is recommended. See the [Running agents](../running_agents.md) guide and [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py).
-   For long reasoning turns or networks with latency spikes, customize websocket keepalive behavior with `responses_websocket_options`. Increase `ping_timeout` to tolerate delayed pong frames, or set `ping_timeout=None` to disable heartbeat timeouts while keeping pings enabled. Prefer HTTP/SSE transport when reliability is more important than websocket latency.
-   By default the SDK disables the incoming message-size limit (`max_size=None`). For long-lived agent processes behind proxies or in memory-constrained containers, set `responses_websocket_options={"max_size": 8 * 1024 * 1024}` to bound per-message memory usage.

### Hosted multi-agent (experimental)

The OpenAI Responses API hosted multi-agent beta lets a GPT-5.6 root model create and coordinate server-hosted subagents. The Agents SDK can keep using its normal `Runner`: hosted orchestration stays on the service, while developer-defined function tools execute in your application.

This integration is experimental and uses the Responses WebSocket transport so local function outputs can be returned to an active hosted agent with `response.inject`. It requires `openai[realtime]>=2.45.0`, including a beta build that exposes `client.beta.responses.connect`. The interface and beta item schemas may change before general availability.

#### Configure the model

Import the model from the experimental module and assign it to an SDK `Agent`:

```python
from agents import Agent
from agents.extensions.experimental.hosted_multi_agent import OpenAIHostedMultiAgentModel

agent = Agent(
    name="Research coordinator",
    instructions="Delegate independent research tasks, then synthesize the findings.",
    model=OpenAIHostedMultiAgentModel(model="gpt-5.6-sol", config={"max_concurrent_subagents": 3}),
)
```

Constructing `OpenAIHostedMultiAgentModel` enables `multi_agent.enabled` and sends the `OpenAI-Beta: responses_multi_agent=v1` WebSocket header. The model uses the default OpenAI client unless `openai_client` is provided. If `max_concurrent_subagents` is omitted, the service default is used.

#### Local function tools

All hosted agents share the model and tools configured for the request. The Responses API decides which hosted agent calls a function. The normal SDK Runner executes the function locally and injects a `function_call_output` with the same call ID into the active WebSocket response, which lets the service resume the original hosted caller. Function execution still passes through the Runner's normal guardrails, hooks, and failure conversion. SDK tool approval interruptions are not supported: any function tool whose `needs_approval` setting is not `False` is rejected before the request is sent.

Use `get_hosted_agent_metadata()` when a tool needs caller-aware logging or authorization:

```python
from typing import Any

from agents import function_tool
from agents.extensions.experimental.hosted_multi_agent import get_hosted_agent_metadata
from agents.tool_context import ToolContext

@function_tool
def lookup_document(ctx: ToolContext[Any], section: str) -> str:
    metadata = get_hosted_agent_metadata(ctx)
    caller = metadata.agent_name if metadata else "unknown"
    print(f"tool caller: {caller}; call ID: {ctx.tool_call_id}")
    return f"Contents for {section}"
```

Hosted agent names are observational metadata, not a local routing mechanism. Route outputs with the call ID supplied by the SDK. For side-effecting tools, use that call ID as an idempotency key and enforce any required authorization in application code before or during tool execution; do not use `needs_approval` with this model. Tool arguments and outputs cross the Responses API boundary.

#### Output and streaming behavior

Only a message attributed to `/root` with phase `final_answer` becomes a normal final message. The experimental adapter filters subagent messages and hosted orchestration records out of the high-level `RunResult`; the SDK never executes those records as local functions.

Raw streaming continues to expose beta Responses events, including hosted output items and `response.inject.created` acknowledgements. The adapter divides one active provider response into SDK-visible logical model turns when a function call is ready, then resumes that same provider response after the Runner produces an output. Use `get_hosted_agent_metadata()` with a raw hosted item or a `ToolContext` to inspect attribution.

#### Relationship to SDK orchestration

Hosted multi-agent is separate from SDK handoffs and agents-as-tools:

-   Hosted multi-agent creates subagents on the OpenAI service. Your application does not create or schedule those subagents.
-   SDK handoffs change the active local SDK `Agent`. They are rejected when this experimental model is used because every hosted agent receives the same handoff tools, which would create conflicting ownership.
-   Agents-as-tools remain available, but using them creates nested client-side and server-side orchestration. Evaluate the additional latency, cost, and tool exposure deliberately.

#### Current limitations

The experimental model rejects `reasoning.summary`, `max_tool_calls`, and caller-supplied `multi_agent` or `betas` overrides. The Responses `/compact` endpoint is not supported by the beta, although an explicit `context_management.compact_threshold` may be used because the service automatically compacts each hosted agent context independently.

One `OpenAIHostedMultiAgentModel` instance owns at most one active hosted response at a time. If a run is abandoned while waiting for local function output, call `await model.close()` to release its WebSocket. Restoring an in-flight hosted response in a different process or event loop is not currently supported.

See the [OpenAI Multi-agent guide](https://developers.openai.com/api/docs/guides/tools-multi-agent) for the underlying Responses API beta behavior. See [`examples/agent_patterns/hosted_multi_agent_beta.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/hosted_multi_agent_beta.py) for non-streaming and streaming SDK usage.

## Non-OpenAI models

If you need a non-OpenAI provider, start with the SDK's built-in provider integration points. In many setups, this is enough without adding a third-party adapter. Examples for each pattern live in [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/).

### Ways to integrate non-OpenAI providers

| Approach | Use it when | Scope |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | One OpenAI-compatible endpoint should be the default for most or all agents | Global default |
| [`ModelProvider`][agents.models.interface.ModelProvider] | One custom provider should apply to a single run | Per run |
| [`Agent.model`][agents.agent.Agent.model] | Different agents need different providers or concrete model objects | Per agent |
| Third-party adapter | You need adapter-managed provider coverage or routing that the built-in paths do not provide | See [Third-party adapters](#third-party-adapters) |

You can integrate other LLM providers with these built-in paths:

1. [`set_default_openai_client`][agents.set_default_openai_client] is useful in cases where you want to globally use an instance of `AsyncOpenAI` as the LLM client. This is for cases where the LLM provider has an OpenAI compatible API endpoint, and you can set the `base_url` and `api_key`. See a configurable example in [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py).
2. [`ModelProvider`][agents.models.interface.ModelProvider] is at the `Runner.run` level. This lets you say "use a custom model provider for all agents in this run". See a configurable example in [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py).
3. [`Agent.model`][agents.agent.Agent.model] lets you specify the model on a specific Agent instance. This enables you to mix and match different providers for different agents. See a configurable example in [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py).

In cases where you do not have an API key from `platform.openai.com`, we recommend disabling tracing via `set_tracing_disabled()`, or setting up a [different tracing processor](../tracing.md).

``` python
from agents import Agent, AsyncOpenAI, OpenAIChatCompletionsModel, set_tracing_disabled

set_tracing_disabled(disabled=True)

client = AsyncOpenAI(api_key="Api_Key", base_url="Base URL of Provider")
model = OpenAIChatCompletionsModel(model="Model_Name", openai_client=client)

agent= Agent(name="Helping Agent", instructions="You are a Helping Agent", model=model)
```

!!! note

    In these examples, we use the Chat Completions API/model, because many LLM providers still do not support the Responses API. If your LLM provider does support it, we recommend using Responses.

## Mixing models in one workflow

Within a single workflow, you may want to use different models for each agent. For example, you could use a smaller, faster model for triage, while using a larger, more capable model for complex tasks. When configuring an [`Agent`][agents.Agent], you can select a specific model by either:

1. Passing the name of a model.
2. Passing any model name + a [`ModelProvider`][agents.models.interface.ModelProvider] that can map that name to a Model instance.
3. Directly providing a [`Model`][agents.models.interface.Model] implementation.

!!! note

    While our SDK supports both the [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] and the [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] shapes, we recommend using a single model shape for each workflow because the two shapes support a different set of features and tools. If your workflow requires mixing and matching model shapes, make sure that all the features you're using are available on both.

```python
import asyncio

from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model="gpt-5-mini", # (1)!
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model=OpenAIChatCompletionsModel( # (2)!
        model="gpt-5-nano",
        openai_client=AsyncOpenAI()
    ),
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    model="gpt-5.6-sol",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

1.  Sets the name of an OpenAI model directly.
2.  Provides a [`Model`][agents.models.interface.Model] implementation.

When you want to further configure the model used for an agent, you can pass [`ModelSettings`][agents.models.interface.ModelSettings], which provides optional model configuration parameters such as temperature.

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(temperature=0.1),
)
```

## Advanced OpenAI Responses settings

When you are on the OpenAI Responses path and need more control, start with `ModelSettings`.

### Common advanced `ModelSettings` options

When you are using the OpenAI Responses API, several request fields already have direct `ModelSettings` fields, so you do not need `extra_args` for them.

- `parallel_tool_calls`: Allow or forbid multiple tool calls in the same turn.
- `truncation`: Set `"auto"` to let the Responses API drop the oldest conversation items instead of failing when context would overflow.
- `store`: Control whether the generated response is stored server-side for later retrieval. This matters for follow-up workflows that rely on response IDs, and for session compaction flows that may need to fall back to local input when `store=False`.
- `context_management`: Configure server-side context handling such as Responses compaction with `compact_threshold`.
- `prompt_cache_retention`: Configure extended retention for earlier model families, for example
  with `"24h"`.
- `prompt_cache_options`: Select implicit or explicit prompt caching and, for GPT-5.6, configure a `"30m"` cache TTL.
- `response_include`: Request richer response payloads such as `web_search_call.action.sources`, `file_search_call.results`, or `reasoning.encrypted_content`.
- `top_logprobs`: Request top-token logprobs for output text. The SDK also adds `message.output_text.logprobs` automatically.
- `retry`: Opt in to runner-managed retry settings for model calls. See [Runner-managed retries](#runner-managed-retries).

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        context_management=[{"type": "compaction", "compact_threshold": 200000}],
        prompt_cache_options={"mode": "explicit", "ttl": "30m"},
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

With explicit prompt caching, add a breakpoint to the content part that ends the reusable prefix. The same `ModelSettings.prompt_cache_options` field is passed through on Responses and Chat Completions requests, and the Chat Completions converter preserves breakpoints on text, image, audio, and file content parts.

```python
from agents import Runner

result = await Runner.run(
    research_agent,
    [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Reusable background material...",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {
                    "type": "input_text",
                    "text": "Analyze the latest question.",
                },
            ],
        }
    ],
)
```

`prompt_cache_retention` remains available for earlier model families that use the legacy
retention control. Do not combine a direct `ModelSettings` field with the same key in
`extra_args`.

When you set `store=False`, the Responses API does not keep that response available for later server-side retrieval. This is useful for stateless or zero-data-retention style flows, but it also means features that would otherwise reuse response IDs need to rely on locally managed state instead. For example, [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] switches its default `"auto"` compaction path to input-based compaction when the last response was not stored. See the [Sessions guide](../sessions/index.md#openai-responses-compaction-sessions).

Server-side compaction is different from [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession]. `context_management=[{"type": "compaction", "compact_threshold": ...}]` is sent with each Responses API request, and the API can emit compaction items as part of the response when the rendered context crosses the threshold. `OpenAIResponsesCompactionSession` calls the standalone `responses.compact` endpoint between turns and rewrites the local session history.

### Passing `extra_args`

Use `extra_args` when you need provider-specific or newer request fields that the SDK does not expose directly at the top level yet.

Also, when you use OpenAI's Responses API, [there are a few other optional parameters](https://platform.openai.com/docs/api-reference/responses/create) (e.g., `user`, `service_tier`, and so on). If they are not available at the top level, you can use `extra_args` to pass them as well. Do not also set the same request field through a direct `ModelSettings` field.

```python
from agents import Agent, ModelSettings

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model="gpt-4.1",
    model_settings=ModelSettings(
        temperature=0.1,
        extra_args={"service_tier": "flex", "user": "user_12345"},
    ),
)
```

## Runner-managed retries

Retries are runtime-only and opt in. The SDK does not retry general model requests unless you set `ModelSettings(retry=...)` and your retry policy chooses to retry.

```python
from agents import Agent, ModelRetrySettings, ModelSettings, retry_policies

agent = Agent(
    name="Assistant",
    model="gpt-5.6-sol",
    model_settings=ModelSettings(
        retry=ModelRetrySettings(
            max_retries=4,
            backoff={
                "initial_delay": 0.5,
                "max_delay": 5.0,
                "multiplier": 2.0,
                "jitter": True,
            },
            policy=retry_policies.any(
                retry_policies.provider_suggested(),
                retry_policies.retry_after(),
                retry_policies.network_error(),
                retry_policies.http_status([408, 409, 429, 500, 502, 503, 504]),
            ),
        )
    ),
)
```

`ModelRetrySettings` has three fields:

<div class="field-table" markdown="1">

| Field | Type | Notes |
| --- | --- | --- |
| `max_retries` | `int | None` | Number of retry attempts allowed after the initial request. |
| `backoff` | `ModelRetryBackoffSettings | dict | None` | Default delay strategy when the policy retries without returning an explicit delay. `backoff.max_delay` caps this computed backoff delay only. It does not cap explicit delays returned by a policy or retry-after hints. |
| `policy` | `RetryPolicy | None` | Callback that decides whether to retry. This field is runtime-only and is not serialized. |

</div>

A retry policy receives a [`RetryPolicyContext`][agents.retry.RetryPolicyContext] with:

- `attempt` and `max_retries` so you can make attempt-aware decisions.
- `stream` so you can branch between streamed and non-streamed behavior.
- `error` for raw inspection.
- `normalized` facts such as `status_code`, `retry_after`, `error_code`, `is_network_error`, `is_timeout`, and `is_abort`.
- `provider_advice` when the underlying model adapter can supply retry guidance.

The policy can return either:

- `True` / `False` for a simple retry decision.
- A [`RetryDecision`][agents.retry.RetryDecision] when you want to override the delay or attach a diagnostic reason.

The SDK exports ready-made helpers on `retry_policies`:

| Helper | Behavior |
| --- | --- |
| `retry_policies.never()` | Always opts out. |
| `retry_policies.provider_suggested()` | Follows provider retry advice when available. |
| `retry_policies.network_error()` | Matches transient transport and timeout failures. |
| `retry_policies.http_status([...])` | Matches selected HTTP status codes. |
| `retry_policies.retry_after()` | Retries only when a retry-after hint is available, using that delay. This helper treats the retry-after value as an explicit policy delay, so `backoff.max_delay` does not cap it. |
| `retry_policies.any(...)` | Retries when any nested policy opts in. |
| `retry_policies.all(...)` | Retries only when every nested policy opts in. |

When you compose policies, `provider_suggested()` is the safest first building block because it preserves provider vetoes and replay-safety approvals when the provider can distinguish them.

##### Safety boundaries

Some failures are never retried automatically:

- Abort errors.
- Requests where provider advice marks replay as unsafe.
- Streamed runs after output has already started in a way that would make replay unsafe.

Stateful follow-up requests using `previous_response_id` or `conversation_id` are also treated more conservatively. For those requests, non-provider predicates such as `network_error()` or `http_status([500])` are not enough by themselves. The retry policy should include a replay-safe approval from the provider, typically via `retry_policies.provider_suggested()`.

##### Runner and agent merge behavior

`retry` is deep-merged between runner-level and agent-level `ModelSettings`:

- An agent can override only `retry.max_retries` and still inherit the runner's `policy`.
- An agent can override only part of `retry.backoff` and keep sibling backoff fields from the runner.
- `policy` is runtime-only, so serialized `ModelSettings` keep `max_retries` and `backoff` but omit the callback itself.

For fuller examples, see [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) and the [adapter-backed retry example](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py).

## Troubleshooting non-OpenAI providers

### Tracing client error 401

If you get errors related to tracing, this is because traces are uploaded to OpenAI servers, and you don't have an OpenAI API key. You have three options to resolve this:

1. Disable tracing entirely: [`set_tracing_disabled(True)`][agents.set_tracing_disabled].
2. Set an OpenAI key for tracing: [`set_tracing_export_api_key(...)`][agents.set_tracing_export_api_key]. This API key will only be used for uploading traces, and must be from [platform.openai.com](https://platform.openai.com/).
3. Use a non-OpenAI trace processor. See the [tracing docs](../tracing.md#custom-tracing-processors).

### Responses API support

The SDK uses the Responses API by default, but many other LLM providers still do not support it. You may see 404s or similar issues as a result. To resolve, you have two options:

1. Call [`set_default_openai_api("chat_completions")`][agents.set_default_openai_api]. This works if you are setting `OPENAI_API_KEY` and `OPENAI_BASE_URL` via environment vars.
2. Use [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel]. There are examples [here](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/).

### Chat Completions compatibility options

When you route through Chat Completions, the SDK preserves compatibility by silently dropping Responses-only fields that Chat Completions cannot send, such as `previous_response_id`, `conversation_id`, prompts, or non-text-only tool outputs. If you want those mismatches to fail fast during development, enable strict feature validation on the OpenAI provider:

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses=False,
    strict_feature_validation=True,
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

If you use [`MultiProvider`][agents.MultiProvider], pass `openai_strict_feature_validation=True` instead.

Some OpenAI-compatible Chat Completions providers stream tool-call deltas in chunks that are not reliable enough for incremental SDK processing. In that case, enable streamed tool-call buffering so the SDK emits tool calls only after the provider stream finishes:

```python
from agents import OpenAIProvider

provider = OpenAIProvider(
    use_responses=False,
    buffer_streamed_tool_calls=True,
)
```

For [`MultiProvider`][agents.MultiProvider], use `openai_buffer_streamed_tool_calls=True`.

### Structured outputs support

Some model providers don't have support for [structured outputs](https://platform.openai.com/docs/guides/structured-outputs). This sometimes results in an error that looks something like this:

```

BadRequestError: Error code: 400 - {'error': {'message': "'response_format.type' : value is not one of the allowed values ['text','json_object']", 'type': 'invalid_request_error'}}

```

This is a shortcoming of some model providers - they support JSON outputs, but don't allow you to specify the `json_schema` to use for the output. We are working on a fix for this, but we suggest relying on providers that do have support for JSON schema output, because otherwise your app will often break because of malformed JSON.

## Mixing models across providers

You need to be aware of feature differences between model providers, or you may run into errors. For example, OpenAI supports structured outputs, multimodal input, and hosted file search and web search, but many other providers don't support these features. Be aware of these limitations:

-   Don't send unsupported `tools` to providers that don't understand them
-   Filter out multimodal inputs before calling models that are text-only
-   Be aware that providers that don't support structured JSON outputs will occasionally produce invalid JSON.

## Third-party adapters

Reach for a third-party adapter only when the SDK's built-in provider integration points are not enough. If you are using OpenAI models only with this SDK, prefer the built-in [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] path instead of Any-LLM or LiteLLM. Third-party adapters are for cases where you need to combine OpenAI models with non-OpenAI providers, or need adapter-managed provider coverage or routing that the built-in paths do not provide. Adapters add another compatibility layer between the SDK and the upstream model provider, so feature support and request semantics can vary by provider. The SDK currently includes Any-LLM and LiteLLM as best-effort, beta adapter integrations.

### Any-LLM

Any-LLM support is included on a best-effort, beta basis for cases where you need Any-LLM-managed provider coverage or routing.

Depending on the upstream provider path, Any-LLM may use the Responses API, Chat Completions-compatible APIs, or provider-specific compatibility layers.

If you need Any-LLM, install `openai-agents[any-llm]`, then start from [`examples/model_providers/any_llm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_auto.py) or [`examples/model_providers/any_llm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/any_llm_provider.py). You can use `any-llm/...` model names with [`MultiProvider`][agents.MultiProvider], instantiate `AnyLLMModel` directly, or use `AnyLLMProvider` at run scope. If you need to pin the model surface explicitly, pass `api="responses"` or `api="chat_completions"` when constructing `AnyLLMModel`.

Any-LLM remains a third-party adapter layer, so provider dependencies and capability gaps are defined upstream by Any-LLM rather than by the SDK. Usage metrics are propagated automatically when the upstream provider returns them, but streamed Chat Completions backends may require `ModelSettings(include_usage=True)` before they emit usage chunks. Validate the exact provider backend you plan to deploy if you depend on structured outputs, tool calling, usage reporting, or Responses-specific behavior.

### LiteLLM

LiteLLM support is included on a best-effort, beta basis for cases where you need LiteLLM-specific provider coverage or routing.

If you need LiteLLM, install `openai-agents[litellm]`, then start from [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) or [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py). You can use `litellm/...` model names or instantiate [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] directly.

Some LiteLLM-backed providers do not populate SDK usage metrics by default. If you need usage reporting, pass `ModelSettings(include_usage=True)` and validate the exact provider backend you plan to deploy if you depend on structured outputs, tool calling, usage reporting, or adapter-specific routing behavior.

If LiteLLM emits Pydantic serializer warnings for response objects, you can opt in to the SDK's compatibility patch before importing the LiteLLM adapter:

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

The patch is disabled by default and is enabled only for `1` or `true` values. It suppresses a specific class of LiteLLM response-serialization warnings by wrapping a private LiteLLM logging helper, so treat it as a targeted workaround rather than a general serialization setting. Because it depends on a private LiteLLM API, validate it again when upgrading LiteLLM and remove the environment variable when the upstream warning no longer occurs.
