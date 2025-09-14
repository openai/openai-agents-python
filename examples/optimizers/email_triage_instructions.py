from __future__ import annotations

import asyncio
import os

from agents import Agent, InstructionOptimizer, evaluate_agent

from .email_triage_greedy import EMAIL_CLASSIFICATION_INSTRUCTIONS, build_dataset, classify_metric


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY to run this example against OpenAI models.")

    agent = Agent(name="EmailTriageInstr", instructions=EMAIL_CLASSIFICATION_INSTRUCTIONS)
    dataset = build_dataset()

    base = await evaluate_agent(agent, dataset=dataset, metric=classify_metric)
    print("Baseline avg:", base.average)

    candidates = [
        EMAIL_CLASSIFICATION_INSTRUCTIONS,
        (
            "Classify the email into one of: billing, technical, sales, account, urgent. "
            "Respond with only the label."
        ),
        (
            "You triage emails for a support desk. Output exactly one label from: "
            "billing, technical, sales, account, urgent."
        ),
    ]
    opt = InstructionOptimizer(candidates=candidates)
    res = await opt.fit(agent, dataset)
    cfg = res.attach_to_runconfig()

    improved = await evaluate_agent(agent, dataset=dataset, metric=classify_metric, run_config=cfg)
    print("Improved avg:", improved.average)
    print("Chosen instructions:\n", res.updated_instructions)


if __name__ == "__main__":
    asyncio.run(main())


