from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

from ..agent import Agent
from ..run import RunConfig
from .evaluation import evaluate_agent
from .types import LabeledExample, MetricFn, OptimizerResult

CandidateGenerator = Callable[[list[LabeledExample]], list[str]]


@dataclass
class InstructionOptimizer:
    """Optimize the system instructions via candidate search.

    This optimizer evaluates a pool of instruction candidates and returns the
    one that maximizes validation performance on the provided dataset.

    Attributes:
        candidates: A static list of instruction strings to try. If not
            provided, ``generate_candidates`` must be set.
        generate_candidates: A function that, given a training set, returns a
            list of instruction candidates (e.g., heuristics or model-proposed).
        metric: The metric to optimize; defaults to exact match.
    """

    candidates: list[str] | None = None
    generate_candidates: CandidateGenerator | None = None
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
                updated_instructions=None,
                selected_examples=[],
                score=0.0,
            )

        split = max(1, int(len(data) * (1 - validation_split)))
        train = data[:split]
        val = data[split:] or data

        # Build the candidate set.
        candidate_instructions: list[str] = list(self.candidates or [])
        if not candidate_instructions and self.generate_candidates is not None:
            candidate_instructions = list(self.generate_candidates(train))

        # If still empty, nothing to optimize.
        if not candidate_instructions:
            return OptimizerResult(
                call_model_input_filter=None,
                updated_instructions=None,
                selected_examples=[],
                score=0.0,
            )

        best_instr: str | None = None
        best_score = -1.0

        for instr in candidate_instructions:
            # Apply via RunConfig composition using OptimizerResult helper later.
            from ..run import CallModelData, ModelInputData

            def call_model_input_filter(
                data: CallModelData[Any], *, _instr: str = instr
            ) -> ModelInputData:
                original = data.model_data
                return ModelInputData(input=original.input, instructions=_instr)

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
                best_instr = instr

        if best_instr is None:
            return OptimizerResult(
                call_model_input_filter=None,
                updated_instructions=None,
                selected_examples=[],
                score=0.0,
            )

        # Provide a pass-through filter (no change) and set updated_instructions.
        def passthrough(data):
            return data.model_data

        return OptimizerResult(
            call_model_input_filter=passthrough,
            updated_instructions=best_instr,
            selected_examples=[],
            score=max(best_score, 0.0),
        )


