"""Validation before connecting to a remote executor."""

import pytest
from pydantic import ValidationError

from agents import Agent, Environment, NetworkPolicy
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, responses as responses
from tests.test_native_agent import provider


def declaration(**options):
    return Environment(url="wss://unused.invalid", cwd="/job", tools=["exec_command"], **options)


def test_unix_socket_declaration():
    assert "network" not in declaration()._to_native()
    policy = NetworkPolicy(allow_all_unix_sockets=True)
    assert declaration(network=policy)._to_native()["network"]["allow_all_unix_sockets"] is True
    with pytest.raises(TypeError, match="must be a bool"):
        declaration(network={"allow_all_unix_sockets": 1})
    with pytest.raises(ValueError, match="require exec_command"):
        Environment(url="wss://unused.invalid", cwd="/job", network=policy)


@pytest.mark.parametrize(("saved", "current"), [(True, None), (True, False), (False, True)])
async def test_restore_requires_matching_current_unix_socket_declaration(
    responses: ResponsesFixture, saved: bool, current: bool | None
) -> None:
    # SHA-256 of the canonical declaration granting all Unix sockets and no domains.
    all_sockets = "567c1273b22001a587910a61e8dfa0092ca15df02c492bbb5b5bd2a4a9236a50"
    async with timeout(20):
        agent = Agent(instructions="Use the job.", provider=provider(responses))
        async with agent.session() as session:
            state = await session.save_state()
        # Keep the real idle checkpoint and vary only the saved network declaration.
        state = state.model_copy(update={"network_policy_digest": all_sockets if saved else None})
        assert ("network_policy_digest" in state.to_json()) is saved
        environment = (
            None
            if current is None
            else declaration(
                network=NetworkPolicy(allow_all_unix_sockets=True) if current else None
            )
        )
        with pytest.raises(ValueError, match="Checkpoint and Environment must agree"):
            async with agent.session(state=state, environment=environment):
                pytest.fail("mismatched restore opened")
        with pytest.raises(ValidationError):
            SessionState.model_validate({**state.model_dump(), "network_policy_digest": "true"})
