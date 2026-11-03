"""Exercise native tool publication and read-only descendant inspection."""

from dataclasses import dataclass, field

import pytest

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    responses as responses,
)


@dataclass
class Application:
    calls: list[tuple[str, str]] = field(default_factory=list)


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-session-operations-key")


async def test_independent_sessions_are_not_native_descendants(responses: ResponsesFixture) -> None:
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(30), agent.session() as first, agent.session() as second:
        assert first.id != second.id
        assert [item async for item in first.subagents()] == []
        assert [item async for item in second.subagents()] == []
        assert responses.requests == []
    with pytest.raises(RuntimeError, match="closed"):
        [item async for item in first.subagents()]
