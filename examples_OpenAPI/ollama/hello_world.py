import asyncio
import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

# Import required modules
from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

async def main():
    # Create Ollama model settings
    ollama_settings = ModelSettings(
        provider="ollama",  # Specify Ollama as the provider
        ollama_base_url="http://localhost:11434",  # Ollama service address
        ollama_default_model="phi4:latest",  # Use phi4 model
        temperature=0.7  # Optional: control creativity
    )
    # Create run configuration
    run_config = RunConfig()
    # Set model provider
    run_config.model_provider = ModelProviderFactory.create_provider(ollama_settings)

    # Create Agent instance
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model_settings=ollama_settings  # Use Ollama settings
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
    # Expected output similar to:
    # Function calls itself,
    # Looping in smaller pieces,
    # Endless by design.

if __name__ == "__main__":
    # Check if Ollama service is running
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("Error: Ollama service returned non-200 status code. Please ensure Ollama service is running.")
            sys.exit(1)
    except Exception as e:
        print(f"Error: Cannot connect to Ollama service. Please ensure Ollama service is running.\n{str(e)}")
        print("\nIf you haven't installed Ollama, please download and install it from https://ollama.ai, then run 'ollama serve' to start the service")
        sys.exit(1)
        
    # Run main function
    asyncio.run(main())
