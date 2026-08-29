from __future__ import annotations

import pytest

from agents.util import _approvals


def test_parse_function_tool_arguments_treats_recursion_error_as_uninspectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion_error(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(_approvals.json, "loads", raise_recursion_error)

    assert _approvals.parse_function_tool_arguments('{"value": 1}') is None
