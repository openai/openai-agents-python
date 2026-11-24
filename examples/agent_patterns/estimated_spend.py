"""Estimate reported token spend in application code, without SDK pricing callbacks.

Run offline with ``python -m examples.agent_patterns.estimated_spend``.
The rates below are fictional dollars per million tokens, not provider prices.
Each job makes one tool-free model call in a separate Runner run.
The application chooses the model and its rates before that run.
This does not implement per-turn model selection inside a run.

The application stops before the next run after reaching its estimate threshold
or receiving unknown usage. A completed request can exceed the threshold.
This is not a billing cap. Retries, failed requests, hidden native work, tool
charges, caching discounts, and other clients can make actual charges differ.
Exceptions propagate. Reconcile their potentially unreported charges before resuming.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.models.interface import Model, ModelProvider
from agents.testing import ModelStep, ScriptedModel, assistant_message
from agents.usage import Usage


@dataclass(frozen=True)
class Rates:
    input_per_million: Decimal
    output_per_million: Decimal


def estimate_usd(raw_usage: Mapping[str, Any] | None, rates: Rates) -> Decimal | None:
    """Estimate Responses-shaped usage. Missing or invalid counts mean unknown, never zero.

    Other provider schemas need their own explicit mapping.
    All input tokens use the supplied input rate.
    Output includes reasoning tokens without counting them again.
    """
    if raw_usage is None:
        return None
    input_tokens = raw_usage.get("input_tokens")
    output_tokens = raw_usage.get("output_tokens")
    if (
        type(input_tokens) is not int
        or type(output_tokens) is not int
        or input_tokens < 0
        or output_tokens < 0
    ):
        return None
    return (
        Decimal(input_tokens) * rates.input_per_million
        + Decimal(output_tokens) * rates.output_per_million
    ) / Decimal(1_000_000)


@dataclass(frozen=True)
class BatchEstimate:
    known_usd: Decimal
    outputs: list[Any]
    stopped: Literal["completed", "threshold", "unknown_usage"]


async def run_batch(
    jobs: Sequence[tuple[str, str]],
    *,
    provider: ModelProvider,
    rates: Mapping[str, Rates],
    stop_at_usd: Decimal,
) -> BatchEstimate:
    """Stop admission of the next job using only estimates from completed responses.

    Model names and rates belong to this application.
    Provider aliases or routing changes require matching rate updates.
    Keep each run tool-free. Adding a model_selector override also requires
    tracking the effective model for pricing.
    The caller owns the provider and closes it after the batch.
    """
    known_usd = Decimal(0)
    outputs: list[Any] = []
    for model_name, prompt in jobs:
        if known_usd >= stop_at_usd:
            return BatchEstimate(known_usd, outputs, "threshold")
        model_rates = rates[model_name]
        result = await Runner.run(
            Agent(
                name="Single answer",
                model=model_name,
                model_settings=ModelSettings(preserve_raw_usage=True),
            ),
            prompt,
            max_turns=1,
            run_config=RunConfig(model_provider=provider, tracing_disabled=True),
        )
        outputs.append(result.final_output)
        estimates = [
            estimate_usd(response.raw_usage, model_rates) for response in result.raw_responses
        ]
        known_usd += sum((value for value in estimates if value is not None), Decimal(0))
        if not estimates or any(value is None for value in estimates):
            return BatchEstimate(known_usd, outputs, "unknown_usage")
    return BatchEstimate(known_usd, outputs, "completed")


class DemoProvider(ModelProvider):
    """Supply deterministic offline responses through the public testing API."""

    def __init__(self, models: dict[str, Model]):
        self.models = models

    def get_model(self, model_name: str | None) -> Model:
        assert model_name is not None
        return self.models[model_name]


async def main() -> None:
    rates = {
        "economy": Rates(Decimal("1"), Decimal("2")),
        "review": Rates(Decimal("3"), Decimal("6")),
    }
    provider = DemoProvider(
        {
            name: ScriptedModel(
                [
                    ModelStep(
                        output=[assistant_message(f"Answer from {name}")],
                        usage=Usage(
                            requests=1, input_tokens=1000, output_tokens=500, total_tokens=1500
                        ),
                        raw_usage={"input_tokens": 1000, "output_tokens": 500},
                    )
                ]
            )
            for name in rates
        }
    )
    unknown_provider = DemoProvider(
        {"economy": ScriptedModel([[assistant_message("No usage reported")]])}
    )
    try:
        result = await run_batch(
            [
                ("economy", "Draft an answer"),
                ("review", "Review it"),
                ("economy", "Another answer"),
            ],
            provider=provider,
            rates=rates,
            stop_at_usd=Decimal("0.005"),
        )
        print(
            f"Known estimate: ${result.known_usd}. Answers: {len(result.outputs)}. Stop: {result.stopped}."
        )
        unknown = await run_batch(
            [("economy", "Usage unavailable"), ("review", "Do not start")],
            provider=unknown_provider,
            rates=rates,
            stop_at_usd=Decimal("0.005"),
        )
        print(f"Known subtotal: ${unknown.known_usd}. Total unknown. Stop: {unknown.stopped}.")
    finally:
        await provider.aclose()
        await unknown_provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
