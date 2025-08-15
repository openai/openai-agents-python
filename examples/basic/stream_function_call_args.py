import asyncio
from typing import Any

from openai.types.responses import ResponseFunctionCallArgumentsDeltaEvent

from agents import Agent, Runner, function_tool


@function_tool
def write_file(filename: str, content: str) -> str:
    """Write content to a file."""
    return f"File {filename} written successfully"


@function_tool
def create_config(project_name: str, version: str, dependencies: list[str]) -> str:
    """Create a configuration file for a project."""
    return f"Config for {project_name} v{version} created"


async def main():
    """
    Demonstrates real-time streaming of function call arguments.

    Function arguments are streamed incrementally as they are generated,
    providing immediate feedback during parameter generation.
    """
    agent = Agent(
        name="CodeGenerator",
        instructions="You are a helpful coding assistant. Use the provided tools to create files and configurations.",
        tools=[write_file, create_config],
    )

    print("🚀 Function Call Arguments Streaming Demo")

    result = Runner.run_streamed(
        agent,
        input="Create a Python web project called 'my-app' with FastAPI. Version 1.0.0, dependencies: fastapi, uvicorn"
    )

    # Track function calls for detailed output
    function_calls: dict[Any, dict[str, Any]] = {}  # call_id -> {name, arguments}
    current_active_call_id = None

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            # Function call started
            if event.data.type == "response.output_item.added":
                if getattr(event.data.item, "type", None) == "function_call":
                    function_name = getattr(event.data.item, "name", "unknown")
                    call_id = getattr(event.data.item, "call_id", "unknown")

                    function_calls[call_id] = {
                        'name': function_name,
                        'arguments': ""
                    }
                    current_active_call_id = call_id
                    print(f"\n📞 Function call streaming started: {function_name}()")
                    print("📝 Arguments building...")

            # Real-time argument streaming
            elif isinstance(event.data, ResponseFunctionCallArgumentsDeltaEvent):
                if current_active_call_id and current_active_call_id in function_calls:
                    function_calls[current_active_call_id]['arguments'] += event.data.delta
                    print(event.data.delta, end="", flush=True)

            # Function call completed
            elif event.data.type == "response.output_item.done":
                if hasattr(event.data.item, 'call_id'):
                    call_id = getattr(event.data.item, "call_id", "unknown")
                    if call_id in function_calls:
                        function_info = function_calls[call_id]
                        print(f"\n✅ Function call streaming completed: {function_info['name']}")
                        print()
                        if current_active_call_id == call_id:
                            current_active_call_id = None

    print("Summary of all function calls:")
    for call_id, info in function_calls.items():
        print(f"  - #{call_id}: {info['name']}({info['arguments']})")

    print(f"\nResult: {result.final_output}")


if __name__ == "__main__":
    asyncio.run(main())
