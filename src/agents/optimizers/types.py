from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Awaitable

from ..run import RunConfig


@dataclass
class LabeledExample:
    """Simple labeled example for evaluation/optimization.

    Attributes:
        input: The user input string to feed the agent.
        expected: The expected final output from the agent.
    """

    input: str
    expected: Any


class MetricFn(Protocol):
    def __call__(self, predicted: Any, expected: Any) -> float: ...


class AsyncMetricFn(Protocol):
    async def __call__(self, predicted: Any, expected: Any) -> float: ...


@dataclass
class EvalResult:
    """Evaluation results for a set of labeled examples."""

    scores: list[float]
    predictions: list[Any]

    @property
    def average(self) -> float:
        return sum(self.scores) / len(self.scores) if self.scores else 0.0


@dataclass
class OptimizerResult:
    """Result of an optimization pass.

    Provides a convenient method to attach the learned augmentation to
    a ``RunConfig`` for future runs.
    """

    call_model_input_filter: Any | None
    """A callable suitable for ``RunConfig.call_model_input_filter`` (or None)."""

    updated_instructions: str | None
    """Optional updated system instructions to apply before model calls."""

    selected_examples: list[LabeledExample]
    """The examples selected by the optimizer (e.g., few-shot demonstrations)."""

    score: float
    """Aggregate score (e.g., average validation metric) achieved by the optimized config."""

    def attach_to_runconfig(self, run_config: RunConfig | None = None) -> RunConfig:
        """Return a new RunConfig that applies this optimizer's augmentation.

        If a RunConfig is provided, it is shallow-copied and updated.
        """
        from dataclasses import replace

        run_config = run_config or RunConfig()

        def combined_filter(data):  # type: ignore[no-untyped-def]
            # If we have our own filter, call it; otherwise, pass through.
            if self.call_model_input_filter is None:
                return data.model_data

            result = self.call_model_input_filter(data)  # type: ignore[misc]
            # If we also have updated instructions, override them.
            if self.updated_instructions is not None and hasattr(result, "instructions"):
                result.instructions = self.updated_instructions
            return result

        # If there is already a filter, compose them: ours runs first then user filter.
        existing = run_config.call_model_input_filter

        if existing is None:
            new_filter = combined_filter
        else:
            def composed_filter(data):  # type: ignore[no-untyped-def]
                intermediate = combined_filter(data)
                # Re-wrap with the same payload shape expected by ``existing``
                from ..run import CallModelData, ModelInputData

                payload = CallModelData(  # type: ignore[type-arg]
                    model_data=intermediate,
                    agent=data.agent,
                    context=data.context,
                )
                return existing(payload)  # type: ignore[misc]

            new_filter = composed_filter

        return replace(run_config, call_model_input_filter=new_filter)

