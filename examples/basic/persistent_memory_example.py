import asyncio
import os
from pathlib import Path
import sys

# This is a common way to enable imports from the parent directory (e.g., 'src')
# when running examples located in a subdirectory.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from src.agents import Agent, Runner # Assuming Agent and Runner are in src/agents/__init__.py or directly accessible
from src.agents.memory import FileStorageMemory

async def main():
    """
    Demonstrates the usage of FileStorageMemory to give an agent persistent memory
    across multiple interactions.
    """
    # Define a file path for the memory
    # Using Path for better path management
    memory_file_path = Path("agent_memory.json")

    print(f"--- Demonstrating persistent memory with FileStorageMemory ---")
    print(f"Memory file will be stored at: {memory_file_path.resolve()}\n")

    try:
        # 1. Initialize FileStorageMemory
        # The agent's conversation history will be stored in 'agent_memory.json'
        # load() is called automatically on init if the file exists.
        persistent_memory = FileStorageMemory(file_path=memory_file_path)

        # 2. Define a simple agent
        # This agent is instructed to be helpful and remember previous interactions.
        memory_agent = Agent(
            name="MemoryBot",
            instructions="You are a helpful assistant that remembers previous interactions. Be concise and friendly.",
            memory=persistent_memory,  # Assign the FileStorageMemory instance
            # model="gpt-3.5-turbo" # Or your preferred model, ensure OPENAI_API_KEY is set
        )

        # 3. First interaction: Provide information to the agent
        print("--- First interaction ---")
        user_input_1 = "My favorite color is blue and my name is Jules."
        print(f"User: {user_input_1}")

        # Runner.run_sync is a helper for simple synchronous execution.
        # For production/async code, use `await Runner.run(...)`
        response_1 = Runner.run_sync(memory_agent, user_input_1)
        assistant_response_1 = response_1.final_output # Extracts the text from the last message
        print(f"MemoryBot: {assistant_response_1}\n")

        # At this point, the conversation ("My name is Jules" and the bot's response)
        # should be saved to 'agent_memory.json' because FileStorageMemory.add() calls save().

        # 4. Second interaction: Ask the agent to recall information
        print("--- Second interaction (testing recall) ---")
        user_input_2 = "What is my name and what is my favorite color?"
        print(f"User: {user_input_2}")

        # Run the agent again. It should use the memory from 'agent_memory.json'
        # because Agent.__post_init__ calls memory.load() and subsequent turns also load.
        response_2 = Runner.run_sync(memory_agent, user_input_2)
        assistant_response_2 = response_2.final_output
        print(f"MemoryBot: {assistant_response_2}")

        if "Jules" in str(assistant_response_2) and "blue" in str(assistant_response_2).lower():
            print("SUCCESS: Agent remembered the name 'Jules' and color 'blue'.\n")
        elif "Jules" in str(assistant_response_2):
            print("PARTIAL SUCCESS: Agent remembered the name 'Jules' but not color 'blue'.\n")
        elif "blue" in str(assistant_response_2).lower():
            print("PARTIAL SUCCESS: Agent remembered color 'blue' but not name 'Jules'.\n")
        else:
            print("FAILURE: Agent did NOT seem to remember the name 'Jules' or color 'blue'. "
                  "This might happen if the LLM fails to follow instructions, memory isn't working, "
                  "or the response format was unexpected.\n")


        # 5. Clear the memory
        print("--- Clearing agent memory ---")
        if memory_agent.memory: # Check if memory object exists
            memory_agent.memory.clear() # Clears messages in memory AND saves the empty list to the file.
            print("Memory cleared (agent_memory.json should now be an empty list).\n")
        else:
            print("Memory object not found on agent.\n")

        # 6. Third interaction: Test if the memory is cleared
        print("--- Third interaction (testing after clear) ---")
        user_input_3 = "What is my name and favorite color?"
        print(f"User: {user_input_3}")

        # Run the agent again. It should not remember the name or color.
        # The memory file ('agent_memory.json') was updated by clear()
        # and the agent will load this cleared state.
        response_3 = Runner.run_sync(memory_agent, user_input_3)
        assistant_response_3 = response_3.final_output
        print(f"MemoryBot: {assistant_response_3}")

        if "Jules" not in str(assistant_response_3) and "blue" not in str(assistant_response_3).lower():
            print("SUCCESS: Agent correctly does not remember the name or color after memory clear.\n")
        else:
            print("FAILURE: Agent still remembered information after clear. This is unexpected.\n")

    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 7. Clean up the created memory file
        # This is good practice for examples to avoid leaving files behind.
        if memory_file_path.exists():
            try:
                os.remove(memory_file_path)
                print(f"Cleaned up memory file: {memory_file_path}")
            except Exception as e:
                print(f"Error cleaning up memory file {memory_file_path}: {e}")
        else:
            # This case might occur if the script failed before creating the file.
            print(f"Memory file {memory_file_path} not found, no cleanup needed or file was not created due to an error.")

if __name__ == "__main__":
    # Ensure OPENAI_API_KEY is set in your environment variables for the LLM to work.
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable not set.")
        print("Please set it to run this example: export OPENAI_API_KEY='your_key_here'")
    else:
        asyncio.run(main())

# To run this example:
# 0. Make sure you are in an environment where the google-ai-agents SDK is accessible.
#    If running from the root of the SDK cloned repository:
#    You might need to add the SDK's root or 'src' to your PYTHONPATH.
#    The example includes a `sys.path.append` to help with this when run directly.
#    Alternatively, install the SDK package if available.
# 1. Set up your OpenAI API key (or any other model provider key you configure the agent with):
#    export OPENAI_API_KEY="your_api_key_here"
# 2. Navigate to the 'google-ai-agents-python' directory (or where the example is located).
# 3. Execute the script from the 'google-ai-agents-python' root directory:
#    python examples/basic/persistent_memory_example.py
#
# Note:
# - 'agent_memory.json' will be created in the directory where you run the script.
# - The example includes cleanup of this file by default in the `finally` block.
# - The LLM's responses can vary. The key observation is whether it
#   correctly recalls information in the second interaction and forgets it in the third.
# - `FileStorageMemory.add()` and `FileStorageMemory.clear()` automatically call `save()`.
# - `FileStorageMemory.load()` is called upon initialization of `FileStorageMemory`
#   and also by the `Agent` during its initialization (`__post_init__`) and by the `Runner` logic
#   at the start of an agent's turn (`if current_turn == 1: memory.load()`).
#
# This example uses `Runner.run_sync` which is a synchronous wrapper around `Runner.run`.
# In an asynchronous application, you would use `await Runner.run(...)`.
```
