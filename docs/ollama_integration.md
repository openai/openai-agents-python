# Ollama Integration

This documentation covers how to use the Ollama integration with the Agents SDK to run local LLMs with your agent applications.

## Overview

Ollama is an open-source framework that makes it easy to run large language models locally. This integration allows you to use local Ollama models with the OpenAI Agents SDK, providing an alternative to cloud-based LLMs when needed.

Key benefits:
- **Privacy**: All data stays on your machine
- **Cost**: No usage fees for inference
- **Flexibility**: Use various open-source models
- **OpenAI API Compatible**: Uses Ollama's OpenAI-compatible API endpoints

## Setup Requirements

1. Install Ollama from [ollama.ai](https://ollama.ai)
2. Start the Ollama service by running `ollama serve`
3. Pull your desired model (e.g., `ollama pull phi4:latest`)

## Configuration

The Ollama provider is configured using `ModelSettings`:

```python
from src.agents.model_settings import ModelSettings

# Create Ollama model settings
ollama_settings = ModelSettings(
    provider="ollama",  # Specify Ollama as the provider
    ollama_base_url="http://localhost:11434",  # Ollama service address
    ollama_default_model="llama3.2",  # Model to use
    temperature=0.7  # Optional: control creativity
)
```

## Using with Agents

To create an agent that uses Ollama:

```python
from src.agents import Agent
from src.agents.model_settings import ModelSettings

# Create model settings
ollama_settings = ModelSettings(
    provider="ollama",
    ollama_base_url="http://localhost:11434",  
    ollama_default_model="llama3.2"
)

# Create an agent with Ollama
agent = Agent(
    name="OllamaAssistant",
    instructions="You are a helpful assistant.",
    model_settings=ollama_settings
)
```

## Running Agents with Ollama

To run an agent with the Ollama provider:

```python
from src.agents import Runner
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

# Create a run configuration with Ollama provider
run_config = RunConfig()
run_config.model_provider = ModelProviderFactory.create_provider(ollama_settings)

# Run the agent with the custom provider
result = await Runner.run(agent, "Tell me a joke", run_config=run_config)
print(result.final_output)
```

## Complete Example

Here's a complete example of using the Ollama integration:

```python
import asyncio
import os, sys

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

async def main():
    # Create Ollama model settings
    ollama_settings = ModelSettings(
        provider="ollama", 
        ollama_base_url="http://localhost:11434", 
        ollama_default_model="llama3.2", 
        temperature=0.7
    )
    
    # Create run configuration
    run_config = RunConfig()
    run_config.model_provider = ModelProviderFactory.create_provider(ollama_settings)

    # Create Agent instance
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model_settings=ollama_settings
    )
    
    # Run Agent
    print("Running Agent, please wait...")
    result = await Runner.run(
        agent, 
        "Tell me about recursion in programming.", 
        run_config=run_config
    )
    
    # Print results
    print("\nResult:")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

## Compatibility Notes

The Ollama integration uses Ollama's OpenAI-compatible API endpoints, which implement the Chat Completions API format. This allows for a smooth transition between cloud-based OpenAI models and local Ollama models.

Current limitations:
- Streaming responses are not yet implemented
- Tool calls may have limited support depending on the model
- Not all models support structured outputs or JSON mode

## Supported Models

You can use any model available in Ollama, including:
- Llama 3
- Mistral
- Phi4
- Gemma
- And many more

To see available models, run `ollama list` in your terminal.

## Troubleshooting

If you encounter issues:

1. Ensure Ollama service is running (`ollama serve`)
2. Check that you've pulled the model you're trying to use
3. Verify the URL and port in `ollama_base_url` (default: `http://localhost:11434`)
4. Check model compatibility - not all models support all features
