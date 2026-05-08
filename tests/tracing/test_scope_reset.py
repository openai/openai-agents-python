from __future__ import annotations

import asyncio
import contextvars

from agents.tracing.scope import Scope


def test_reset_current_span_swallows_cross_context_value_error() -> None:
    # Create a token in a child Context, then attempt to reset it from the
    # parent Context to mimic a span finishing in a different asyncio task
    # than the one that started it.
    captured: dict[str, contextvars.Token] = {}

    def grab_token() -> None:
        captured["token"] = Scope.set_current_span(None)

    contextvars.copy_context().run(grab_token)

    # No ValueError should propagate even though the token was created in a
    # different Context.
    Scope.reset_current_span(captured["token"])


def test_reset_current_trace_swallows_cross_context_value_error() -> None:
    captured: dict[str, contextvars.Token] = {}

    def grab_token() -> None:
        captured["token"] = Scope.set_current_trace(None)

    contextvars.copy_context().run(grab_token)

    Scope.reset_current_trace(captured["token"])


def test_reset_current_span_in_different_asyncio_task() -> None:
    # Replicates the production failure mode: token created in task A,
    # reset attempted from task B.
    async def main() -> None:
        captured: dict[str, contextvars.Token] = {}

        async def setter() -> None:
            captured["token"] = Scope.set_current_span(None)

        await asyncio.create_task(setter())

        async def resetter() -> None:
            Scope.reset_current_span(captured["token"])

        await asyncio.create_task(resetter())

    asyncio.run(main())
