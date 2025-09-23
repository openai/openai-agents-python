import asyncio
import json
from dataclasses import dataclass
from typing import Annotated, Optional

from openai.types.responses import ResponseFunctionCallArgumentsDeltaEvent

from agents import Agent, Runner, function_tool


@dataclass
class FunctionCallInfo:
    name: str
    arguments: str = ""


@function_tool
def write_file(filename: Annotated[str, "Name of the file"], content: str) -> str:
    """Write content to a file."""
    return f"File {filename} written successfully"


@function_tool
def create_config(
    project_name: Annotated[str, "Project name"],
    version: Annotated[str, "Project version"],
    dependencies: Annotated[Optional[list[str]], "Dependencies (list of packages)"],
) -> str:
    """Generate a project configuration file."""
    return f"Config for {project_name} v{version} created"


async def main():
    """
    Demonstrates real-time streaming of function call arguments.

    Function arguments are streamed incrementally as they are generated,
    providing immediate feedback during parameter generation.
    """
    agent = Agent(
        name="CodeGenerator",
        instructions="""You are a helpful coding assistant. Use the provided tools to create files and configurations. 
        **Important: You must use the tools one at a time. Complete one function call before starting another.**""",
        tools=[write_file, create_config],
    )

    print("🚀 Function Call Arguments Streaming Demo")

    result = Runner.run_streamed(
        agent,
        input="Create a Python web project called 'my-app' with FastAPI. Version 1.0.0, dependencies: fastapi, uvicorn",
    )

    function_calls: dict[str, FunctionCallInfo] = {}
    current_active_call_id: Optional[str] = None

    async for event in result.stream_events():
        if getattr(event, "type", None) == "raw_response_event":
            data = getattr(event, "data", None)
            item_type = getattr(data, "type", None)

            # Function call started
            if item_type == "response.output_item.added":
                item = getattr(data, "item", None)
                if getattr(item, "type", None) == "function_call":
                    function_name = getattr(item, "name", "unknown")
                    call_id = getattr(item, "call_id", "unknown")

                    function_calls[call_id] = FunctionCallInfo(name=function_name)
                    current_active_call_id = call_id
                    print(f"\n📞 Function call streaming started: {function_name}()")
                    print("📝 Arguments building...")

            # Real-time argument streaming
            elif isinstance(data, ResponseFunctionCallArgumentsDeltaEvent):
                if current_active_call_id and current_active_call_id in function_calls:
                    function_calls[current_active_call_id].arguments += data.delta
                    print(data.delta, end="", flush=True)

            # Function call completed
            elif item_type == "response.output_item.done":
                item = getattr(data, "item", None)
                call_id = getattr(item, "call_id", None)
                if call_id and call_id in function_calls:
                    function_info = function_calls[call_id]
                    print(f"\n✅ Function call streaming completed: {function_info.name}")
                    # try parse JSON args
                    try:
                        parsed = (
                            json.loads(function_info.arguments)
                            if function_info.arguments.strip()
                            else {}
                        )
                        print("Parsed args:", parsed)
                    except json.JSONDecodeError:
                        print("Args (raw):", function_info.arguments)
                    if current_active_call_id == call_id:
                        current_active_call_id = None

    print("Summary of all function calls:")
    for call_id, info in function_calls.items():
        print(f"  - #{call_id}: {info.name}({info.arguments})")

    print(f"\nResult: {getattr(result, 'final_output', None)}")


if __name__ == "__main__":
    asyncio.run(main())
