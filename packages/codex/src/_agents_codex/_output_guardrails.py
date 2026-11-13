"""Converted-output guardrail settlement on already selected application checks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ._lifecycle import _join
from .completion import CompletionContext
from .guardrails import GuardrailRecord, GuardrailResult, GuardrailTripwire, OutputGuardrail

if TYPE_CHECKING:
    from .agent import Agent


async def _settle_output_guardrails(
    checks: Sequence[OutputGuardrail[Any]],
    agent: Agent[Any, Any],
    request: CompletionContext[Any],
    records: list[GuardrailRecord],
    invoke: Callable[[OutputGuardrail[Any], CompletionContext[Any]], Awaitable[Any]],
    active: Callable[[], bool],
    *,
    revoke: Callable[[], None] | None = None,
) -> None:
    async def check(guardrail: OutputGuardrail[Any]) -> GuardrailRecord:
        result = await invoke(
            guardrail, replace(request, final_output=deepcopy(request.final_output))
        )
        if not active():
            raise asyncio.CancelledError()
        if not isinstance(result, GuardrailResult):
            raise TypeError("Guardrail callback must return GuardrailResult")
        record = GuardrailRecord(
            kind="output",
            name=guardrail.name,
            result=result,
            agent=agent,
            session_id=request.session_id,
            turn_id=request.turn_id,
            subagent_id=request.subagent_id,
            tool_call=request.tool_call,
        )
        records.append(record)
        return record

    tasks = [asyncio.create_task(check(guardrail)) for guardrail in checks]
    tripped = None
    try:
        for done in asyncio.as_completed(tasks):
            record = await done
            if record.result.tripwire_triggered:
                tripped = record
                break
    finally:
        if revoke is not None:
            revoke()
        for task in tasks:
            if not task.done():
                task.cancel()
        await _join(asyncio.gather(*tasks, return_exceptions=True))
    if tripped is not None:
        raise GuardrailTripwire(tripped, tuple(records))
