from __future__ import annotations

import asyncio
from typing import Any

from ..logger import log_model_action_warning, logger
from ..models.interface import ModelProvider
from ..run_config import RunConfig, _coerce_run_config


def _normalize_run_config_for_runner(
    value: RunConfig | dict[str, Any] | None,
) -> tuple[RunConfig, bool]:
    """Normalize a run config and report whether Runner created its model provider."""
    owns_model_provider = value is None or (
        isinstance(value, dict) and "model_provider" not in value
    )
    run_config = RunConfig() if value is None else _coerce_run_config(value)
    return run_config, owns_model_provider


async def _close_runner_owned_model_provider(model_provider: ModelProvider) -> None:
    """Finish provider cleanup despite repeated cancellation, then restore cancellation."""

    async def close() -> None:
        try:
            await model_provider.aclose()
        except Exception as error:
            log_model_action_warning(
                logger,
                "Failed to close model provider created for run",
                error,
            )

    close_task = asyncio.create_task(close())
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                continue
        if not close_task.cancelled():
            close_task.result()
        raise
