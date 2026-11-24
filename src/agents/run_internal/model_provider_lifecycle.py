from __future__ import annotations

import asyncio
from copy import copy
from typing import Any

from .._redaction import current_redactor
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
    detail = run_config.tracing.get("operation_detail") if run_config.tracing else None
    if detail not in (None, "metadata", "content"):
        raise ValueError("operation_detail must be 'metadata' or 'content'")
    redactor = current_redactor()
    if redactor is None:
        redactor = run_config.redact_text
    elif run_config.redact_text is not None and run_config.redact_text is not redactor:
        raise ValueError("A nested run cannot replace its parent's redact_text policy.")
    if redactor is not None or (detail == "metadata" and run_config.trace_include_sensitive_data):
        # Route every existing capture decision through the run's sensitive-data gate.
        run_config = copy(run_config)
        run_config.trace_include_sensitive_data = False
        if redactor is not None:
            run_config.redact_text = redactor
            run_config.tracing = {**(run_config.tracing or {}), "operation_detail": "metadata"}
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
