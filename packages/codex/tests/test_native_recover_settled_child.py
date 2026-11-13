"""One-shot recovery may append an answer after an owned child has settled."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agents import Agent, NativeAgents, NativeRole, NativeRoleTool, PromptedOutput, RecoverRun
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import collaboration, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_profile_model_controls import MODEL


class Record(BaseModel):
    count: int


@pytest.mark.parametrize("persist", [True, False])
async def test_recover_after_settled_native_role(responses, tmp_path, monkeypatch, persist):
    role = NativeRole(name="research", instructions="Read the record.", description="Read.")
    managed = NativeRoleTool(name="read_record", role=role.name, description="Read a record.")
    responses.respond(collaboration(managed.name, {"input": "Read."}, "recover-child"))
    responses.respond(assistant_output("child result"))
    responses.respond(assistant_output("invalid parent output"))
    handler_calls = []

    def recover(failure):
        handler_calls.append(failure.kind)
        return RecoverRun({"count": 3}, include_in_history=persist)

    async with timeout(25), provider(responses) as native:
        agent = Agent(
            model=MODEL,
            instructions="Use the tool and return a count.",
            provider=native,
            output_type=PromptedOutput(Record, max_retries=0),
            tools=[
                NativeAgents(
                    additional_roles=[role],
                    managed_tools=[managed],
                    approval=lambda _context, _call: True,
                )
            ],
        )
        async with ExecutorPeer(tmp_path / "remote").serve() as executor:
            trust(executor, tmp_path, monkeypatch)
            result = await agent.run(
                "Read the record.",
                environment=declaration(executor),
                error_handler=recover,
                return_history=True,
                return_items=True,
            )
    assert result.final_output == Record(count=3)
    assert handler_calls == ["invalid_output"]
    assert len(responses.requests) == 3
    assert result.history is not None
    assert (
        any(
            part.get("text") == '{"count": 3}'
            for row in result.history
            if row.get("role") == "assistant"
            for part in row.get("content", ())
        )
        is persist
    )
