import asyncio
import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

from .manager import FinancialResearchManager


# Entrypoint for the financial research agent example using Azure OpenAI.
# Run this as `python -m examples_OpenAPI.azure.financial_research_agent.main` and enter a
# financial research query, for example:
# "Write up an analysis of Apple Inc.'s most recent quarter."
async def main() -> None:
    # Create run configuration
    run_config = RunConfig()
    
    # Set up Azure OpenAI provider
    run_config.model_provider = AzureOpenAIProvider()
    
    # Get financial research query from user
    query = input("Enter a financial research query: ")
    
    # Create and run the financial research manager with Azure configuration
    mgr = FinancialResearchManager(run_config)
    await mgr.run(query)


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI Financial Research Agent Example")
    print("=========================================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
