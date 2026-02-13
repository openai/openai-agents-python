"""Human-in-the-loop example with a custom rejection message.

This example is intentionally minimal:
1. A single sensitive tool requires human approval.
2. The first turn always issues that tool call.
3. Rejection uses a custom message via ``tool_error_formatter``.
"""

import asyncio

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    ToolErrorFormatterArgs,
    function_tool,
)
from examples.auto_mode import confirm_with_fallback


async def tool_error_formatter(args: ToolErrorFormatterArgs[None]) -> str | None:
    """Build a simple output message for rejected tool calls."""
    if args.kind != "approval_rejected":
        return None
    # The defualt one is "Tool execution was not approved."
    return "Publish action was canceled because approval was rejected."


@function_tool(needs_approval=True)
async def publish_announcement(title: str, body: str) -> str:
    """Simulate publishing an announcement to users."""
    return f"Published announcement '{title}' with body: {body}"


async def main() -> None:
    agent = Agent(
        name="Operations Assistant",
        instructions=(
            "When a user asks to publish an announcement, call the publish_announcement tool directly. "
            "Do not ask the user for approval in plain text; runtime approvals handle that."
        ),
        model_settings=ModelSettings(tool_choice="publish_announcement"),
        tools=[publish_announcement],
    )
    run_config = RunConfig(tool_error_formatter=tool_error_formatter)

    result = await Runner.run(
        agent,
        "Please publish an announcement titled 'Office maintenance' with body "
        "'The office will close at 6 PM today.'",
        run_config=run_config,
    )

    while result.interruptions:
        print("\nApproval required:")
        state = result.to_state()
        for interruption in result.interruptions:
            print(f"- Tool: {interruption.name}")
            print(f"  Arguments: {interruption.arguments}")
            approved = confirm_with_fallback(
                "Approve this tool call? [y/N]: ",
                default=False,
            )
            if approved:
                state.approve(interruption)
            else:
                state.reject(interruption)

        result = await Runner.run(agent, state, run_config=run_config)

    print("\nFinal output:")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
