import asyncio
import os
from openai import AsyncOpenAI
from agents import Agent, Runner, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel
from examples.open_responses_built_in_tools import OpenResponsesBuiltInTools

"""
This program runs the brave search agent example for multiple iterations.
It allows selecting different model providers (groq, openai, claude) and runs the test
for a specified number of iterations (default: 10) with parallel execution (fixed: 5 concurrent runs).
"""

# Base URL for all providers
BASE_URL = os.getenv("OPEN_RESPONSES_URL") or "http://localhost:8080/v1"

# Model mapping for different providers
MODEL_MAPPING = {
    "groq": {
        "name": "qwen-2.5-32b",
        "api_key_env": "GROQ_API_KEY",
        "headers": lambda api_key: {
            "Authorization": f"Bearer {api_key}"
        }
    },
    "openai": {
        "name": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
        "headers": lambda api_key: {
            "Authorization": f"Bearer {api_key}",
            "x-model-provider": "openai"
        }
    },
    "claude": {
        "name": "claude-3-7-sonnet-20250219",
        "api_key_env": "CLAUDE_API_KEY",
        "headers": lambda api_key: {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "x-model-provider": "claude"
        }
    }
}

def get_client_for_provider(provider):
    """Create an OpenAI client configured for the specified provider."""
    provider_config = MODEL_MAPPING.get(provider)
    if not provider_config:
        raise ValueError(f"Unknown provider: {provider}. Available providers: {', '.join(MODEL_MAPPING.keys())}")
    
    api_key_env = provider_config["api_key_env"]
    api_key = os.getenv(api_key_env) or ""
    
    if not api_key:
        raise ValueError(f"API key for {provider} not found. Please set {api_key_env} environment variable.")
    
    custom_headers = provider_config["headers"](api_key)
    
    return AsyncOpenAI(
        base_url=BASE_URL,
        api_key=api_key,
        default_headers=custom_headers
    )

async def run_single_iteration(iteration, search_agent, input_text):
    """Run a single iteration of the brave search test."""
    try:
        result = await Runner.run(search_agent, input=input_text)
        print(f"Iteration {iteration+1} completed with result: {result.final_output[:150]}...")  # Truncate long results
        return result.final_output
    except Exception as e:
        error_message = f"ERROR: {e}"
        print(f"Iteration {iteration+1} failed with error: {error_message}")
        return error_message

async def run_brave_search_test(provider="groq", num_iterations=10, concurrency=5, input_text="Where did NVIDIA GTC happen in 2025 and what were the major announcements?"):
    """Run the brave search test for the specified number of iterations with parallel execution."""
    client = get_client_for_provider(provider)
    model_name = MODEL_MAPPING[provider]["name"]
    
    # Disable tracing to reduce output noise during load testing
    set_tracing_disabled(disabled=True)
    
    # Create the brave search tool
    brave_search_tool = OpenResponsesBuiltInTools(tool_name="brave_web_search")
    
    # Create the search agent
    search_agent = Agent(
        name="Brave Search Agent",
        instructions=(
            "You are a research assistant that uses Brave web search. "
            "When given a query, perform a web search using Brave and provide a concise summary."
        ),
        tools=[brave_search_tool],
        model=OpenAIResponsesModel(model=model_name, openai_client=client)
    )
    
    print(f"\nRunning {num_iterations} iterations with concurrency of {concurrency}...\n")
    
    # Create tasks for all iterations
    tasks = []
    for i in range(num_iterations):
        tasks.append(run_single_iteration(i, search_agent, input_text))
    
    # Run tasks with semaphore to limit concurrency
    semaphore = asyncio.Semaphore(concurrency)
    
    async def run_with_semaphore(task):
        async with semaphore:
            return await task
    
    # Execute tasks with limited concurrency
    results = await asyncio.gather(*[run_with_semaphore(task) for task in tasks])
    
    # Print summary
    print("\n===== SUMMARY =====")
    print(f"Total iterations: {num_iterations}")
    print(f"Successful responses: {len([r for r in results if not r.startswith('ERROR')])}")
    print(f"Failed responses: {len([r for r in results if r.startswith('ERROR')])}")
    
    return results

async def main():
    # Ask for model provider
    print("Available model providers: groq, openai, claude")
    provider = input("Enter model provider (default: groq): ").lower() or "groq"
    
    # Ask for number of iterations
    iterations_input = input("Enter number of iterations (default: 10): ")
    iterations = int(iterations_input) if iterations_input.strip() else 10
    
    # Hard-coded concurrency level of 5
    concurrency = 5
    
    # Ask for input text
    default_query = "Where did NVIDIA GTC happen in 2025 and what were the major announcements?"
    input_text = input(f"Enter search query (default: '{default_query}'): ") or default_query
    
    # Run the test
    await run_brave_search_test(provider, iterations, concurrency, input_text)

if __name__ == "__main__":
    # Apply common patches for OpenResponsesBuiltInTools
    try:
        from examples.open_responses import common_patches
    except ImportError:
        print("Warning: Could not import common_patches. Brave search may not work correctly.")
    
    asyncio.run(main()) 