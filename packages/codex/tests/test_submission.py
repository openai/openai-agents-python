"""The extracted submission keeps one identity and independent live state."""

from __future__ import annotations

import asyncio
from dataclasses import fields

from _agents_codex import session
from _agents_codex._submission import _future, _Submission
from _agents_codex.schema import _prepare_spec
from _agents_codex.usage import _UsageCollector


def test_submission_keeps_session_aliases():
    assert session._Submission is _Submission
    assert session._future is _future
    assert session._ChildCallbacks.retain_cleanup_error is _Submission.retain_cleanup_error


async def test_submission_defaults_are_independent():
    output = _prepare_spec(str)
    left, right = _Submission(output), _Submission(output)
    independent = (asyncio.Future, asyncio.Event, dict, list, set, _UsageCollector)
    for definition in fields(left):
        value = getattr(left, definition.name)
        if isinstance(value, independent):
            assert value is not getattr(right, definition.name), definition.name
    futures = [
        getattr(owner, name)
        for owner in (left, right)
        for name in ("accepted", "completion", "terminal", "failure")
    ]
    assert len({id(future) for future in futures}) == 8
    assert all(future.get_loop() is asyncio.get_running_loop() for future in futures)
