"""Approve a synthetic function call before its callback runs."""

import asyncio

from agents import Agent, ApprovalRequested, function_tool
from agents.testing import ScriptedResponses, text, tool_call


async def main() -> None:
    effects: list[str] = []

    @function_tool(needs_approval=True)
    def record(value: str) -> str:
        """Record a synthetic value after application approval."""
        effects.append(value)
        return value

    async with ScriptedResponses(
        [tool_call("record", {"value": "synthetic"}, call_id="review-record")],
        [text("Recorded.")],
    ) as peer:
        agent = Agent(instructions="Record the value.", tools=[record], provider=peer.provider)
        async with agent.session() as session:
            turn = await session.submit("Record the synthetic value.")
            approved = False
            async for request in session.interactions():
                if isinstance(request, ApprovalRequested):
                    assert effects == []
                    assert request.effective_arguments.value == "synthetic"
                    await request.approve()
                    approved = True
                    break
            assert approved
            assert (await turn.get_result()).final_output == "Recorded."
    assert effects == ["synthetic"]
    print("Approved callback ran once.")


if __name__ == "__main__":
    asyncio.run(main())
