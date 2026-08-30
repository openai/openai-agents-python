from __future__ import annotations

import pytest

from agents import _tool_invocation
from agents.util import _approvals


def test_parse_function_tool_arguments_treats_recursion_error_as_uninspectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion_error(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(_approvals.json, "loads", raise_recursion_error)

    assert _approvals.parse_function_tool_arguments('{"value": 1}') is None


def test_tool_invocation_identity_tolerates_json_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion_error(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(_tool_invocation.json, "loads", raise_recursion_error)

    identity = _tool_invocation.tool_invocation_identity(
        {
            "type": "function_call",
            "name": "send_email",
            "call_id": "call-deep",
            "arguments": '{"value": 1}',
        }
    )

    assert identity is not None
