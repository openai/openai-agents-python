from decimal import Decimal

import pytest

from agents.testing import ModelStep, ScriptedModel, assistant_message
from agents.usage import Usage
from examples.agent_patterns.estimated_spend import DemoProvider, Rates, estimate_usd, run_batch


def response(raw_usage):
    return ModelStep(
        output=[assistant_message("answer")],
        usage=Usage(requests=1, input_tokens=1000, output_tokens=500, total_tokens=1500),
        raw_usage=raw_usage,
    )


async def test_changes_models_and_stops_before_next_run_after_threshold():
    economy = ScriptedModel([response({"input_tokens": 1000, "output_tokens": 500})])
    review = ScriptedModel([response({"input_tokens": 1000, "output_tokens": 500})])
    result = await run_batch(
        [("economy", "draft"), ("review", "review"), ("economy", "never start")],
        provider=DemoProvider({"economy": economy, "review": review}),
        rates={"economy": Rates(Decimal(1), Decimal(2)), "review": Rates(Decimal(3), Decimal(6))},
        stop_at_usd=Decimal("0.005"),
    )
    assert result.known_usd == Decimal("0.008")
    assert result.outputs == ["answer", "answer"]
    assert result.stopped == "threshold"
    assert len(economy.calls) == len(review.calls) == 1
    assert not economy.calls[0].tools and not review.calls[0].tools


@pytest.mark.parametrize("raw_usage", [None, {}, {"input_tokens": 1000}])
async def test_missing_usage_stops_even_when_normalized_usage_has_counts(raw_usage):
    model = ScriptedModel([response(raw_usage)])
    result = await run_batch(
        [("model", "first"), ("model", "never start")],
        provider=DemoProvider({"model": model}),
        rates={"model": Rates(Decimal(1), Decimal(2))},
        stop_at_usd=Decimal(1),
    )
    assert result.stopped == "unknown_usage"
    assert result.known_usd == 0
    assert result.outputs == ["answer"]
    assert len(model.calls) == 1


async def test_reported_zero_allows_next_job_and_retains_known_subtotal():
    model = ScriptedModel(
        [
            response({"input_tokens": 0, "output_tokens": 0}),
            response({"input_tokens": 1000, "output_tokens": 500}),
            response(None),
        ]
    )
    result = await run_batch(
        [("model", str(index)) for index in range(4)],
        provider=DemoProvider({"model": model}),
        rates={"model": Rates(Decimal(1), Decimal(2))},
        stop_at_usd=Decimal(1),
    )
    assert result.stopped == "unknown_usage"
    assert result.known_usd == Decimal("0.002")
    assert len(result.outputs) == len(model.calls) == 3


async def test_zero_threshold_starts_no_request():
    model = ScriptedModel([])
    result = await run_batch(
        [("model", "never start")],
        provider=DemoProvider({"model": model}),
        rates={"model": Rates(Decimal(1), Decimal(2))},
        stop_at_usd=Decimal(0),
    )
    assert result.stopped == "threshold"
    assert result.outputs == []
    assert not model.calls


@pytest.mark.parametrize("count", [None, -1, True, "10"])
def test_invalid_reported_count_remains_unknown(count):
    assert (
        estimate_usd({"input_tokens": count, "output_tokens": 10}, Rates(Decimal(1), Decimal(2)))
        is None
    )
