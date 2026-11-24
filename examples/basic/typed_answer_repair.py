"""Bounded semantic repair through the existing final-output policy, offline."""

import asyncio
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel

from agents import (
    Agent,
    AgentHooks,
    ModelBehaviorError,
    ProposedFinalOutput,
    RunConfig,
    RunContextWrapper,
    Runner,
)
from agents.models.interface import Model
from agents.testing import ScriptedModel, assistant_message


class Verdict(BaseModel):
    decision: Literal["approve", "reject"]
    evidence_count: int


class RepairLimitError(RuntimeError):
    """The application declined to accept a candidate within its fixed budget."""


@dataclass
class Review:
    attempts: int = 0
    feedback: str = "Return a verdict supported by at least one evidence item."


def validate_candidate(
    context: RunContextWrapper[Review], proposal: ProposedFinalOutput[Review]
) -> Literal["finish", "continue"]:
    context.context.attempts += 1
    candidate = proposal.output
    if isinstance(candidate, Verdict) and candidate.evidence_count > 0:
        return "finish"
    if context.context.attempts >= 2:
        raise RepairLimitError("No acceptable verdict within two candidates")
    # Dynamic instructions give fixed feedback without copying the rejected text.
    context.context.feedback = "Repair the verdict: include a positive evidence_count."
    return "continue"


async def repair(
    model: Model | str | None, *, streamed: bool = False, run_config: RunConfig | None = None
) -> tuple[Verdict, Review]:
    context = Review()
    agent = Agent[Review](
        name="reviewer",
        model=model,
        output_type=Verdict,
        instructions=lambda ctx, agent: ctx.context.feedback,
        hooks=AgentHooks(final_output_policy=validate_candidate),
    )
    config = replace(run_config or RunConfig(), tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(
            agent, "Review the synthetic evidence.", context=context, run_config=config, max_turns=2
        )
        async for _ in result.stream_events():
            pass  # Raw events are not approved display.
    else:
        result = await Runner.run(
            agent, "Review the synthetic evidence.", context=context, run_config=config, max_turns=2
        )
    return result.final_output_as(Verdict), context


async def repair_schema(model: ScriptedModel) -> tuple[Verdict, int]:
    """Retry malformed synthetic answers at most twice without replaying bad text.

    This separate offline recipe uses a tool-free scripted model. Do not apply
    automatic new-run retries to a provider that may perform native side effects.
    Each attempt has no session, so a malformed candidate enters no next request.
    """
    for attempt in range(1, 3):
        try:
            result = await Runner.run(
                Agent(name="schema reviewer", model=model, output_type=Verdict),
                "Return JSON with decision approve/reject and positive evidence_count.",
                run_config=RunConfig(tracing_disabled=True),
                max_turns=1,
            )
        except ModelBehaviorError:
            # Parsing precedes final_output_policy. Never feed the error or raw
            # rejected text back into the next request or display it to a user.
            continue
        answer = result.final_output_as(Verdict)
        if answer.evidence_count > 0:
            return answer, attempt
    raise RepairLimitError("No acceptable typed verdict within two attempts")


async def main() -> None:
    model = ScriptedModel(
        [
            [assistant_message('{"decision":"approve","evidence_count":0}')],
            [assistant_message('{"decision":"reject","evidence_count":1}')],
        ]
    )
    answer, review = await repair(model)
    print(answer.model_dump_json(), f"Candidates: {review.attempts}")


if __name__ == "__main__":
    asyncio.run(main())
