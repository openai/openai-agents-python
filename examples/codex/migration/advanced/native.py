"""Native port and explicit cutover from reviewed business facts, without state conversion."""

import argparse
import asyncio
import json
from pathlib import Path

from agents import Agent, Allow, Deny, NativeProvider, SessionState, ToolContext, function_tool
from agents.testing import ScriptedResponses, text, tool_call

from .business import (
    ANSWER_JSON,
    FOLLOW_UP,
    INITIAL,
    INSTRUCTIONS,
    REVIEW_JSON,
    Application,
    Invoice,
    Policy,
    PolicyReview,
    SupportAnswer,
    can_access,
    read_invoice,
    record_case,
)


def build_agent(provider: NativeProvider, specialist_provider: NativeProvider):
    @function_tool
    def read_policy(context: ToolContext[Policy]) -> str:
        """Read the host-supplied billing policy."""
        return context.context.text

    specialist = Agent(
        name="Policy specialist",
        instructions="Read the policy and return a billing recommendation. Do not perform writes.",
        tools=[read_policy],
        provider=specialist_provider,
        output_type=PolicyReview,
    )

    @function_tool
    def lookup_invoice(context: ToolContext[Application], invoice_id: str) -> Invoice:
        """Read an invoice inside the application's tenant scope."""
        return read_invoice(context.context, invoice_id)

    @function_tool
    async def consult_policy(question: str) -> str:
        """Return the separately configured specialist's review to this agent."""
        # This one-shot call owns a separate Session and returns to Invoice support.
        review = await specialist.run(question, context=Policy())
        return review.final_output.model_dump_json()

    def authorize_case(context: ToolContext[Application]) -> Allow | Deny:
        if can_access(context.context, context.effective_arguments["invoice_id"]):
            return Allow()
        return Deny("Invoice is outside the application's tenant scope.")

    @function_tool(before=authorize_case)
    def open_case(context: ToolContext[Application], invoice_id: str) -> str:
        """Record a billing support case only for an authorized invoice."""
        return record_case(context.context, invoice_id)

    return Agent(
        name="Invoice support",
        instructions=INSTRUCTIONS,
        tools=[lookup_invoice, consult_policy, open_case],
        provider=provider,
        output_type=SupportAnswer,
    )


async def run_example(directory: Path):
    """Exercise the port on a fresh synthetic request, then reopen its native checkpoint."""
    directory.mkdir(parents=True, exist_ok=True)
    app = Application()
    async with (
        ScriptedResponses(
            [tool_call("lookup_invoice", {"invoice_id": "INV-42"}, call_id="lookup")],
            [
                tool_call(
                    "consult_policy", {"question": "Review the paid invoice."}, call_id="review"
                )
            ],
            [tool_call("open_case", {"invoice_id": "INV-42"}, call_id="allowed")],
            [tool_call("open_case", {"invoice_id": "INV-private"}, call_id="denied")],
            [text(ANSWER_JSON)],
            [text(ANSWER_JSON)],
        ) as peer,
        ScriptedResponses(
            [tool_call("read_policy", {}, call_id="policy")], [text(REVIEW_JSON)]
        ) as specialist_peer,
    ):
        agent = build_agent(peer.provider, specialist_peer.provider)
        async with agent.session(context=app) as session:
            turn = await session.submit(INITIAL)
            answer = (await turn.get_result()).final_output
            assert answer.model_dump() == json.loads(ANSWER_JSON)
            state = await session.save_state()
            (directory / "native-state.json").write_text(state.to_json(), encoding="utf-8")
        # Close the producer before restoring with current application tools and context.
        state = SessionState.from_json(
            (directory / "native-state.json").read_text(encoding="utf-8")
        )
        async with agent.session(context=app, state=state) as restored:
            turn = await restored.submit(FOLLOW_UP)
            answer = (await turn.get_result()).final_output
        assert answer.model_dump() == json.loads(ANSWER_JSON)
        # The list records every actual callback effect; it does not deduplicate repeats.
        assert app.case_writes == ["INV-42"]
        assert peer.tool_output("allowed") == "case-1"
        assert "outside" in (peer.tool_output("denied") or "")
        assert "case-1" in json.dumps(peer.requests[-1]["input"])
        assert "atlas" in json.dumps(peer.requests[-1]["input"])
        assert "Paid invoices" in json.dumps(specialist_peer.requests[-1]["input"])
    return app, answer, peer, specialist_peer


async def continue_from_reviewed_facts(facts_file: Path, directory: Path):
    """Start a new conversation; a completed old case must not run again."""
    facts = SupportAnswer.model_validate_json(facts_file.read_text(encoding="utf-8"))
    directory.mkdir(parents=True, exist_ok=True)
    app = Application()
    prompt = (
        "Reviewed business facts from an earlier conversation: "
        + facts.model_dump_json()
        + "\nThe case was already recorded. This is a new conversation, with no pending calls "
        "or approvals. Repeat these facts without calling tools."
    )
    async with (
        ScriptedResponses([text(facts.model_dump_json())], [text(facts.model_dump_json())]) as peer,
        ScriptedResponses() as specialist_peer,
    ):
        agent = build_agent(peer.provider, specialist_peer.provider)
        async with agent.session(context=app) as session:
            assert not [item async for item in session.items()]
            turn = await session.submit(prompt)
            answer = (await turn.get_result()).final_output
            state = await session.save_state()
            checkpoint = directory / "cutover-state.json"
            checkpoint.write_text(state.to_json(), encoding="utf-8")
        state = SessionState.from_json(checkpoint.read_text(encoding="utf-8"))
        async with agent.session(context=app, state=state) as restored:
            turn = await restored.submit(FOLLOW_UP)
            answer = (await turn.get_result()).final_output
        assert answer == facts
        assert app.case_writes == []
        assert "Reviewed business facts" in json.dumps(peer.requests[0]["input"])
        assert facts.case_id in json.dumps(peer.requests[0]["input"])
        assert facts.label in json.dumps(peer.requests[0]["input"])
        assert facts.case_id in json.dumps(peer.requests[1]["input"])
        assert facts.label in json.dumps(peer.requests[1]["input"])
    return app, answer, peer


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--facts", type=Path)
    args = parser.parse_args()
    app, answer, _, _ = await run_example(args.directory)
    print(f"native port: {answer.model_dump_json()}; new writes={app.case_writes}")
    if args.facts is not None:
        app, answer, _ = await continue_from_reviewed_facts(args.facts, args.directory)
        print(f"native cutover: {answer.model_dump_json()}; new writes={app.case_writes}")


if __name__ == "__main__":
    asyncio.run(main())
