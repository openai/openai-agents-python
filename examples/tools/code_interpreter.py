import asyncio
from collections.abc import Mapping
from typing import Any

from agents import Agent, CodeInterpreterTool, Runner, trace


def _get_field(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


async def main():
    agent = Agent(
        name="Code interpreter",
        # Note: using gpt-5-class models with streaming for this tool may require org verification.
        # Code interpreter does not support gpt-5 minimal reasoning effort; use default effort.
        model="gpt-5.4",
        instructions=(
            "Always use the code interpreter tool to solve numeric problems, and show the code "
            "you ran when possible."
        ),
        tools=[
            CodeInterpreterTool(
                tool_config={"type": "code_interpreter", "container": {"type": "auto"}},
            )
        ],
    )

    with trace("Code interpreter example"):
        print("Solving math problem with the code interpreter...")
        result = Runner.run_streamed(
            agent,
            (
                "Use the code interpreter tool to calculate the square root of 273 * 312821 + "
                "1782. Show the Python code you ran and then provide the numeric answer."
            ),
        )
        saw_code_interpreter_call = False
        async for event in result.stream_events():
            if event.type != "run_item_stream_event":
                continue

            item = event.item
            if item.type == "tool_call_item":
                raw_call = item.raw_item
                if _get_field(raw_call, "type") == "code_interpreter_call":
                    saw_code_interpreter_call = True
                    code = _get_field(raw_call, "code")
                    if isinstance(code, str):
                        print(f"Code interpreter code:\n```\n{code}\n```\n")
                        continue

            print(f"Other event: {event.item.type}")

        if not saw_code_interpreter_call:
            print("No code_interpreter_call item was emitted.")
        print(f"Final output: {result.final_output}")


if __name__ == "__main__":
    asyncio.run(main())
