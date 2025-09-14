from __future__ import annotations

import asyncio
from typing import Any, cast

from pydantic import BaseModel

from agents import Agent, BootstrapFewShot, LabeledExample, evaluate_agent
from agents.optimizers import MetricFn


class CalendarEvent(BaseModel):
    name: str
    date: str
    participants: list[str]


INSTR = (
    "Extract a structured CalendarEvent from the user's message. "
    "Respond in the exact JSON shape: {name: str, date: str, participants: list[str]}"
)


def build_dataset() -> list[LabeledExample]:
    return [
        LabeledExample(
            input="Lunch with Alice next Tuesday at 12:30pm",
            expected=CalendarEvent(name="Lunch with Alice", date="next Tuesday 12:30pm", participants=["Alice"]),
        ),
        LabeledExample(
            input="Team sync on 2025-10-03 10:00 with Bob and Carol",
            expected=CalendarEvent(name="Team sync", date="2025-10-03 10:00", participants=["Bob", "Carol"]),
        ),
        LabeledExample(
            input="Dentist appointment tomorrow 9am",
            expected=CalendarEvent(name="Dentist appointment", date="tomorrow 9am", participants=[]),
        ),
        LabeledExample(
            input="Project kickoff Friday 2pm with Dana",
            expected=CalendarEvent(name="Project kickoff", date="Friday 2pm", participants=["Dana"]),
        ),
    ]


def event_metric(pred: Any, exp: Any) -> float:
    if not isinstance(pred, dict) and not hasattr(pred, "model_dump"):
        return 0.0
    try:
        p = pred if isinstance(pred, dict) else pred.model_dump()
        e = exp if isinstance(exp, dict) else exp.model_dump()
        score = 0.0
        score += 1.0 if p.get("name") == e.get("name") else 0.0
        score += 1.0 if p.get("date") == e.get("date") else 0.0
        score += 1.0 if list(p.get("participants", [])) == list(e.get("participants", [])) else 0.0
        return score / 3.0
    except Exception:
        return 0.0


async def main() -> None:
    agent = Agent(
        name="Calendar extractor",
        instructions=INSTR,
        output_type=CalendarEvent,
    )

    dataset = build_dataset()
    metric_fn: MetricFn = cast(MetricFn, event_metric)
    baseline = await evaluate_agent(agent, dataset=dataset, metric=metric_fn)
    print("Baseline avg:", baseline.average)

    opt = BootstrapFewShot(max_examples=3, base_instructions=INSTR)
    res = await opt.fit(agent, dataset)
    cfg = res.attach_to_runconfig()

    improved = await evaluate_agent(agent, dataset=dataset, metric=metric_fn, run_config=cfg)
    print("Improved avg:", improved.average)


if __name__ == "__main__":
    asyncio.run(main())


