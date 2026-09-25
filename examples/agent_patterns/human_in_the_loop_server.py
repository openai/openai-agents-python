"""Keep serialized RunState on the server; accept only decisions from the client.

Run with `uv run -m examples.agent_patterns.human_in_the_loop_server`.
The CLI simulates a client and an authenticated server in one process. An HTTP
adapter must obtain the user identity from its authenticated session, authorize
access to the run, and apply its normal request/CSRF protections. Never accept
the user identity or serialized RunState from the approval request body.

This store is for one event loop in one process. Production applications need
bounded retention and an atomic, owner-checked consume operation in shared
storage. A consumed request cannot be retried, including after cancellation or
failure; reconcile tool side effects before initiating another run.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import Any

from agents import Agent, Runner, RunResult, RunState
from agents.decorators import tool
from examples.auto_mode import confirm_with_fallback


@dataclass(frozen=True)
class ApprovalPrompt:
    decision_id: str
    tool_name: str
    arguments: str | None


@dataclass(frozen=True)
class PendingApproval:
    request_id: str
    prompts: tuple[ApprovalPrompt, ...]


@dataclass(frozen=True)
class _StoredRun:
    owner_id: str
    state_string: str
    decision_ids: tuple[str, ...]


class ApprovalServer:
    """Example application service; identities come from trusted authentication code."""

    def __init__(self, agent: Agent[Any]) -> None:
        self._agent = agent
        self._pending: dict[str, _StoredRun] = {}

    async def start(self, authenticated_user_id: str, message: str) -> PendingApproval | RunResult:
        result = await Runner.run(self._agent, message)
        return self._save(authenticated_user_id, result)

    def _save(self, owner_id: str, result: RunResult) -> PendingApproval | RunResult:
        if not result.interruptions:
            return result
        state = result.to_state()
        interruptions = state.get_interruptions()
        decision_ids = tuple(secrets.token_urlsafe(32) for _ in interruptions)
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = _StoredRun(owner_id, state.to_string(), decision_ids)
        # These detached display values are the only approval data sent to the client.
        # Filter tool arguments here if the authenticated reviewer must not see them.
        return PendingApproval(
            request_id,
            tuple(
                ApprovalPrompt(decision_id, item.name or "unknown_tool", item.arguments)
                for decision_id, item in zip(decision_ids, interruptions, strict=False)
            ),
        )

    async def decide(
        self,
        authenticated_user_id: str,
        request_id: str,
        decisions: dict[str, bool],
    ) -> PendingApproval | RunResult:
        stored = self._pending.get(request_id)
        if stored is None or stored.owner_id != authenticated_user_id:
            raise ValueError("Approval request is unavailable.")
        # An HTTP adapter must validate its request schema before calling this method.
        if set(decisions) != set(stored.decision_ids) or any(
            type(value) is not bool for value in decisions.values()
        ):
            raise ValueError("Provide one boolean decision for every pending tool call.")
        decisions = dict(decisions)
        # No await between ownership validation and consumption: a second submission
        # in this event loop cannot execute the same saved run, even while resume awaits.
        del self._pending[request_id]
        state = await RunState.from_string(self._agent, stored.state_string)
        for decision_id, interruption in zip(
            stored.decision_ids, state.get_interruptions(), strict=False
        ):
            if decisions[decision_id]:
                state.approve(interruption)
            else:
                state.reject(interruption)
        result = await Runner.run(self._agent, state)
        return self._save(authenticated_user_id, result)


@tool(needs_approval=True)
def get_temperature(city: str) -> str:
    """Return a sample temperature for a city."""
    return f"The temperature in {city} is 20 Celsius."


async def main() -> None:
    server = ApprovalServer(
        Agent(
            name="Weather assistant",
            instructions="Use get_temperature to answer temperature questions.",
            tools=[get_temperature],
        )
    )
    # Simulation only: replace this with your server's authenticated session identity.
    authenticated_user_id = "example-user"
    response = await server.start(authenticated_user_id, "What is the temperature in Oakland?")
    while isinstance(response, PendingApproval):
        decisions = {
            prompt.decision_id: confirm_with_fallback(
                f"Allow {prompt.tool_name} with {prompt.arguments}? (y/n): ", default=False
            )
            for prompt in response.prompts
        }
        response = await server.decide(authenticated_user_id, response.request_id, decisions)
    # RunResult remains server-side; only the application-selected output is displayed.
    print(response.final_output)


if __name__ == "__main__":
    asyncio.run(main())
