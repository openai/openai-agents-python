import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

# Create run configuration
run_config = RunConfig()

# Create provider directly, it will automatically read configuration from environment variables
run_config.model_provider = AzureOpenAIProvider()

# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.7  # Optional: control creativity
)

# Create Agent instance
agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant.",
    model_settings=azure_settings
)

# Use existing event loop in Jupyter notebook
# This file is suitable for running directly in a Jupyter notebook
result = await Runner.run(agent, "Write a haiku about Azure cloud services.", run_config=run_config)  # type: ignore[top-level-await]  # noqa: F704
print(result.final_output)

# Expected output similar to:
# Azure's vast data clouds,
# Computing power surges forth,
# World at your command.
