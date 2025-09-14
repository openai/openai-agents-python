from __future__ import annotations

import asyncio
import os

from agents import Agent, BootstrapFewShotRandomSearch, evaluate_agent

from .email_triage_greedy import EMAIL_CLASSIFICATION_INSTRUCTIONS, build_dataset, classify_metric


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY to run this example against OpenAI models.")

    agent = Agent(name="EmailTriageRS", instructions=EMAIL_CLASSIFICATION_INSTRUCTIONS)

    dataset = build_dataset()

    baseline = await evaluate_agent(agent, dataset=dataset, metric=classify_metric)
    print("Baseline avg:", baseline.average)

    opt = BootstrapFewShotRandomSearch(
        max_examples=4,
        num_trials=24,
        base_instructions=EMAIL_CLASSIFICATION_INSTRUCTIONS,
        seed=13,
    )
    result = await opt.fit(agent, dataset)
    cfg = result.attach_to_runconfig()

    improved = await evaluate_agent(agent, dataset=dataset, metric=classify_metric, run_config=cfg)
    print("Improved avg:", improved.average)
    print("Selected examples:", len(result.selected_examples))


if __name__ == "__main__":
    asyncio.run(main())


