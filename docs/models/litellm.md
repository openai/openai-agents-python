# Using any model via LiteLLM

!!! note

    The LiteLLM integration is in beta. You may run into issues with some model providers, especially smaller ones. Please report any issues via [Github issues](https://github.com/openai/openai-agents-python/issues) and we'll fix quickly.

[LiteLLM](https://docs.litellm.ai/docs/) is a library that allows you to use 100+ models via a single interface. We've added a LiteLLM integration to allow you to use any AI model in the Agents SDK.

## Setup

You'll need to ensure `litellm` is available. You can do this by installing the optional `litellm` dependency group:

```bash
pip install "openai-agents[litellm]"
```

Once done, you can use [`LitellmModel`][agents.extensions.models.litellm_model.LitellmModel] in any agent.

## Example

This is a fully working example. When you run it, you'll be prompted for a model name and API key. For example, you could enter:

-   `openai/gpt-4.1` for the model, and your OpenAI API key
-   `anthropic/claude-3-5-sonnet-20240620` for the model, and your Anthropic API key
-   etc

For a full list of models supported in LiteLLM, see the [litellm providers docs](https://docs.litellm.ai/docs/providers).

```python
from __future__ import annotations

import asyncio

from agents import Agent, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel

@function_tool
def get_weather(city: str):
    print(f"[debug] getting weather for {city}")
    return f"The weather in {city} is sunny."


async def main(model: str, api_key: str):
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model=LitellmModel(model=model, api_key=api_key),
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(result.final_output)


if __name__ == "__main__":
    # First try to get model/api key from args
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, required=False)
    parser.add_argument("--api-key", type=str, required=False)
    args = parser.parse_args()

    model = args.model
    if not model:
        model = input("Enter a model name for Litellm: ")

    api_key = args.api_key
    if not api_key:
        api_key = input("Enter an API key for Litellm: ")

    asyncio.run(main(model, api_key))
```

## Tracking usage data

If you want LiteLLM responses to populate the Agents SDK usage metrics, pass `ModelSettings(include_usage=True)` when creating your agent.

```python
from agents import Agent, ModelSettings
from agents.extensions.models.litellm_model import LitellmModel

agent = Agent(
    name="Assistant",
    model=LitellmModel(model="your/model", api_key="..."),
    model_settings=ModelSettings(include_usage=True),
)
```

With `include_usage=True`, LiteLLM requests report token and request counts through `result.context_wrapper.usage` just like the built-in OpenAI models.

## Troubleshooting

If you see Pydantic serializer warnings from LiteLLM responses, enable a small compatibility patch by setting:

```bash
export OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true
```

This opt-in flag suppresses known LiteLLM serializer warnings while preserving normal behavior. Turn it off (unset or `false`) if you do not need it.

## Anthropic-specific features

When using Anthropic/Claude models with `LitellmModel`, several Anthropic-specific features are available:

!!! warning "Model Support"
    Advanced Anthropic features (cache control and deferred tools) are only supported on the following models:
    
    - `claude-sonnet-4-5-20250929`
    - `claude-haiku-4-5-20251001`
    - `claude-opus-4-5-20251101`
    
    Older Claude models (e.g., `claude-3-5-sonnet-20241022`) do not support these features. If you attempt to enable these features on an unsupported model, a warning will be logged and the features will be automatically disabled.

### Auto-detection

`LitellmModel` automatically detects Anthropic models (by checking if "anthropic" or "claude" is in the model name) and enables appropriate features for supported models.

### Prompt caching

Anthropic's [prompt caching](https://docs.anthropic.com/claude/docs/prompt-caching) is automatically enabled for supported Anthropic models. You can explicitly control this:

```python
from agents.extensions.models.litellm_model import LitellmModel

# Auto-detect (enabled for supported Anthropic models)
model = LitellmModel("claude-haiku-4-5-20251001")

# Explicitly enable (will be disabled with warning if model doesn't support it)
model = LitellmModel("claude-haiku-4-5-20251001", enable_cache_control=True)

# Explicitly disable
model = LitellmModel("claude-haiku-4-5-20251001", enable_cache_control=False)
```

!!! note
    Prompt caching is now a stable Anthropic feature and does not require beta headers.

### Deferred tool loading

For Anthropic's [advanced tool use features](https://docs.anthropic.com/en/docs/build-with-claude/tool-use), you can enable deferred tool loading on supported models:

```python
from agents.extensions.models.litellm_model import LitellmModel

# Only works on supported models (Claude 4.5 series)
model = LitellmModel(
    "claude-haiku-4-5-20251001",
    enable_deferred_tools=True,
)
```

When enabled:
- Tools marked with `_is_anthropic=True` and `_is_device_tool=True` attributes will be loaded using Anthropic's deferred mechanism
- The `anthropic-beta: advanced-tool-use-2025-11-20` header is automatically added
- If the model doesn't support this feature, it will be disabled with a warning

### Beta headers

For experimental Anthropic features, you can specify custom beta headers:

```python
from agents.extensions.models.litellm_model import LitellmModel

# Auto-add beta headers based on enabled features
model = LitellmModel(
    "claude-3-5-sonnet-20241022",
    enable_deferred_tools=True,  # Adds "advanced-tool-use-2025-11-20"
)

# Explicitly set custom beta headers
model = LitellmModel(
    "claude-3-5-sonnet-20241022",
    anthropic_beta_headers=["max-tokens-3-5-sonnet-2022-07-01", "other-feature-2026-01-01"],
)
```

Multiple beta features are joined with commas as per the [Anthropic API specification](https://docs.anthropic.com/en/api/beta-headers).