"""
Example demonstrating how to use the reasoning content feature with the Runner API.

This example shows how to extract and use reasoning content from responses when using
the Runner API, which is the most common way users interact with the Agents library.

To run this example, you need to:
1. Set your OPENAI_API_KEY environment variable
2. Use a model that supports reasoning content (e.g., deepseek-reasoner)
"""

import os
import asyncio

from agents import Agent, Runner, ModelSettings, trace
from agents.items import ReasoningItem

# Replace this with a model that supports reasoning content (e.g., deepseek-reasoner)
# For demonstration purposes, we'll use a placeholder model name
MODEL_NAME = "deepseek-reasoner"

async def main():
    print(f"Using model: {MODEL_NAME}")
    
    # Create an agent with a model that supports reasoning content
    agent = Agent(
        name="Reasoning Agent",
        instructions="You are a helpful assistant that explains your reasoning step by step.",
        model=MODEL_NAME,
    )
    
    # Example 1: Non-streaming response
    with trace("Reasoning Content - Non-streaming"):
        print("\n=== Example 1: Non-streaming response ===")
        result = await Runner.run(
            agent, 
            "What is the square root of 841? Please explain your reasoning."
        )
        
        # Extract reasoning content from the result items
        reasoning_content = None
        for item in result.items:
            if isinstance(item, ReasoningItem):
                reasoning_content = item.raw_item.content
                break
        
        print("\nReasoning Content:")
        print(reasoning_content or "No reasoning content provided")
        
        print("\nFinal Output:")
        print(result.final_output)
    
    # Example 2: Streaming response
    with trace("Reasoning Content - Streaming"):
        print("\n=== Example 2: Streaming response ===")
        print("\nStreaming response:")
        
        # Buffers to collect reasoning and regular content
        reasoning_buffer = ""
        content_buffer = ""
        
        async for event in Runner.run_streamed(
            agent, 
            "What is 15 × 27? Please explain your reasoning."
        ):
            if isinstance(event, ReasoningItem):
                # This is reasoning content
                reasoning_buffer += event.raw_item.content
                print(f"\033[33m{event.raw_item.content}\033[0m", end="", flush=True)  # Yellow for reasoning
            elif hasattr(event, "text"):
                # This is regular content
                content_buffer += event.text
                print(f"\033[32m{event.text}\033[0m", end="", flush=True)  # Green for regular content
        
        print("\n\nCollected Reasoning Content:")
        print(reasoning_buffer)
        
        print("\nCollected Final Answer:")
        print(content_buffer)

if __name__ == "__main__":
    asyncio.run(main()) 