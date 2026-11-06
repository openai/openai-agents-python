"""Carry application-owned task state across automatic or manual compaction."""

import asyncio
from dataclasses import dataclass

from agents import Agent, CompactionContext


@dataclass
class TaskState:
    objective: str
    requirements: str
    pending: str
    references: str


def preserve_task(request: CompactionContext[TaskState]) -> str:
    # This application keeps the task's essential state outside the transcript.
    task = request.context
    return (
        f"Objective: {task.objective}\n"
        f"Requirements: {task.requirements}\n"
        f"Pending: {task.pending}\n"
        f"References: {task.references}"
    )


async def main() -> None:
    task = TaskState(
        objective="Prepare a project status update.",
        requirements="Include completed work, open questions, and next steps.",
        pending="Draft the update from the project notes.",
        references="Project notes: prototype complete; deployment date undecided.",
    )
    agent = Agent[TaskState, str](
        instructions="Write concise project updates using the supplied task state.",
        before_compact=preserve_task,
    )
    async with agent.session(context=task) as session:
        # The same callback runs when a long conversation compacts automatically.
        await session.compact()
        turn = await session.submit("Draft the project update.")
        print((await turn.get_result()).final_output)


if __name__ == "__main__":
    asyncio.run(main())
