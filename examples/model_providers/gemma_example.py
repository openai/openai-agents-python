"""
Example: Using Local Gemma Model with OpenAI Agents SDK

This example demonstrates how to use a locally-hosted Gemma model
instead of OpenAI's API, enabling offline and privacy-preserving
agent execution.

Setup:
    1. Set HF_TOKEN environment variable
    2. Install dependencies: pip install transformers torch accelerate bitsandbytes
    3. Run: python gemma_example.py

The first run will download the Gemma model (~5GB).
"""

import os
import asyncio

from agents import Agent, Runner

# Import our local Gemma provider
from gemma_local_provider import create_gemma_provider


async def main():
    """Run agent with local Gemma model"""
    
    # Check HF_TOKEN
    if not os.getenv("HF_TOKEN"):
        print("Error: HF_TOKEN environment variable not set")
        print("Get your token from: https://huggingface.co/settings/tokens")
        print("Then run: export HF_TOKEN=your_token")
        return
    
    print("="*70)
    print(" OpenAI Agents SDK + Local Gemma Model")
    print("="*70)
    print()
    print("This example demonstrates using a local Gemma 2B model")
    print("with the OpenAI Agents SDK for completely offline operation.")
    print()
    
    # Create Gemma provider
    print("[1/3] Initializing Gemma local provider...")
    provider = create_gemma_provider(
        model_name="google/gemma-2b-it",
        use_4bit=True,  # Use 4-bit quantization for efficiency
    )
    print("      Provider ready")
    print()
    
    # Create agent with local model
    print("[2/3] Creating agent with local Gemma model...")
    agent = Agent(
        name="LocalAssistant",
        instructions="You are a helpful assistant. Answer concisely.",
        model_provider=provider,
    )
    print("      Agent created")
    print()
    
    # Run agent
    print("[3/3] Running agent...")
    print()
    
    queries = [
        "What is machine learning?",
        "Explain the concept of recursion in programming.",
    ]
    
    for query in queries:
        print(f"User: {query}")
        print()
        
        result = await Runner.run(agent, query)
        
        print(f"Gemma: {result.final_output}")
        print()
        print("-" * 70)
        print()
    
    print("="*70)
    print(" Example Complete!")
    print("="*70)
    print()
    print("Key benefits of local models:")
    print("  ✓ Complete privacy - no data sent to cloud")
    print("  ✓ No API costs")
    print("  ✓ Works offline")
    print("  ✓ Full control over the model")
    print()
    print("Note: Gemma 2B is smaller than GPT-4, so responses may be less")
    print("      sophisticated, but it's completely private and free!")
    print()


if __name__ == "__main__":
    asyncio.run(main())
