from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..agent import Agent
from ..run import RunConfig
from .bootstrap_few_shot import _format_few_shot_messages
from .evaluation import evaluate_agent
from .types import LabeledExample, MetricFn, OptimizerResult


@dataclass
class BootstrapFewShotRandomSearch:
    """Random-search variant of few-shot example selection.

    This optimizer randomly samples subsets of training examples up to
    ``max_examples`` and selects the subset that maximizes validation score.

    Attributes:
        max_examples: Maximum number of few-shot examples to include.
        num_trials: Number of random subsets to try.
        sample_size: If provided, fix the subset size; otherwise sample a
            size uniformly in [1, max_examples].
        base_instructions: Optional system instructions to apply.
        metric: Optional custom metric; defaults to exact match.
        seed: PRNG seed for reproducibility.
    """

    max_examples: int = 4
    num_trials: int = 32
    sample_size: int | None = None
    base_instructions: str | None = None
    metric: MetricFn | None = None
    seed: int | None = 7

    async def fit(
        self,
        agent: Agent[Any],
        dataset: Iterable[LabeledExample],
        *,
        run_config: RunConfig | None = None,
        validation_split: float = 0.3,
        max_concurrency: int = 8,
    ) -> OptimizerResult:
        data = list(dataset)
        if not data:
            return OptimizerResult(
                call_model_input_filter=None,
                updated_instructions=self.base_instructions,
                selected_examples=[],
                score=0.0,
            )

        # Split into train/val
        split = max(1, int(len(data) * (1 - validation_split)))
        train = data[:split]
        val = data[split:] or data

        prng = random.Random(self.seed)

        best_subset: list[LabeledExample] = []
        best_score = -1.0

        # Always include an empty subset trial (baseline)
        trials = self.num_trials
        for i in range(trials + 1):
            if i == 0:
                subset: list[LabeledExample] = []
            else:
                if not train:
                    continue
                k = self.sample_size if self.sample_size is not None else prng.randint(1, min(self.max_examples, len(train)))
                k = max(1, min(k, len(train), self.max_examples))
                subset = prng.sample(train, k)

            # Build a filter that injects the few-shot examples and optional instructions
            few_shot_items = _format_few_shot_messages(subset)

            def call_model_input_filter(data):  # type: ignore[no-untyped-def]
                original = data.model_data
                input_items = list(few_shot_items) + list(original.input)
                instructions = (
                    self.base_instructions if self.base_instructions is not None else original.instructions
                )
                from ..run import ModelInputData

                return ModelInputData(input=input_items, instructions=instructions)

            cfg = RunConfig(call_model_input_filter=call_model_input_filter)

            eval_res = await evaluate_agent(
                agent,
                dataset=val,
                metric=self.metric,
                run_config=cfg,
                max_concurrency=max_concurrency,
            )
            score = eval_res.average
            if score > best_score:
                best_score = score
                best_subset = subset

        # Return an optimizer result that applies the best subset and instructions
        few_shot_items = _format_few_shot_messages(best_subset)

        def input_filter(data):  # type: ignore[no-untyped-def]
            original = data.model_data
            input_items = list(few_shot_items) + list(original.input)
            instructions = (
                self.base_instructions if self.base_instructions is not None else original.instructions
            )
            from ..run import ModelInputData

            return ModelInputData(input=input_items, instructions=instructions)

        return OptimizerResult(
            call_model_input_filter=input_filter,
            updated_instructions=self.base_instructions,
            selected_examples=best_subset,
            score=max(best_score, 0.0),
        )


