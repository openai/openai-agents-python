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
| Use one non-OpenAI provider | Start with the built-in provider integration points | [Non-OpenAI models](#non-openai-models) |
| Mix models or providers across agents | Select providers per run or per agent and review feature differences | [Mixing models in one workflow](#mixing-models-in-one-workflow) and [Mixing models across providers](#mixing-models-across-providers) |
| Tune advanced OpenAI Responses request settings | Use `ModelSettings` on the OpenAI Responses path | [Advanced OpenAI Responses settings](#advanced-openai-responses-settings) |
| Use LiteLLM for non-OpenAI Chat Completions providers | Treat LiteLLM as a beta fallback | [LiteLLM](#litellm) |

## OpenAI models

For most OpenAI-only apps, the recommended path is to use string model names with the default OpenAI provider and stay on the Responses model path.

When you don't specify a model when initializing an `Agent`, the default model will be used. The default is currently [`gpt-4.1`](https://developers.openai.com/api/docs/models/gpt-4.1) for compatibility and low latency. If you have access, we recommend setting your agents to [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) for higher quality while keeping explicit `model_settings`.

If you want to switch to other models like [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4), there are two ways to configure your agents.

### Default model

First, if you want to consistently use a specific model for all agents that do not set a custom model, set the `OPENAI_DEFAULT_MODEL` environment variable before running your agents.

```bash
export OPENAI_DEFAULT_MODEL=gpt-5.4
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
    run_config=RunConfig(model="gpt-5.4"),
)
```

#### GPT-5 models

When you use any GPT-5 model such as [`gpt-5.4`](https://developers.openai.com/api/docs/models/gpt-5.4) in this way, the SDK applies default `ModelSettings`. It sets the ones that work the best for most use cases. To adjust the reasoning effort for the default model, pass your own `ModelSettings`:

```python
from openai.types.shared import Reasoning
from agents import Agent, ModelSettings

my_agent = Agent(
    name="My Agent",
    instructions="You're a helpful agent.",
    # If OPENAI_DEFAULT_MODEL=gpt-5.4 is set, passing only model_settings works.
    # It's also fine to pass a GPT-5 model name explicitly:
    model="gpt-5.4",
    model_settings=ModelSettings(reasoning=Reasoning(effort="high"), verbosity="low")
)
```

For lower latency, using `reasoning.effort="none"` with `gpt-5.4` is recommended. The gpt-4.1 family (including mini and nano variants) also remains a solid choice for building interactive agent apps.

#### ComputerTool model selection

If an agent includes [`ComputerTool`][agents.tool.ComputerTool], the effective model on the actual Responses request determines which computer-tool payload the SDK sends. Explicit `gpt-5.4` requests use the GA built-in `computer` tool, while explicit `computer-use-preview` requests keep the older `computer_use_preview` payload.

Prompt-managed calls are the main exception. If a prompt template owns the model and the SDK omits `model` from the request, the SDK defaults to the preview-compatible computer payload so it does not guess which model the prompt pins. To keep the GA path in that flow, either make `model="gpt-5.4"` explicit on the request or force the GA selector with `ModelSettings(tool_choice="computer")` or `ModelSettings(tool_choice="computer_use")`.

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

This affects OpenAI Responses models resolved by the default OpenAI provider (including string model names such as `"gpt-5.4"`).

Transport selection happens when the SDK resolves a model name into a model instance. If you pass a concrete [`Model`][agents.models.interface.Model] object, its transport is already fixed: [`OpenAIResponsesWSModel`][agents.models.openai_responses.OpenAIResponsesWSModel] uses websocket, [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] uses HTTP, and [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] stays on Chat Completions. If you pass `RunConfig(model_provider=...)`, that provider controls transport selection instead of the global default.

#### Provider or run-level setup

You can also configure websocket transport per provider or per run:

```python
from agents import Agent, OpenAIProvider, RunConfig, Runner

provider = OpenAIProvider(
    use_responses_websocket=True,
    # Optional; if omitted, OPENAI_WEBSOCKET_BASE_URL is used when set.
    websocket_base_url="wss://your-proxy.example/v1",
)

agent = Agent(name="Assistant")
result = await Runner.run(
    agent,
    "Hello",
    run_config=RunConfig(model_provider=provider),
)
```

#### Advanced routing with `MultiProvider`

If you need prefix-based model routing (for example mixing `openai/...` and `litellm/...` model names in one run), use [`MultiProvider`][agents.MultiProvider] and set `openai_use_responses_websocket=True` there instead.

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

If you use a custom OpenAI-compatible endpoint or proxy, websocket transport also requires a compatible websocket `/responses` endpoint. In those setups you may need to set `websocket_base_url` explicitly.

#### Notes

-   This is the Responses API over websocket transport, not the [Realtime API](../realtime/guide.md). It does not apply to Chat Completions or non-OpenAI providers unless they support the Responses websocket `/responses` endpoint.
-   Install the `websockets` package if it is not already available in your environment.
-   You can use [`Runner.run_streamed()`][agents.run.Runner.run_streamed] directly after enabling websocket transport. For multi-turn workflows where you want to reuse the same websocket connection across turns (and nested agent-as-tool calls), the [`responses_websocket_session()`][agents.responses_websocket_session] helper is recommended. See the [Running agents](../running_agents.md) guide and [`examples/basic/stream_ws.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/stream_ws.py).

## Non-OpenAI models

If you need a non-OpenAI provider, start with the SDK's built-in provider integration points. In many setups, this is enough without adding LiteLLM. Examples for each pattern live in [examples/model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/).

### Ways to integrate non-OpenAI providers

| Approach | Use it when | Scope |
| --- | --- | --- |
| [`set_default_openai_client`][agents.set_default_openai_client] | One OpenAI-compatible endpoint should be the default for most or all agents | Global default |
| [`ModelProvider`][agents.models.interface.ModelProvider] | One custom provider should apply to a single run | Per run |
| [`Agent.model`][agents.agent.Agent.model] | Different agents need different providers or concrete model objects | Per agent |
| LiteLLM (beta) | You need LiteLLM-specific provider coverage or routing | See [LiteLLM](#litellm) |

You can integrate other LLM providers with these built-in paths:

1. [`set_default_openai_client`][agents.set_default_openai_client] is useful in cases where you want to globally use an instance of `AsyncOpenAI` as the LLM client. This is for cases where the LLM provider has an OpenAI compatible API endpoint, and you can set the `base_url` and `api_key`. See a configurable example in [examples/model_providers/custom_example_global.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_global.py).
2. [`ModelProvider`][agents.models.interface.ModelProvider] is at the `Runner.run` level. This lets you say "use a custom model provider for all agents in this run". See a configurable example in [examples/model_providers/custom_example_provider.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_provider.py).
3. [`Agent.model`][agents.agent.Agent.model] lets you specify the model on a specific Agent instance. This enables you to mix and match different providers for different agents. See a configurable example in [examples/model_providers/custom_example_agent.py](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/custom_example_agent.py).

In cases where you do not have an API key from `platform.openai.com`, we recommend disabling tracing via `set_tracing_disabled()`, or setting up a [different tracing processor](../tracing.md).

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
from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel
import asyncio

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
    model="gpt-5.4",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
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
- `prompt_cache_retention`: Keep cached prompt prefixes around longer, for example with `"24h"`.
- `response_include`: Request richer response payloads such as `web_search_call.action.sources`, `file_search_call.results`, or `reasoning.encrypted_content`.
- `top_logprobs`: Request top-token logprobs for output text. The SDK also adds `message.output_text.logprobs` automatically.
- `retry`: Opt in to runner-managed retry settings for model calls. See [Runner-managed retries](#runner-managed-retries).

```python
from agents import Agent, ModelSettings

research_agent = Agent(
    name="Research agent",
    model="gpt-5.4",
    model_settings=ModelSettings(
        parallel_tool_calls=False,
        truncation="auto",
        store=True,
        prompt_cache_retention="24h",
        response_include=["web_search_call.action.sources"],
        top_logprobs=5,
    ),
)
```

When you set `store=False`, the Responses API does not keep that response available for later server-side retrieval. This is useful for stateless or zero-data-retention style flows, but it also means features that would otherwise reuse response IDs need to rely on locally managed state instead. For example, [`OpenAIResponsesCompactionSession`][agents.memory.openai_responses_compaction_session.OpenAIResponsesCompactionSession] switches its default `"auto"` compaction path to input-based compaction when the last response was not stored. See the [Sessions guide](../sessions/index.md#openai-responses-compaction-sessions).

### Passing `extra_args`

Use `extra_args` when you need provider-specific or newer request fields that the SDK does not expose directly at the top level yet.

Also, when you use OpenAI's Responses API, [there are a few other optional parameters](https://platform.openai.com/docs/api-reference/responses/create) (e.g., `user`, `service_tier`, and so on). If they are not available at the top level, you can use `extra_args` to pass them as well.

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
    model="gpt-5.4",
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
| `backoff` | `ModelRetryBackoffSettings | dict | None` | Default delay strategy when the policy retries without returning an explicit delay. |
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
| `retry_policies.retry_after()` | Retries only when a retry-after hint is available, using that delay. |
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

For fuller examples, see [`examples/basic/retry.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry.py) and [`examples/basic/retry_litellm.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/retry_litellm.py).

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

## LiteLLM

LiteLLM support is included on a best-effort, beta basis for cases where you need to bring non-OpenAI providers into an Agents SDK workflow.

If you are using OpenAI models with this SDK, we recommend the built-in [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] path instead of LiteLLM.

If you need to combine OpenAI models with non-OpenAI providers, especially through Chat Completions-compatible APIs, LiteLLM is available as a beta option, but it may not be the optimal choice for every setup.

If you need LiteLLM for a non-OpenAI provider, install `openai-agents[litellm]`, then start from [`examples/model_providers/litellm_auto.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_auto.py) or [`examples/model_providers/litellm_provider.py`](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers/litellm_provider.py). You can either use `litellm/...` model names or instantiate [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] directly.

If you want LiteLLM responses to populate the SDK's usage metrics, pass `ModelSettings(include_usage=True)`.
