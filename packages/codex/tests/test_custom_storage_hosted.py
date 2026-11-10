"""Provider-hosted tools compose with the selected canonical backend."""

import json
import os
from pathlib import Path

import pytest

from agents import Agent, CustomStorage, NativeProvider, SQLiteSessionStore, WebSearch
from tests.native_fixtures import assistant_output, responses as responses


@pytest.mark.skipif(
    os.name != "posix", reason="SQLiteSessionStore reference adapter requires POSIX locking"
)
async def test_custom_backend_retains_hosted_search_across_native_resume(tmp_path: Path, responses):
    action = {"type": "search", "query": "synthetic query", "sources": []}
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "web_search_call",
                "id": "ws_stored",
                "status": "completed",
                "action": action,
            },
        },
        assistant_output("stored answer"),
    )
    responses.respond(assistant_output("continued"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-storage-hosted"
    ) as provider:
        agent = Agent(
            instructions="Answer.",
            model="gpt-5.5",
            provider=provider,
            tools=[WebSearch(route="hosted")],
        )
        storage = CustomStorage(SQLiteSessionStore(tmp_path / "canonical.sqlite"))
        async with agent.session(storage=storage) as session:
            identity = session.id
            assert (
                await (await session.submit("research")).get_result()
            ).final_output == "stored answer"
        async with agent.session(storage=storage, resume=identity) as restored:
            searches = [
                item async for item in restored.items() if item["type"] == "web_search_call"
            ]
            assert searches
            assert searches[0]["action"]["type"] == "search"
            assert searches[0]["action"]["query"] == "synthetic query"
            assert searches[0]["action"]["sources"] == []
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "continued"
    assert "web_search" in json.dumps(responses.requests[0]["tools"])
    assert "stored answer" in json.dumps(responses.requests[1]["input"])
