# pyright: strict
"""Manual declaration matching preserves input and reply types for consumers."""

from pydantic import BaseModel
from typing_extensions import assert_type

from agents import (
    ActionReply,
    ActionRequested,
    Agent,
    Completed,
    FunctionTool,
    Interaction,
    InteractionRequired,
    Session,
    Turn,
)
from agents.observation import Observation
from agents.state import SessionState


class LookupArgs(BaseModel):
    key: str


class LookupResult(BaseModel):
    value: int


lookup = FunctionTool(
    name="lookup", description="Look up a key", input_type=LookupArgs, output_type=LookupResult
)
assert_type(lookup, FunctionTool[LookupArgs, LookupResult])
agent = Agent[None, str](instructions="Use lookup.", tools=[lookup])


async def match_unknown(event: object) -> None:
    request = lookup.match(event)
    assert_type(request, ActionRequested[LookupArgs, LookupResult] | None)
    if request is not None:
        assert_type(request.arguments, LookupArgs)
        assert_type(request.subagent_id, str | None)
        assert_type(await request.respond(LookupResult(value=1)), None)


def passive_stays_separate(event: Observation) -> None:
    assert_type(lookup.match(event), ActionRequested[LookupArgs, LookupResult] | None)


async def submit_reply() -> None:
    async with agent.session() as session:
        await session.submit("look up a key")
        async for event in session.interactions():
            request = lookup.match(event)
            if request is not None:
                assert_type(await request.respond(LookupResult(value=1)), None)


async def submit_to_structured_session(
    session: Session[None, LookupResult],
    request: ActionRequested[LookupArgs, LookupResult],
) -> None:
    # Session replies can target a root or a native child. A text submission
    # always targets the root and retains its exact output type.
    root = await session.submit("look up a key")
    assert_type(root, Turn[LookupResult])
    assert_type(await root.get_result(), Completed[LookupResult])
    assert_type(await request.respond(LookupResult(value=1)), None)


async def resume_after_explicit_suspend(session: Session[None, str]) -> None:
    turn = await session.submit("look up a key")
    saved = await session.suspend()
    assert_type(saved, SessionState)
    try:
        await turn.get_result()
    except InteractionRequired as paused:
        assert_type(paused.state, SessionState | None)
        assert paused.state is not None
        assert_type(paused.state, SessionState)
        saved = SessionState.from_json(paused.state.to_json())
    async with agent.session(state=saved) as restored:
        assert_type(restored, Session[None, str])


def answer(event: Interaction) -> ActionReply[LookupResult]:
    request = lookup.match(event)
    assert request is not None
    reply = request.reply(LookupResult(value=1))
    assert_type(reply, ActionReply[LookupResult])
    return reply


async def answer_async(
    event: Interaction,
) -> ActionReply[LookupResult]:
    return answer(event)


async def run_with_interactions() -> None:
    assert_type(await agent.run("read", on_interaction=answer), Completed[str])
    assert_type(await agent.run("read", on_interaction=answer_async), Completed[str])


def run_sync_with_interactions() -> None:
    assert_type(agent.run_sync("read", on_interaction=answer_async), Completed[str])
    assert_type(agent.run_sync("read", on_interaction=answer), Completed[str])
