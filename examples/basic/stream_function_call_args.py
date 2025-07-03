import asyncio
import os
from typing import Any

from openai import AsyncOpenAI
from openai.types.responses import ResponseFunctionCallArgumentsDeltaEvent

from agents import Agent, OpenAIChatCompletionsModel, Runner, function_tool, set_tracing_disabled

BASE_URL = os.getenv("EXAMPLE_BASE_URL") or ""
API_KEY = os.getenv("EXAMPLE_API_KEY") or ""
MODEL_NAME = os.getenv("EXAMPLE_MODEL_NAME") or ""

if not BASE_URL or not API_KEY or not MODEL_NAME:
    raise ValueError(
        "Please set EXAMPLE_BASE_URL, EXAMPLE_API_KEY, EXAMPLE_MODEL_NAME via env var or code."
    )

client = AsyncOpenAI(base_url=BASE_URL, api_key=API_KEY)
set_tracing_disabled(disabled=True)


async def demo_single_function_call():
    """
    Demonstrates real-time streaming of function call arguments for a single function.

    As the AI generates a function call, you can see the arguments
    being built up incrementally, rather than waiting for the complete
    function call to finish.
    """
    print("=" * 60)
    print("DEMO 1: Single Function Call Streaming")
    print("=" * 60)

    @function_tool
    def write_file(filename: str, content: str) -> str:
        """Write content to a file."""
        print(f"⚡ write_file: {filename}, {content}")
        return f"File {filename} written successfully"

    agent = Agent(
        name="CodeGenerator",
        instructions="""You are a helpful coding assistant. When asked to create files,
        use the write_file tool with appropriate filenames and content.""",
        model=OpenAIChatCompletionsModel(model=MODEL_NAME, openai_client=client),
        tools=[write_file],
    )

    print("📝 Request: Create a Python script that prints 'Hello, World!' and saves it as hello.py")
    print("🚀 Starting single function call streaming...\n")

    result = Runner.run_streamed(
        agent,
        input="Create a Python script that prints 'Hello, World!' and saves it as hello.py"
    )

    function_name = None
    current_arguments = ""

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            # Function call started
            if event.data.type == "response.output_item.added":
                if hasattr(event.data.item, 'name'):
                    function_name = event.data.item.name
                    print(f"📞 Function call streaming started: {function_name}()")
                    print("📝 Arguments building...")

            # Real-time argument streaming
            elif isinstance(event.data, ResponseFunctionCallArgumentsDeltaEvent):
                current_arguments += event.data.delta
                print(f"   + {event.data.delta}", end="", flush=True)

            # Function call completed
            elif event.data.type == "response.output_item.done":
                if hasattr(event.data.item, 'name'):
                    print(f"\n✅ Function call streaming completed: {function_name}")
                    print(f"🔧 Final arguments: {current_arguments}")
                    print()

    print(f"🎉 Result: {result.final_output}\n")


async def demo_multiple_function_calls():
    """
    Demonstrates real-time streaming of function call arguments for multiple functions.

    As the AI generates multiple function calls, you can see the arguments
    for each function being built up incrementally, with clear identification
    of which arguments belong to which function call.
    """
    print("=" * 60)
    print("DEMO 2: Multiple Function Calls Streaming")
    print("=" * 60)

    # Create multiple tools for a comprehensive demo
    @function_tool
    def create_directory(path: str) -> str:
        """Create a directory at the specified path."""
        print(f"⚡ create_directory: {path}")
        return f"Directory {path} created successfully"

    @function_tool
    def write_file(filename: str, content: str) -> str:
        """Write content to a file."""
        print(f"⚡ write_file: {filename}, {content}")
        return f"File {filename} written successfully"

    @function_tool
    def create_config(project_name: str, version: str, dependencies: list[str]) -> str:
        """Create a configuration file for a project."""
        print(f"⚡ create_config: {project_name}, {version}, {dependencies}")
        return f"Config for {project_name} v{version} created with {len(dependencies)} dependencies"

    @function_tool
    def add_readme(project_name: str, description: str) -> str:
        """Add a README file to the project."""
        print(f"⚡ add_readme: {project_name}, {description}")
        return f"README for {project_name} added with description"

    agent = Agent(
        name="ProjectSetupAgent",
        instructions="""You are a helpful project setup assistant. When asked to create
        a new project, you should:
        1. Create the project directory
        2. Create the main application file
        3. Create a configuration file
        4. Add a README file

        Use all the available tools to set up a complete project structure.""",
        model=OpenAIChatCompletionsModel(model=MODEL_NAME, openai_client=client),
        tools=[create_directory, write_file, create_config, add_readme],
    )

    print("📝 Request: Create a new Python web project called 'my-web-app' with FastAPI")
    print("🚀 Starting multiple function calls streaming...\n")

    result = Runner.run_streamed(
        agent,
        input="Create a new Python web project called 'my-web-app' with FastAPI. Set it up with version 1.0.0 and include dependencies: fastapi, uvicorn, pydantic"
    )

    # Track function calls
    function_calls: dict[Any, dict[str, Any]] = {}  # call_id -> {name, output_index, arguments}
    current_active_call_id = None  # Track which function call is currently receiving arguments

    async for event in result.stream_events():
        if event.type == "raw_response_event":
            # Function call started
            if event.data.type == "response.output_item.added":
                if hasattr(event.data.item, 'name') and hasattr(event.data.item, 'call_id'):
                    output_index = event.data.output_index
                    function_name = event.data.item.name
                    call_id = event.data.item.call_id

                    function_calls[call_id] = {
                        'name': function_name,
                        'output_index': output_index,
                        'arguments': ""
                    }
                    # Set this as the current active function call
                    current_active_call_id = call_id
                    print(f"📞 Function call #{call_id} streaming started: {function_name}()")
                    print("📝 Arguments building...")

            # Real-time argument streaming
            elif isinstance(event.data, ResponseFunctionCallArgumentsDeltaEvent):
                # Use the current active call_id to add arguments
                if current_active_call_id and current_active_call_id in function_calls:
                    # Ensure arguments is always a string
                    prev_args = function_calls[current_active_call_id]['arguments']
                    if not isinstance(prev_args, str):
                        prev_args = str(prev_args)
                    function_calls[current_active_call_id]['arguments'] = prev_args + str(event.data.delta)
                    print(f"   + {event.data.delta}", end="", flush=True)

            # Function call completed
            elif event.data.type == "response.output_item.done":
                if hasattr(event.data.item, 'call_id'):
                    output_index = event.data.output_index
                    call_id = event.data.item.call_id

                    if call_id in function_calls:
                        function_info = function_calls[call_id]
                        print(f"\n✅ Function call #{call_id} streaming completed: {function_info['name']}")
                        print(f"🔧 Final arguments: {function_info['arguments']}")
                        print()
                        # Clear the current active call_id when this function call is done
                        if current_active_call_id == call_id:
                            current_active_call_id = None

    print("📊 Summary of all function calls:")
    for call_id, info in function_calls.items():
        print(f"  - #{call_id}: {info['name']}({info['arguments']})")

    print(f"\n🎉 Result: {result.final_output}\n")


async def main():
    """
    Main function that demonstrates both single and multiple function call streaming.

    This comprehensive demo shows:
    1. How function arguments are streamed for single function calls
    2. How multiple function calls are handled with proper identification
    3. Real-time argument building for complex workflows
    """
    print("🚀 Function Call Arguments Streaming Demo")
    print("This demo shows real-time streaming of function arguments")
    print("for both single and multiple function call scenarios.\n")

    # Demo 1: Single function call
    await demo_single_function_call()

    await asyncio.sleep(1)

    # Demo 2: Multiple function calls
    await demo_multiple_function_calls()


if __name__ == "__main__":
    asyncio.run(main())
