"""Fresh local storage admits the opening Agent before any model request."""

from pathlib import Path

import pytest

from agents import Agent, LocalStorage, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, responses as responses


@pytest.mark.parametrize("with_graph", [False, True], ids=["mono", "graph"])
async def test_local_opening_activates_original_agent_without_model_request(
    responses: ResponsesFixture, tmp_path: Path, with_graph: bool
) -> None:
    target = Agent(name="B", instructions="B handles delegated work.")
    source = Agent(
        name="A",
        instructions="A starts the conversation.",
        handoffs=[target] if with_graph else [],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-local-profile-opening"),
    )
    async with (
        timeout(20),
        source.session(storage=LocalStorage(tmp_path / "profile")) as session,
    ):
        assert await session.get_active_agent() is source
        assert not responses.requests
    assert not responses.requests
