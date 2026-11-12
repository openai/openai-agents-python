"""Selecting a native handoff target after the main model invokes delegation.

    PYTHONPATH=. python -m examples.codex.recipes.decision_handoff

The fixture has been using the real native model loop with synthetic Responses
and Decisions. The named target roster has been supplying application authority.
"""

import asyncio

from agents import Agent, DecisionHandoff
from agents.decisions import DecisionRequest, DecisionResponse
from agents.testing import ScriptedResponses, text, tool_call


class SyntheticDecisions:
    async def get_decision(self, request: DecisionRequest) -> DecisionResponse:
        assert request.input == "A billing question."
        return DecisionResponse(value="billing")


async def main() -> None:
    billing = Agent(name="billing", instructions="Answer billing questions.")
    technical = Agent(name="technical", instructions="Answer technical questions.")
    delegation = DecisionHandoff(
        name="delegate",
        description="Transfer a specialist question.",
        question="Which authorized specialist handles this input?",
        targets={"billing": billing, "technical": technical},
    )
    async with ScriptedResponses(
        [tool_call("delegate", {"input": "A billing question."}, call_id="route")],
        [text("The billing specialist has been answering.")],
    ) as peer:
        main_agent = Agent(
            instructions="Orchestrate and delegate specialist questions.",
            handoffs=[delegation],
            decisions=SyntheticDecisions(),
            provider=peer.provider,
        )
        async with main_agent.session(include_request_attempts=True) as session:
            turn = await session.submit("Consult billing.")
            result = await turn.get_result()
            assert await session.get_active_agent() is billing
            assert len(peer.requests) == 2
            assert turn.submission_request_attempts is not None
            print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
