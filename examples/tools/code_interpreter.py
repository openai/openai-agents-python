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
        model="gpt-5.2",
        instructions="You love doing math.",
        tools=[
            CodeInterpreterTool(
                tool_config={"type": "code_interpreter", "container": {"type": "auto"}},
            )
        ],
    )

    with trace("Code interpreter example"):
        print("Solving math problem...")
        result = Runner.run_streamed(agent, "What is the square root of273 * 312821 plus 1782?")
        async for event in result.stream_events():
            if event.type != "run_item_stream_event":
                continue

            item = event.item
            if item.type == "tool_call_item":
                raw_call = item.raw_item
                if _get_field(raw_call, "type") == "code_interpreter_call":
                    code = _get_field(raw_call, "code")
                    if isinstance(code, str):
                        print(f"Code interpreter code:\n```\n{code}\n```\n")
                        continue

            print(f"Other event: {event.item.type}")

        print(f"Final output: {result.final_output}")


if __name__ == "__main__":
    asyncio.run(main())
