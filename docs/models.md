# Models

The Agents SDK comes with out-of-the-box support for OpenAI models in two flavors:

-   **Recommended**: the [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel], which calls OpenAI APIs using the new [Responses API](https://platform.openai.com/docs/api-reference/responses).
-   The [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel], which calls OpenAI APIs using the [Chat Completions API](https://platform.openai.com/docs/api-reference/chat).

## Mixing and matching models

Within a single workflow, you may want to use different models for each agent. For example, you could use a smaller, faster model for triage, while using a larger, more capable model for complex tasks. When configuring an [`Agent`][agents.Agent], you can select a specific model by either:

1. Passing the name of an OpenAI model.
2. Passing any model name + a [`ModelProvider`][agents.models.interface.ModelProvider] that can map that name to a Model instance.
3. Directly providing a [`Model`][agents.models.interface.Model] implementation.

!!!note

    While our SDK supports both the [`OpenAIResponsesModel`][agents.models.openai_responses.OpenAIResponsesModel] and the [`OpenAIChatCompletionsModel`][agents.models.openai_chatcompletions.OpenAIChatCompletionsModel] shapes, we recommend using a single model shape for each workflow because the two shapes support a different set of features and tools. If your workflow requires mixing and matching model shapes, make sure that all the features you're using are available on both.

```python
from agents import Agent, Runner, AsyncOpenAI, OpenAIChatCompletionsModel
import asyncio

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model="o3-mini", # (1)!
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model=OpenAIChatCompletionsModel( # (2)!
        model="gpt-4o",
        openai_client=AsyncOpenAI()
    ),
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    model="gpt-3.5-turbo",
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
```

1.  Sets the name of an OpenAI model directly.
2.  Provides a [`Model`][agents.models.interface.Model] implementation.

## Using other LLM providers

Many providers also support the OpenAI API format, which means you can pass a `base_url` to the existing OpenAI model implementations and use them easily. `ModelSettings` is used to configure tuning parameters (e.g., temperature, top_p) for the model you select.

```python
external_client = AsyncOpenAI(
    api_key="EXTERNAL_API_KEY",
    base_url="https://api.external.com/v1/",
)

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model=OpenAIChatCompletionsModel(
        model="EXTERNAL_MODEL_NAME",
        openai_client=external_client,
    ),
    model_settings=ModelSettings(temperature=0.5),
)
```

## Using LiteLLM Provider

The SDK includes built-in support for [LiteLLM](https://docs.litellm.ai/), a unified interface for multiple LLM providers. LiteLLM provides a proxy server that exposes an OpenAI-compatible API for various LLM providers including OpenAI, Anthropic, Azure, AWS Bedrock, Google, and more.

### Basic Usage

```python
from agents import Agent, Runner, LiteLLMProvider, RunConfig
import asyncio

# Create a LiteLLM provider
provider = LiteLLMProvider(
    api_key="your-litellm-api-key",  # or set LITELLM_API_KEY env var
    base_url="http://localhost:8000", # or set LITELLM_API_BASE env var
)

# Create an agent using a specific model
agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant.",
    model="claude-3",  # Will be routed to Anthropic by the provider
)

# Create a run configuration with the provider
run_config = RunConfig(model_provider=provider)

async def main():
    result = await Runner.run(
        agent, 
        input="Hello!",
        run_config=run_config  # Pass the provider through run_config
    )
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

### Environment Variables

The LiteLLM provider supports configuration through environment variables:

```bash
# LiteLLM configuration
export LITELLM_API_KEY="your-litellm-api-key"
export LITELLM_API_BASE="http://localhost:8000"
export LITELLM_MODEL="gpt-4"  # Default model (optional)

# Provider-specific keys (examples)
export OPENAI_API_KEY="sk-..."
export ANTHROPIC_API_KEY="sk-ant-..."
export AZURE_API_KEY="..."
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
```

### Model Routing

The provider automatically routes model names to their appropriate providers:

```python
# Create the LiteLLM provider
provider = LiteLLMProvider(
    api_key="your-litellm-api-key",
    base_url="http://localhost:8000"
)

# Create a run configuration with the provider
run_config = RunConfig(model_provider=provider)

# Models are automatically routed based on their names
openai_agent = Agent(
    name="OpenAI Agent",
    instructions="Using GPT-4",
    model="gpt-4",  # Will be routed to OpenAI
)

anthropic_agent = Agent(
    name="Anthropic Agent",
    instructions="Using Claude",
    model="claude-3",  # Will be routed to Anthropic
)

azure_agent = Agent(
    name="Azure Agent",
    instructions="Using Azure OpenAI",
    model="azure/gpt-4",  # Explicitly using Azure
)

# Run any of the agents with the provider
result = await Runner.run(openai_agent, input="Hello!", run_config=run_config)
```

You can also explicitly specify providers using prefixes:

- `openai/` - OpenAI models
- `anthropic/` - Anthropic models
- `azure/` - Azure OpenAI models
- `aws/` - AWS Bedrock models
- `cohere/` - Cohere models
- `replicate/` - Replicate models
- `huggingface/` - Hugging Face models
- `mistral/` - Mistral AI models
- `gemini/` - Google Gemini models
- `groq/` - Groq models

### Advanced Configuration

The provider supports additional configuration options:

```python
provider = LiteLLMProvider(
    api_key="your-litellm-api-key",
    base_url="http://localhost:8000",
    model_name="gpt-4",  # Default model
    use_responses=True,  # Use OpenAI Responses API format
    extra_headers={      # Additional headers
        "x-custom-header": "value"
    },
    drop_params=True,    # Drop unsupported params for specific models
)
```

### Using Multiple Providers

You can use different providers for different agents in your workflow:

```python
from agents import Agent, Runner, OpenAIProvider, LiteLLMProvider, RunConfig
import asyncio

# OpenAI provider for direct OpenAI API access
openai_provider = OpenAIProvider()

# LiteLLM provider for other models
litellm_provider = LiteLLMProvider(
    api_key="your-litellm-api-key",
    base_url="http://localhost:8000"
)

# Create agents with different model names
triage_agent = Agent(
    name="Triage",
    instructions="Route requests to appropriate agents",
    model="gpt-3.5-turbo",  # Will be routed by the provider
)

analysis_agent = Agent(
    name="Analysis",
    instructions="Perform detailed analysis",
    model="claude-3",  # Will be routed by the provider
)

# Run with OpenAI provider
openai_config = RunConfig(model_provider=openai_provider)
result_triage = await Runner.run(
    triage_agent, 
    input="Analyze this data",
    run_config=openai_config
)

# Run with LiteLLM provider
litellm_config = RunConfig(model_provider=litellm_provider)
result_analysis = await Runner.run(
    analysis_agent,
    input="Perform detailed analysis of this data",
    run_config=litellm_config
)
```

The LiteLLM provider makes it easy to use multiple LLM providers while maintaining a consistent interface and the full feature set of the Agents SDK including handoffs, tools, and tracing.
