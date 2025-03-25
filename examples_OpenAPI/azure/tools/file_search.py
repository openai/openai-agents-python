import asyncio
import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, FileSearchTool, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

"""
This example demonstrates how to use the FileSearchTool with Azure OpenAI.
The agent searches in a vector store for information about Arrakis from Dune.
"""

async def main():
    # Create run configuration
    run_config = RunConfig()
    
    # Create provider directly, it will automatically read configuration from environment variables
    run_config.model_provider = AzureOpenAIProvider()
    
    # Create Azure OpenAI model settings
    azure_settings = ModelSettings(
        provider="azure_openai",  # Specify Azure OpenAI as the provider
        temperature=0.7  # Optional: control creativity
    )
    
    agent = Agent(
        name="File searcher",
        instructions="You are a helpful agent.",
        tools=[
            FileSearchTool(
                max_num_results=3,
                vector_store_ids=["vs_67bf88953f748191be42b462090e53e7"],
                include_search_results=True,
            )
        ],
        model_settings=azure_settings,
    )

    with trace("File search example"):
        result = await Runner.run(
            agent, 
            "Be concise, and tell me 1 sentence about Arrakis I might not know.", 
            run_config=run_config
        )
        print(result.final_output)
        """
        Arrakis, the desert planet in Frank Herbert's "Dune," was inspired by the scarcity of water
        as a metaphor for oil and other finite resources.
        """

        print("\n".join([str(out) for out in result.new_items]))
        """
        {"id":"...", "queries":["Arrakis"], "results":[...]}
        """


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI File Search Example")
    print("==============================")
    print("This example requires Azure OpenAI credentials and a valid vector store ID.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
