"""Classic port: typed tools, a specialist, effects and SQLite history.

Install the matching source with its classic extra as described in README.md.
"""

import argparse
import asyncio
import json
from pathlib import Path

from agents.classic import Agent, RunConfig, RunContextWrapper, Runner, SQLiteSession, function_tool
from agents.classic.testing import ScriptedModel, assistant_message, function_call

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
    read_invoice,
    record_case,
    write_reviewed_facts,
)


def build_agent(model: ScriptedModel, specialist_model: ScriptedModel):
    @function_tool
    def read_policy(context: RunContextWrapper[Policy]) -> str:
        """Read the host-supplied billing policy."""
        return context.context.text

    specialist = Agent(
        name="Policy specialist",
        instructions="Read the policy and return a billing recommendation. Do not perform writes.",
        tools=[read_policy],
        model=specialist_model,
        output_type=PolicyReview,
    )

    @function_tool
    def lookup_invoice(context: RunContextWrapper[Application], invoice_id: str) -> Invoice:
        """Read an invoice inside the application's tenant scope."""
        return read_invoice(context.context, invoice_id)

    @function_tool
    async def consult_policy(question: str) -> str:
        """Return the separately configured specialist's review to this agent."""
        review = await Runner.run(
            specialist, question, context=Policy(), run_config=RunConfig(tracing_disabled=True)
        )
        return review.final_output.model_dump_json()

    @function_tool
    def open_case(context: RunContextWrapper[Application], invoice_id: str) -> str:
        """Record a billing support case only for an authorized invoice."""
        return record_case(context.context, invoice_id)

    return Agent(
        name="Invoice support",
        instructions=INSTRUCTIONS,
        tools=[lookup_invoice, consult_policy, open_case],
        model=model,
        output_type=SupportAnswer,
    )


async def run_example(directory: Path, *, resume: bool = False):
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "classic.sqlite"
    if not resume and database.exists():
        raise ValueError("Choose a fresh directory, or use --resume for the existing store.")
    if resume and not database.is_file():
        raise ValueError("Run the v0 baseline first; its SQLite store is required.")
    app = Application()
    model = ScriptedModel(
        [[assistant_message(ANSWER_JSON)]]
        if resume
        else [
            [function_call("lookup_invoice", {"invoice_id": "INV-42"}, call_id="lookup")],
            [
                function_call(
                    "consult_policy", {"question": "Review the paid invoice."}, call_id="review"
                )
            ],
            [function_call("open_case", {"invoice_id": "INV-42"}, call_id="allowed")],
            # Deliberately request an unauthorized effect to test the application boundary.
            [function_call("open_case", {"invoice_id": "INV-private"}, call_id="denied")],
            [assistant_message(ANSWER_JSON)],
            [assistant_message(ANSWER_JSON)],
        ]
    )
    specialist_model = ScriptedModel(
        []
        if resume
        else [
            [function_call("read_policy", {}, call_id="policy")],
            [assistant_message(REVIEW_JSON)],
        ]
    )
    agent = build_agent(model, specialist_model)
    session = SQLiteSession("invoice-support", database)
    try:
        result = await Runner.run(
            agent,
            FOLLOW_UP if resume else INITIAL,
            context=app,
            session=session,
            run_config=RunConfig(tracing_disabled=True),
        )
        if not resume:
            result = await Runner.run(
                agent,
                FOLLOW_UP,
                context=app,
                session=session,
                run_config=RunConfig(tracing_disabled=True),
            )
        assert isinstance(result.final_output, SupportAnswer)
        assert result.final_output.model_dump() == json.loads(ANSWER_JSON)
        assert app.case_writes == ([] if resume else ["INV-42"])
        # A canned recall reply alone would not prove that prior history was supplied.
        assert "case-1" in json.dumps(model.calls[-1].input)
        assert "atlas" in json.dumps(model.calls[-1].input)
        if not resume:
            assert "case-1" in json.dumps(model.calls[3].input)
            assert "error" in json.dumps(model.calls[4].input).lower()
            assert "Paid invoices" in json.dumps(specialist_model.calls[-1].input)
        model.assert_complete()
        specialist_model.assert_complete()
        write_reviewed_facts(directory, result.final_output)
        return app, result.final_output, model, specialist_model
    finally:
        session.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    app, answer, _, _ = await run_example(args.directory, resume=args.resume)
    print(f"classic: {answer.model_dump_json()}; new writes={app.case_writes}")


if __name__ == "__main__":
    asyncio.run(main())
