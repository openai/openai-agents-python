import asyncio
import json
import sys
from pathlib import Path

from agents import (
    Agent,
    Allow,
    ApprovalRequested,
    InteractionExpiredError,
    NativeProvider,
    RunBudget,
    function_tool,
)
from agents.state import SessionState


async def main():
    state_path, effects_path, url, decision, turn_id, mode, *budget_args = sys.argv[1:]
    guarded = mode == "guarded"
    effects = Path(effects_path)
    predicates = []
    guards = []

    def before(context):
        guards.append(context.effective_arguments["value"])
        return Allow()

    def policy(context):
        predicates.append(context.call_id)
        return False

    @function_tool(
        name="risky",
        description="Synthetic effect",
        needs_approval=policy,
        before=before if guarded else None,
        run_before_approval=guarded,
    )
    def risky(value: str) -> str:
        with effects.open("a") as out:
            out.write("risky\n")
        return "accepted-" + value

    @function_tool(name="accepted", description="Accepted sibling")
    def accepted(value: str) -> str:
        with effects.open("a") as out:
            out.write("accepted\n")
        return "sibling-" + value

    agent = Agent(
        instructions="Use both tools.",
        tools=[accepted, risky],
        provider=NativeProvider(
            base_url=url, api_key="synthetic-restore", supports_websockets=False
        ),
    )
    async with agent.session(
        state=SessionState.from_json(Path(state_path).read_text()),
        resume_budget=RunBudget(max_requests=int(budget_args[0])) if budget_args else None,
    ) as session:
        turn = session.restored_turn
        assert turn is not None and turn.turn_id == turn_id
        request = None
        async for event in session.interactions():
            request = risky.match(event)
            if request is not None:
                break
        if not isinstance(request, ApprovalRequested):
            await turn.get_result()
            raise AssertionError("Restored turn ended without its captured approval")
        assert request.call_id == "pending-approval" and request.turn_id == turn_id
        assert effects.read_text().splitlines() == ["accepted"]
        assert predicates == [], "restored captured requirement was reevaluated"
        assert guards == [], "completed preapproval input guard was repeated"
        assert request.effective_arguments.value == ("B-pre" if guarded else "B")
        if decision == "approve":
            await request.approve()
        else:
            await request.reject("restored rejection")
        result = await turn.get_result()
        assert result.final_output == "resumed"
        assert guards == (["B-pre"] if guarded and decision == "approve" else [])
        try:
            await request.approve()
        except InteractionExpiredError:
            pass
        else:
            raise AssertionError("duplicate restored grant accepted")
        await session.save_state()
    print(
        json.dumps(
            {
                "turn_id": turn_id,
                "predicates": predicates,
                "effects": effects.read_text().splitlines(),
            }
        )
    )


asyncio.run(main())
