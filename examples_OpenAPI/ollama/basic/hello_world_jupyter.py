import sys
import os
import asyncio

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))

from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

# Create Ollama model settings
ollama_settings = ModelSettings(
    provider="ollama",
    ollama_base_url="http://localhost:11434",
    ollama_default_model="llama3.2",
    temperature=0.7,
)
# Create runtime configuration
run_config = RunConfig(tracing_disabled=True)
# Set model provider
run_config.model_provider = ModelProviderFactory.create_provider(ollama_settings)

agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant",
    model_settings=ollama_settings,
)

print("Running with Ollama, please wait...")
# Intended for Jupyter notebooks where there's an existing event loop

async def main():
    result = await Runner.run(
        agent, "Write a haiku about recursion in programming.", run_config=run_config
    )  # type: ignore[top-level-await]  # noqa: F704
    print(result.final_output)


# Run the async function
asyncio.run(main())
