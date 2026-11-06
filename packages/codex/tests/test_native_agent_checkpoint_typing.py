# pyright: strict
"""Checkpoint state keeps the current Agent's context and output types."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from typing_extensions import assert_type

from agents import (
    Agent,
    Completed,
    LocalStorage,
    PendingRequests,
    Session,
    SessionState,
    ToolContext,
    function_tool,
)


@dataclass
class Application:
    tenant: str


class Answer(BaseModel):
    answer: str


@function_tool
async def lookup(context: ToolContext[Application]) -> str:
    return context.context.tenant


async def checkpoint(state: SessionState) -> None:
    assert_type(state.instructions, str)
    agent = Agent(instructions=state.instructions, tools=[lookup], output_type=Answer)
    async with agent.session(state=state, context=Application("current")) as session:
        assert_type(session, Session[Application, Answer])
        assert_type(await session.update(model="gpt-5.2"), None)
        assert_type(await session.update(model_settings={"reasoning_effort": "high"}), None)
        assert_type(await session.update(model_settings={"service_tier": None}), None)
        saved = await session.save_state()
        assert_type(saved, SessionState)
        assert_type(SessionState.from_json(saved.to_json()), SessionState)
        handle = await session.submit("Continue.")
        assert_type(await handle.get_result(), Completed[Answer])
        assert_type(
            await handle.wait_for_request_or_result(), Completed[Answer] | PendingRequests[Answer]
        )
        assert_type(await handle.get_result(), Completed[Answer])
    async with Agent(instructions="Answer.").session(state=state) as text_session:
        assert_type(text_session, Session[None, str])


async def local_reopen(path: Path, thread_id: str) -> None:
    agent = Agent(instructions="Use the current record.", tools=[lookup], output_type=Answer)
    async with agent.session(
        context=Application("new process"), storage=LocalStorage(path), resume=thread_id
    ) as session:
        assert_type(session, Session[Application, Answer])
        assert_type(session.id, str)
        result = await (await session.submit("New input.")).get_result()
        assert_type(result, Completed[Answer])


async def settled_fork() -> None:
    agent = Agent(instructions="Answer.", tools=[lookup], output_type=Answer)
    async with agent.session(context=Application("source")) as source:
        copied = await source.fork()
        assert_type(copied, SessionState)
    # The copied state conveys no context or output typing authority. The
    # destination Agent supplies both, through its existing explicit restore.
    async with agent.session(state=copied, context=Application("fresh")) as restored:
        assert_type(restored, Session[Application, Answer])
        assert_type(await (await restored.submit("Continue.")).get_result(), Completed[Answer])
