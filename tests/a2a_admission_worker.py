"""Restore host policy and a prepared call in a genuinely fresh interpreter."""

import asyncio
import json
import sys
from pathlib import Path

from agents import Agent
from agents.state import SessionState
from agents.testing import ScriptedResponses
from tests.test_a2a_host_admission import configured
from tests.test_native_manual_tools import next_request


async def main():
    authority, journal, jobs, tool, _ = configured(Path(sys.argv[1]), sys.argv[2])
    authority.admit(journal.state_json, "checkpoint")
    denied = False
    async with ScriptedResponses() as peer:
        agent = Agent(instructions="Synthetic", tools=[tool], provider=peer.provider)
        async with agent.session(state=SessionState.from_json(journal.state_json)) as session:
            request = await next_request(session.interactions(), tool)
            try:
                await jobs.advance(session, request)
            except PermissionError:
                denied = True
        assert authority.denied == ["claim"]
        result = {
            "denied": denied,
            "model_requests": len(peer.requests),
            "writes": journal.writes,
            "policy": authority.policy()["current"],
        }
        print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
