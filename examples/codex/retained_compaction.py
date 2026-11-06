"""Keep an original message alongside an application-written summary.

Set OPENAI_API_KEY and run `python examples/codex/retained_compaction.py`.
"""

import asyncio

from agents import Agent, ModelContextSnapshot, SessionState


async def main() -> None:
    agent = Agent(instructions="Answer briefly using the conversation.")
    constraint = "Use metric units for every measurement."
    async with agent.session() as session:
        await (await session.submit(constraint)).get_result()
        await (await session.submit("Suggest an easy hike for a beginner.")).get_result()

        snapshot: ModelContextSnapshot = await session.model_context()
        retain = [
            index
            for index, item in enumerate(snapshot.items)
            if item.get("role") == "user"
            and any(part.get("text") == constraint for part in item.get("content", []))
        ]
        await session.compact(
            summary="The user is planning an easy beginner hike.",
            snapshot=snapshot,
            retain=retain,
        )
        saved = (await session.save_state()).to_json()

    async with agent.session(state=SessionState.from_json(saved)) as restored:
        turn = await restored.submit("Suggest a suitable distance and elevation gain.")
        print((await turn.get_result()).final_output)


if __name__ == "__main__":
    asyncio.run(main())
