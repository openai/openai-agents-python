from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterable, Sequence
from typing import Any

from ..agent import Agent
from ..run import RunConfig, Runner
from .assessment import extract_prediction_for_metric
from .types import AsyncMetricFn, EvalResult, LabeledExample, MetricFn


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
    return extract_prediction_for_metric(result)


async def evaluate_agent(
    agent: Agent[Any],
    *,
    dataset: Iterable[LabeledExample],
    metric: MetricFn | AsyncMetricFn | None = None,
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
    # Support async metrics (e.g., LLM-as-a-judge)
    async def score_one(pred: Any, expected: Any) -> float:
        # Handle async metric functions or async __call__ on objects
        metric_fn = metric
        try:
            if inspect.iscoroutinefunction(metric_fn):
                from typing import cast
                return await cast(AsyncMetricFn, metric_fn)(pred, expected)
            if callable(metric_fn) and inspect.iscoroutinefunction(metric_fn.__call__):
                from typing import cast
                return await cast(AsyncMetricFn, metric_fn)(pred, expected)
            from typing import cast
            result = cast(MetricFn, metric_fn)(pred, expected)
            return float(result)
        except Exception:
            return 0.0

    scores = await asyncio.gather(
        *(score_one(pred, ex.expected) for pred, ex in zip(predictions, examples))
    )
    return EvalResult(scores=scores, predictions=predictions)


async def cross_validate_agent(
    agent: Agent[Any],
    *,
    dataset: Sequence[LabeledExample],
    metric: MetricFn | None = None,
    run_config: RunConfig | None = None,
    k_folds: int = 5,
    max_concurrency: int = 8,
) -> list[EvalResult]:
    """K-fold cross validation over a dataset.

    Splits the dataset into ``k_folds`` contiguous folds and evaluates the
    agent on each held-out fold. Returns a list of per-fold ``EvalResult``.
    """
    if k_folds <= 1:
        return [
            await evaluate_agent(
                agent,
                dataset=dataset,
                metric=metric,
                run_config=run_config,
                max_concurrency=max_concurrency,
            )
        ]

    n = len(dataset)
    if n == 0:
        return [EvalResult(scores=[], predictions=[])]

    fold_size = max(1, n // k_folds)
    results: list[EvalResult] = []
    for i in range(k_folds):
        start = i * fold_size
        end = n if i == k_folds - 1 else min(n, (i + 1) * fold_size)
        val = list(dataset[start:end])
        if not val:
            continue
        res = await evaluate_agent(
            agent,
            dataset=val,
            metric=metric,
            run_config=run_config,
            max_concurrency=max_concurrency,
        )
        results.append(res)
    return results

