"""Per-agent LangSmith outcome traces."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

from agents.memory import await_session_mutation
from agents.plugins import Plugin, PluginHooks, RunOutcome


class _Client(Protocol):
    def create_run(
        self, *, name: str, inputs: dict[str, Any], run_type: Literal["chain"], **kwargs: Any
    ) -> None: ...


def langsmith(client: _Client, *, project_name: str = "Agents") -> Plugin[Any]:
    """Send the status and duration of each submission to an app-owned LangSmith Client."""
    if not project_name.strip():
        raise ValueError("Choose a non-empty LangSmith project name.")

    async def record(outcome: RunOutcome) -> None:
        end = datetime.now(timezone.utc)
        write = asyncio.to_thread(
            client.create_run,
            id=uuid4(),
            project_name=project_name,
            name="Agent run",
            run_type="chain",
            start_time=end - timedelta(seconds=outcome.elapsed_seconds),
            end_time=end,
            inputs={},
            outputs={"status": outcome.status},
            error=outcome.status if outcome.status in ("failed", "cancelled") else None,
        )

        # Finish using the app-owned client before cancellation reaches its owner.
        await await_session_mutation(write)

    return Plugin(name="langsmith", hooks=PluginHooks(on_run_end=record))
