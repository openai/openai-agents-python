from __future__ import annotations

import asyncio
import os
from typing import Any

from agents import Agent, BootstrapFewShot, LabeledExample, evaluate_agent


def classify_metric(predicted: Any, expected: Any) -> float:
    """Returns 1.0 if the predicted text contains the expected label (case-insensitive).

    This metric is tolerant to extra text in the model's response.
    """
    if not isinstance(predicted, str):
        return 0.0
    return 1.0 if str(expected).lower() in predicted.lower() else 0.0


EMAIL_CLASSIFICATION_INSTRUCTIONS = (
    "You are an email triage assistant.\n"
    "Categorize the user's email into exactly one label from this set: "
    "['billing', 'technical', 'sales', 'account', 'urgent'].\n"
    "Return only the label, no extra text."
)


def build_dataset() -> list[LabeledExample]:
    return [
        LabeledExample(
            input=(
                "Subject: Invoice discrepancy\n\n"
                "Hi, my latest invoice has an unexpected charge I don't recognize."
            ),
            expected="billing",
        ),
        LabeledExample(
            input=(
                "Subject: App keeps crashing\n\n"
                "The mobile app closes every time I open settings on Android 14."
            ),
            expected="technical",
        ),
        LabeledExample(
            input=(
                "Subject: Upgrade plan\n\n"
                "We're evaluating your enterprise plan and need pricing details."
            ),
            expected="sales",
        ),
        LabeledExample(
            input=(
                "Subject: Reset my password\n\n"
                "I can't log in and need help resetting my password."
            ),
            expected="account",
        ),
        LabeledExample(
            input=(
                "Subject: Service down for launch!\n\n"
                "Our site launch is in 2 hours and your API is failing with 500s."
            ),
            expected="urgent",
        ),
        LabeledExample(
            input=(
                "Subject: Wrong tax ID on invoice\n\n"
                "The tax ID on invoice #8291 is incorrect and needs updating."
            ),
            expected="billing",
        ),
        LabeledExample(
            input=(
                "Subject: OAuth redirect issue\n\n"
                "I'm getting an invalid redirect URI error during OAuth signin."
            ),
            expected="technical",
        ),
        LabeledExample(
            input=(
                "Subject: Bulk licenses\n\n"
                "Do you offer discounts for 250 seats and multi-year contracts?"
            ),
            expected="sales",
        ),
        LabeledExample(
            input=(
                "Subject: Delete my account\n\n"
                "Please permanently delete my account and confirm when done."
            ),
            expected="account",
        ),
        LabeledExample(
            input=(
                "Subject: Production outage\n\n"
                "Payment webhooks are failing for all customers since 15 minutes ago."
            ),
            expected="urgent",
        ),
        LabeledExample(
            input=(
                "Subject: VAT question\n\n"
                "How do you handle VAT for customers in the EU?"
            ),
            expected="billing",
        ),
        LabeledExample(
            input=(
                "Subject: 2FA not working\n\n"
                "My authenticator codes are rejected. Is there a backup method?"
            ),
            expected="account",
        ),
    ]


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY to run this example against OpenAI models.")

    agent = Agent(name="EmailTriage", instructions=EMAIL_CLASSIFICATION_INSTRUCTIONS)

    dataset = build_dataset()

    baseline = await evaluate_agent(agent, dataset=dataset, metric=classify_metric)
    print("Baseline avg:", baseline.average)

    opt = BootstrapFewShot(max_examples=4, base_instructions=EMAIL_CLASSIFICATION_INSTRUCTIONS)
    result = await opt.fit(agent, dataset)
    cfg = result.attach_to_runconfig()

    improved = await evaluate_agent(agent, dataset=dataset, metric=classify_metric, run_config=cfg)
    print("Improved avg:", improved.average)
    print("Selected examples:", len(result.selected_examples))


if __name__ == "__main__":
    asyncio.run(main())


