from __future__ import annotations as _annotations

import os
import asyncio

from pydantic import BaseModel
try:
    from mem0 import AsyncMemoryClient
except ImportError:
    raise ImportError("mem0 is not installed. Please install it using 'pip install mem0ai'.")

from agents import (
    Agent,
    ItemHelpers,
    MessageOutputItem,
    RunContextWrapper,
    Runner,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
    function_tool,
)

### CONTEXT

class Mem0Context(BaseModel):
    user_id: str | None = None

### CLIENT

client = AsyncMemoryClient(api_key=os.getenv("MEM0_API_KEY")) ## Obtain API Key from https://app.mem0.ai/dashboard/api-keys

### TOOLS

@function_tool
async def add_to_memory(
    context: RunContextWrapper[Mem0Context],
    content: str,
) -> str:
    """
    Add a message to Mem0

    Args:
        content: The content to store in memory.
    """
    messages = [{"role": "user", "content": content}]
    user_id = context.context.user_id or "default_user"
    await client.add(messages, user_id=user_id)
    return f"Stored message: {content}"

@function_tool
async def search_memory(
    context: RunContextWrapper[Mem0Context],
    query: str,
) -> str:
    """
    Search for memories in Mem0

    Args:
        query: The search query.
    """
    user_id = context.context.user_id or "default_user"
    memories = await client.search(query, user_id=user_id, output_format="v1.1")
    results = '\n'.join([result["memory"] for result in memories["results"]])
    return str(results)

@function_tool
async def get_all_memory(
    context: RunContextWrapper[Mem0Context],
) -> str:
    """Retrieve all memories from Mem0"""
    user_id = context.context.user_id or "default_user"
    memories = await client.get_all(user_id=user_id, output_format="v1.1")
    results = '\n'.join([result["memory"] for result in memories["results"]])
    return str(results)

### AGENTS

memory_agent = Agent[Mem0Context](
    name="Memory Assistant",
    instructions="""You are a helpful assistant with memory capabilities. You can:
    1. Store new information using add_to_memory
    2. Search existing information using search_memory
    3. Retrieve all stored information using get_all_memory

    When users ask questions:
    - If they want to store information, use add_to_memory
    - If they're searching for specific information, use search_memory
    - If they want to see everything stored, use get_all_memory""",
    tools=[add_to_memory, search_memory, get_all_memory],
)

### RUN

async def main():
    current_agent: Agent[Mem0Context] = memory_agent
    input_items: list[TResponseInputItem] = []
    context = Mem0Context()

    while True:
        user_input = input("Enter your message (or 'quit' to exit): ")
        if user_input.lower() == 'quit':
            break

        input_items.append({"content": user_input, "role": "user"})
        result = await Runner.run(current_agent, input_items, context=context)

        for new_item in result.new_items:
            agent_name = new_item.agent.name
            if isinstance(new_item, MessageOutputItem):
                print(f"{agent_name}: {ItemHelpers.text_message_output(new_item)}")
            elif isinstance(new_item, ToolCallItem):
                print(f"{agent_name}: Calling a tool")
            elif isinstance(new_item, ToolCallOutputItem):
                print(f"{agent_name}: Tool call output: {new_item.output}")
            else:
                print(f"{agent_name}: Skipping item: {new_item.__class__.__name__}")

        input_items = result.to_input_list()

if __name__ == "__main__":
    asyncio.run(main())