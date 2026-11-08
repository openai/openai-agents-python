# pyright: reportArgumentType=true, reportCallIssue=true, reportAssignmentType=true, reportUnnecessaryTypeIgnoreComment=true
"""Handoff consumers retain context/output types and mismatch diagnostics."""

from typing import TYPE_CHECKING

from typing_extensions import assert_type

from agents import Agent, SessionState

if TYPE_CHECKING:
    text = Agent[dict[str, str], str](instructions="Answer", name="Text")
    structured = Agent[dict[str, str], int](instructions="Count", name="Count", output_type=int)
    inferred = Agent(instructions="Route", handoffs=[text])
    assert_type(inferred, Agent[dict[str, str], str])
    typed = Agent(instructions="Route", output_type=int, handoffs=[structured])
    assert_type(typed, Agent[dict[str, str], int])

    def deferred() -> Agent[dict[str, str], int]:
        return structured

    lazy = Agent(instructions="Route", output_type=int, handoffs=[deferred])
    assert_type(lazy, Agent[dict[str, str], int])

    Agent[dict[str, str], str](instructions="Wrong output", handoffs=[structured])  # pyright: ignore[reportArgumentType]
    Agent[dict[str, str], str](instructions="Wrong lazy output", handoffs=[deferred])  # pyright: ignore[reportArgumentType]
    wrong_context = Agent[int, str](instructions="Other", name="Other")
    Agent[dict[str, str], str](instructions="Wrong context", handoffs=[wrong_context])  # pyright: ignore[reportArgumentType]

    async def consume_session(saved: SessionState) -> None:
        async with inferred.session(context={}) as session:
            assert_type(await session.get_active_agent(), Agent[dict[str, str], str])
            await session.update(agent=text)
            await session.update(agent=structured)  # pyright: ignore[reportArgumentType]
            await session.update(agent=wrong_context)  # pyright: ignore[reportArgumentType]
        async with inferred.session(context={}, state=saved, restored_output_type=int) as session:
            # Restored output remains erased; future submits retain declared str.
            assert_type(await session.get_active_agent(), Agent[dict[str, str], str])
