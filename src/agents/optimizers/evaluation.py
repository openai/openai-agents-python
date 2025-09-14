from __future__ import annotations

import asyncio
from typing import Any, Iterable

from ..agent import Agent
from ..run import RunConfig, Runner
from ..run_context import RunContextWrapper
from ..util._types import MaybeAwaitable
from .types import EvalResult, LabeledExample, MetricFn


def exact_match_metric(predicted: Any, expected: Any) -> float:
    """Simple exact-match metric returning 1.0 if equal, 0.0 otherwise."""
    return 1.0 if predicted == expected else 0.0


async def _run_single_example(
    agent: Agent[Any],
    example: LabeledExample,
    *,
    run_config: RunConfig | None,
) -> Any:
    result = await Runner.run(agent, input=example.input, run_config=run_config)
    return result.final_output


async def evaluate_agent(
    agent: Agent[Any],
    *,
    dataset: Iterable[LabeledExample],
    metric: MetricFn | None = None,
    run_config: RunConfig | None = None,
    max_concurrency: int = 8,
) -> EvalResult:
    """Evaluate an agent on a dataset of labeled examples.

    Runs the agent on each example (optionally with a ``RunConfig``),
    computes per-example scores with ``metric`` (default: exact match), and
    returns predictions and scores along with the average.
    """
    metric = metric or exact_match_metric
    examples = list(dataset)

    semaphore = asyncio.Semaphore(max_concurrency)
    predictions: list[Any] = [None] * len(examples)

    async def run_and_store(i: int, ex: LabeledExample) -> None:
        async with semaphore:
            predictions[i] = await _run_single_example(agent, ex, run_config=run_config)

    await asyncio.gather(*(run_and_store(i, ex) for i, ex in enumerate(examples)))
    scores = [metric(pred, ex.expected) for pred, ex in zip(predictions, examples)]
    return EvalResult(scores=scores, predictions=predictions)

