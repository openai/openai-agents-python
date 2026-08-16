from __future__ import annotations

import asyncio

import pytest

from agents.models.interface import Model
from agents.models.openai_provider import OpenAIProvider


class _CloseTrackingModel(Model):
    """Fake model whose close() raises on demand and records invocation."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        if self._error is not None:
            raise self._error

    # Unused Model machinery for these tests
    async def get_response(
        self,
        agent_runner,  # noqa: ANN001
        system_instructions,  # noqa: ANN001
        input,  # noqa: ANN001
        model_settings,  # noqa: ANN001
        tools,  # noqa: ANN001
        output_schema,  # noqa: ANN001
        previous_response_id,  # noqa: ANN001
        trace_include_sensitive_data,  # noqa: ANN001
    ):
        raise NotImplementedError

    async def stream_response(
        self,
        agent_runner,  # noqa: ANN001
        system_instructions,  # noqa: ANN001
        input,  # noqa: ANN001
        model_settings,  # noqa: ANN001
        tools,  # noqa: ANN001
        output_schema,  # noqa: ANN001
        previous_response_id,  # noqa: ANN001
        trace_include_sensitive_data,  # noqa: ANN001
    ):
        raise NotImplementedError
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_close_models_closes_all_and_raises_first_error():
    first = _CloseTrackingModel(RuntimeError("first close failed"))
    second = _CloseTrackingModel()

    with pytest.raises(RuntimeError, match="first close failed"):
        await OpenAIProvider._close_models(None, [first, second])

    assert first.closed
    assert second.closed, "a failing close must not leak the remaining models"


@pytest.mark.asyncio
async def test_close_models_reraises_without_cancelling_others():
    failing = _CloseTrackingModel(ValueError("boom"))
    also_failing = _CloseTrackingModel(RuntimeError("second"))

    with pytest.raises(ValueError, match="boom"):
        await OpenAIProvider._close_models(None, [failing, also_failing])

    assert failing.closed and also_failing.closed


@pytest.mark.asyncio
async def test_close_models_all_succeed():
    models = [_CloseTrackingModel() for _ in range(3)]

    await OpenAIProvider._close_models(None, models)

    assert all(m.closed for m in models)


@pytest.mark.asyncio
async def test_close_ws_models_same_loop_uses_resilient_close():
    provider = object.__new__(OpenAIProvider)
    first = _CloseTrackingModel(RuntimeError("ws close failed"))
    second = _CloseTrackingModel()
    loop = asyncio.get_running_loop()

    with pytest.raises(RuntimeError, match="ws close failed"):
        await provider._close_ws_models_for_loop(loop, [first, second], loop)

    assert first.closed and second.closed
