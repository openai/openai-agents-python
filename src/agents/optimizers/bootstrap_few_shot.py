from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ..agent import Agent
from ..items import ItemHelpers
from ..run import CallModelData, ModelInputData, RunConfig
from ..run_context import RunContextWrapper
from ..util._types import MaybeAwaitable
from .evaluation import evaluate_agent
from .types import LabeledExample, MetricFn, OptimizerResult


def _format_few_shot_messages(examples: Sequence[LabeledExample]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for ex in examples:
        messages.append({"role": "user", "content": ex.input})
        messages.append({"role": "assistant", "content": str(ex.expected)})
    return messages


@dataclass
class BootstrapFewShot:
    """Bootstrap a small set of few-shot demonstrations by greedy selection.

    This is a lightweight approximation of DSPy's BootstrapFewShot that works with
    the Agents SDK. We select up to ``max_examples`` training items that maximize
    validation performance on the provided dataset using a simple greedy sweep.

    We then provide those examples to the model via ``RunConfig.call_model_input_filter``
    by prepending them to the input messages. Optionally, ``base_instructions`` can be
    provided to override or provide system instructions during evaluation.
    """

    max_examples: int = 4
    base_instructions: str | None = None
    metric: MetricFn | None = None

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
        val = data[split:] or data  # fallback: if split yields empty val, reuse full data

        # Greedy selection: add examples one by one if they improve val score
        selected: list[LabeledExample] = []
        best_score = -1.0

        for _ in range(min(self.max_examples, len(train))):
            best_candidate: LabeledExample | None = None
            best_candidate_score = best_score

            for ex in train:
                if ex in selected:
                    continue
                candidate = selected + [ex]
                candidate_filter = self._build_filter(candidate)
                cfg = RunConfig(call_model_input_filter=candidate_filter)
                if self.base_instructions is not None:
                    # The filter will override instructions, but also set here for clarity
                    cfg = cfg

                eval_res = await evaluate_agent(
                    agent,
                    dataset=val,
                    metric=self.metric,
                    run_config=cfg,
                    max_concurrency=max_concurrency,
                )
                score = eval_res.average
                if score > best_candidate_score:
                    best_candidate_score = score
                    best_candidate = ex

            if best_candidate is not None and best_candidate_score > best_score:
                selected.append(best_candidate)
                best_score = best_candidate_score
            else:
                break

        return OptimizerResult(
            call_model_input_filter=self._build_filter(selected),
            updated_instructions=self.base_instructions,
            selected_examples=selected,
            score=max(best_score, 0.0),
        )

    def _build_filter(self, examples: Sequence[LabeledExample]):
        few_shot_items = _format_few_shot_messages(examples)
        base_instructions = self.base_instructions

        def call_model_input_filter(data: CallModelData[Any]) -> ModelInputData:  # type: ignore[misc]
            # Prepend the few-shot pairs to the model input messages. Also optionally set instructions.
            original = data.model_data
            input_items = list(few_shot_items) + list(original.input)
            instructions = base_instructions if base_instructions is not None else original.instructions
            return ModelInputData(input=input_items, instructions=instructions)

        return call_model_input_filter

