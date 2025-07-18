import asyncio
import os
import shutil
import subprocess
import time
from typing import Any

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServer, MCPServerStreamableHttp
from agents.model_settings import ModelSettings


async def get_instructions_from_prompt(mcp_server: MCPServer, prompt_name: str, **kwargs) -> str:
    """Get agent instructions by calling MCP prompt endpoint (user-controlled)"""
    print(f"Getting instructions from prompt: {prompt_name}")

    try:
        prompt_result = await mcp_server.get_prompt(prompt_name, kwargs)
        content = prompt_result.messages[0].content
        if hasattr(content, "text"):
            instructions = content.text
        else:
            instructions = str(content)
        print("Generated instructions")
        return instructions
    except Exception as e:
        print(f"Failed to get instructions: {e}")
        return f"You are a helpful assistant. Error: {e}"


async def demo_code_review(mcp_server: MCPServer):
    """Demo: Code review with user-selected prompt"""
    print("=== CODE REVIEW DEMO ===")

    # User explicitly selects prompt and parameters
    instructions = await get_instructions_from_prompt(
        mcp_server,
        "generate_code_review_instructions",
        focus="security vulnerabilities",
        language="python",
    )

    agent = Agent(
        name="Code Reviewer Agent",
        instructions=instructions,  # Instructions from MCP prompt
        model_settings=ModelSettings(tool_choice="auto"),
    )

    message = """Please review this code:

def process_user_input(user_input):
    command = f"echo {user_input}"
    os.system(command)
    return "Command executed"

"""

    print(f"Running: {message[:60]}...")
    result = await Runner.run(starting_agent=agent, input=message)
    print(result.final_output)
    print("\n" + "=" * 50 + "\n")


async def show_available_prompts(mcp_server: MCPServer):
    """Show available prompts for user selection"""
    print("=== AVAILABLE PROMPTS ===")

    prompts_result = await mcp_server.list_prompts()
    print("User can select from these prompts:")
    for i, prompt in enumerate(prompts_result.prompts, 1):
        print(f"  {i}. {prompt.name} - {prompt.description}")
    print()


async def main():
    async with MCPServerStreamableHttp(
        name="Simple Prompt Server",
        params={"url": "http://localhost:8000/mcp"},
    ) as server:
        trace_id = gen_trace_id()
        with trace(workflow_name="Simple Prompt Demo", trace_id=trace_id):
            print(f"Trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")

            await show_available_prompts(server)
            await demo_code_review(server)


if __name__ == "__main__":
    if not shutil.which("uv"):
        raise RuntimeError("uv is not installed")

    process: subprocess.Popen[Any] | None = None
    try:
        this_dir = os.path.dirname(os.path.abspath(__file__))
        server_file = os.path.join(this_dir, "server.py")

        print("Starting Simple Prompt Server...")
        process = subprocess.Popen(["uv", "run", server_file])
        time.sleep(3)
        print("Server started\n")
    except Exception as e:
        print(f"Error starting server: {e}")
        exit(1)

    try:
        asyncio.run(main())
    finally:
        if process:
            process.terminate()
            print("Server terminated.")
