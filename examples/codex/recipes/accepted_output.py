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
)
from agents.observation import OutputTextDelta
from agents.testing import ScriptedResponse, ScriptedResponses, event, text


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


async def main() -> None:
    await revised_report()
    modes: tuple[SupportMode, ...] = ("accepted", "rejected", "cancelled", "failed", "invalid")
    for mode in modes:
        await support_answer(mode)


if __name__ == "__main__":
    asyncio.run(main())
