"""Withholding provisional text and releasing only application-accepted results.

    PYTHONPATH=. python -m examples.codex.recipes.accepted_output

Using synthetic Responses HTTP through the native Session, without credentials.
Keeping all effects in memory; see accepted-output.md for the publication contract.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agents import (
    Agent,
    CompletionContext,
    ContinueTurn,
    RunBudget,
    StopTurn,
    TurnStoppedError,
    function_tool,
)
from agents.observation import OutputTextDelta
from agents.testing import ScriptedResponse, ScriptedResponses, event, text, tool_call


def streamed_text(item_id: str, value: str) -> ScriptedResponse:
    return ScriptedResponse(
        [
            event(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": item_id,
                        "role": "assistant",
                        "content": [],
                    },
                }
            ),
            event(
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": value,
                }
            ),
            text(value, item_id=item_id),
        ]
    )


async def revised_report() -> list[str]:
    published: list[str] = []
    private_deltas: list[str] = []
    private_candidates: list[str | None] = []

    async def check_report(request: CompletionContext[None]) -> ContinueTurn | StopTurn | None:
        private_candidates.append(request.text)
        print("Checking report facts...")
        if request.text is None or "2 pending checks" not in request.text:
            if request.continuation_count >= 1:
                return StopTurn("Report facts remain incomplete")
            return ContinueTurn("Include the 2 pending checks; do not claim all checks passed.")
        return None

    async with ScriptedResponses(
        streamed_text("report-draft", "All checks passed."),
        streamed_text(
            "report-revised", "Inventory report: 10 checks passed; 2 pending checks need review."
        ),
    ) as peer:
        agent = Agent(
            instructions="Summarize the supplied inventory facts.",
            provider=peer.provider,
            before_complete=check_report,
        )
        async with agent.session() as session:
            turn = await session.submit(
                "Inventory facts: 10 checks passed, 2 checks pending.",
                budget=RunBudget(max_requests=2),
            )
            # Keeping observations private, including text later rejected by the hook.
            async for observation in turn.events():
                if isinstance(observation, OutputTextDelta):
                    assert not published
                    private_deltas.append(observation.delta)
            result = await turn.get_result()
            if result.final_output is None or "2 pending checks" not in result.final_output:
                raise ValueError("Missing accepted report")
            published.append(result.final_output)
        assert len(peer.requests) == 2

    assert private_candidates[0] == "All checks passed."
    assert private_deltas == [
        "All checks passed.",
        "Inventory report: 10 checks passed; 2 pending checks need review.",
    ]
    assert published == ["Inventory report: 10 checks passed; 2 pending checks need review."]
    print("Accepted report:", published[0])
    return published


class SupportAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    case_id: str
    message: str


SupportMode = Literal["accepted", "rejected", "cancelled", "failed", "invalid"]


@dataclass
class SupportContext:
    # Supplying approval from the application, outside model-controlled arguments.
    approved: bool
    checking: asyncio.Event = field(default_factory=asyncio.Event)


async def support_answer(mode: SupportMode) -> list[SupportAnswer]:
    published: list[SupportAnswer] = []
    app = SupportContext(approved=mode != "rejected")

    async def check_approval(request: CompletionContext[SupportContext]) -> StopTurn | None:
        print("Checking support approval...")
        if not request.context.approved:
            return StopTurn("Application approval missing")
        if mode == "failed":
            raise RuntimeError("Synthetic approval service failure")
        if mode == "cancelled":
            request.context.checking.set()
            await asyncio.Future[None]()
        return None

    candidate = (
        '{"case_id":"synthetic-42","message":"A specialist is reviewing your request."}'
        if mode != "invalid"
        else '{"case_id":"wrong-case","message":"A specialist is reviewing your request."}'
    )
    async with ScriptedResponses([text(candidate)]) as peer:
        agent = Agent(
            instructions="Draft a JSON support answer for case synthetic-42.",
            provider=peer.provider,
            before_complete=check_approval,
        )
        async with agent.session(context=app) as session:
            turn = await session.submit("Draft a support answer.", budget=RunBudget(max_requests=1))
            try:
                if mode == "cancelled":
                    await asyncio.wait_for(app.checking.wait(), timeout=10)
                    await turn.cancel()
                result = await turn.get_result()
                # Checking the completed value before any customer-visible publication.
                answer = SupportAnswer.model_validate_json(result.final_output or "")
                if answer.case_id != "synthetic-42" or not answer.message.strip():
                    raise ValueError("Support answer failed application checks")
            except TurnStoppedError:
                assert mode in ("rejected", "cancelled")
            except RuntimeError as error:
                assert mode == "failed" and str(error) == "Synthetic approval service failure"
            except ValueError as error:
                assert (
                    mode == "invalid" and str(error) == "Support answer failed application checks"
                )
            else:
                published.append(answer)
        assert len(peer.requests) == 1

    assert len(published) == (1 if mode == "accepted" else 0)
    if published:
        print("Accepted support answer:", published[0].model_dump_json())
    else:
        # Releasing only an application-owned status, without a draft or raw exception.
        print(f"Support outcome: {mode}; no answer released.")
    return published


async def accepted_sections() -> None:
    # The application checks each section independently. It can publish a section
    # before the next turn because later sections cannot change its facts.
    expected = (
        ("inventory", "7 units available"),
        ("dispatch", "2 orders packed"),
        ("delivery", "Delivery unconfirmed"),
    )
    published: list[tuple[str, str]] = []
    async with ScriptedResponses(
        [text("7 units available")],
        [text("2 orders packed")],
        [text("All orders delivered")],
    ) as peer:
        agent = Agent(instructions="Report one section at a time.", provider=peer.provider)
        async with agent.session() as session:
            for index, (section, verified_text) in enumerate(expected):
                assert published == list(expected[:index])
                assert len(peer.requests) == index
                turn = await session.submit(f"Report the verified {section} facts.")
                result = await turn.get_result()
                if result.final_output != verified_text:
                    print(f"Section withheld: {section}.")
                    break
                published.append((section, verified_text))
                print(f"Accepted {section}: {verified_text}")
        assert len(peer.requests) == len(expected)
    assert published == list(expected[:2])


async def same_turn_sections() -> None:
    published: dict[str, str] = {}
    private_candidates: list[str | None] = []
    facts = {"inventory": "7 units available", "delivery": "Delivery unconfirmed"}

    @function_tool
    def publish_section(section: Literal["inventory", "delivery"], content: str) -> str:
        """The application has been checking and publishing one independent section."""
        # Trusted application facts have been governing publication.
        if content != facts[section]:
            return "Section withheld."
        published[section] = content
        return "Section published."

    def next_section(_request: object) -> ScriptedResponse:
        # The peer has been checking delivery before its next generation response.
        assert published == {"inventory": "7 units available"}
        return ScriptedResponse(
            [
                tool_call(
                    "publish_section",
                    {"section": "delivery", "content": "All orders delivered"},
                    call_id="rejected-section",
                )
            ]
        )

    def final_proposal(_request: object) -> ScriptedResponse:
        assert published == {"inventory": "7 units available"}
        return ScriptedResponse([text("Provisional summary: all orders delivered.")])

    def reject_final(request: CompletionContext[None]) -> StopTurn:
        private_candidates.append(request.text)
        return StopTurn("Final answer has not been passing application checks")

    async with ScriptedResponses(
        [
            tool_call(
                "publish_section",
                {"section": "inventory", "content": "7 units available"},
                call_id="accepted-section",
            )
        ],
        next_section,
        final_proposal,
    ) as peer:
        agent = Agent(
            instructions="You have been publishing independently verified sections with publish_section.",
            provider=peer.provider,
            tools=[publish_section],
            before_complete=reject_final,
        )
        async with agent.session() as session:
            turn = await session.submit("I have been requesting inventory and delivery facts.")
            try:
                await turn.get_result()
            except TurnStoppedError:
                pass
            else:
                raise AssertionError("Final proposal has been unexpectedly passing")
        assert len(peer.requests) == 3
        assert peer.tool_output("accepted-section") == "Section published."
        assert peer.tool_output("rejected-section") == "Section withheld."
    assert private_candidates == ["Provisional summary: all orders delivered."]
    assert published == {"inventory": "7 units available"}
    print("Same-turn application publication:", published)


async def main() -> None:
    await revised_report()
    modes: tuple[SupportMode, ...] = ("accepted", "rejected", "cancelled", "failed", "invalid")
    for mode in modes:
        await support_answer(mode)
    await accepted_sections()
    await same_turn_sections()


if __name__ == "__main__":
    asyncio.run(main())
