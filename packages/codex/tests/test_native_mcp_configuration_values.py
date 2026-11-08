"""Captured MCP credentials support copying and safe validation failures."""

from __future__ import annotations

import copy
import json
import operator
import pickle
from dataclasses import asdict, is_dataclass
from typing import Any

import pytest

from agents import ChatGPTAppsMCP, HostedVerificationMCP, HttpMCP, StdioMCP

_SECRET = "synthetic-mcp-credential-for-traceback-tests"


def assert_private_frames(error: BaseException) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None
    assert _SECRET not in str(error)
    current = error.__traceback__
    inspected = 0
    while current is not None:
        frame = current.tb_frame
        if frame.f_code.co_filename != __file__:
            inspected += 1
            values = frame.f_locals
            assert _SECRET not in repr(values)
            for value in values.values():
                if is_dataclass(value) and not isinstance(value, type):
                    assert _SECRET not in repr(vars(value))
        current = current.tb_next
    assert inspected > 0


@pytest.mark.parametrize(
    "declaration,field,options",
    [
        (StdioMCP, "env", {"name": "local", "command": "peer", "tools": []}),
        (HttpMCP, "headers", {"name": "remote", "url": "https://example.invalid", "tools": []}),
        (HostedVerificationMCP, "headers", {"url": "https://example.invalid", "tools": []}),
    ],
)
def test_captured_values_support_dataclass_and_standard_copies(
    declaration: type[Any], field: str, options: dict[str, Any]
) -> None:
    supplied = {"X_SECRET": _SECRET}
    server = declaration(**options, **{field: supplied})
    supplied["X_SECRET"] = "later"
    expected = {"X_SECRET": _SECRET}
    assert asdict(server)[field] == expected
    assert json.loads(json.dumps(asdict(server)))[field] == expected
    for cloned in (copy.copy(server), copy.deepcopy(server), pickle.loads(pickle.dumps(server))):
        assert cloned is not server
        assert cloned == server
        values: Any = getattr(cloned, field)
        assert values == expected
        mutations = [
            lambda mapping: operator.setitem(mapping, "X_SECRET", "changed"),
            lambda mapping: operator.delitem(mapping, "X_SECRET"),
            lambda mapping: mapping.clear(),
            lambda mapping: mapping.pop("X_SECRET"),
            lambda mapping: mapping.popitem(),
            lambda mapping: mapping.setdefault("NEW", "changed"),
            lambda mapping: mapping.update({"NEW": "changed"}),
            lambda mapping: operator.ior(mapping, {"NEW": "changed"}),
        ]
        for mutate in mutations:
            with pytest.raises(TypeError) as error:
                mutate(values)
            assert values == expected
            assert_private_frames(error.value)


@pytest.mark.parametrize(
    "declaration,options",
    [
        (StdioMCP, {"name": "local", "command": "peer", "env": {"BAD=NAME": _SECRET}}),
        (StdioMCP, {"name": "local", "command": "peer", "env": {"TOKEN": _SECRET + "\x00"}}),
        (StdioMCP, {"name": "local", "command": "peer", "env": {"TOKEN": _SECRET + "\ud800"}}),
        (StdioMCP, {"name": "local", "command": "", "env": {"TOKEN": _SECRET}}),
        (StdioMCP, {"name": "local", "command": "peer", "env": [_SECRET]}),
        (
            HttpMCP,
            {
                "name": "remote",
                "url": "https://example.invalid",
                "headers": {"X-Token": _SECRET, "X-Invalid": 1},
            },
        ),
        (
            HttpMCP,
            {"name": "remote", "url": "https://example.invalid", "headers": {"bad name": _SECRET}},
        ),
        (
            HttpMCP,
            {
                "name": "remote",
                "url": "https://example.invalid",
                "headers": {"X-Token": _SECRET + "\r\n"},
            },
        ),
        (
            HttpMCP,
            {
                "name": "remote",
                "url": "https://example.invalid",
                "headers": {"X-Token": _SECRET + "\ud800"},
            },
        ),
        (HttpMCP, {"name": "remote", "url": "", "headers": {"X-Token": _SECRET}}),
        (
            HttpMCP,
            {
                "name": "remote",
                "url": "https://example.invalid",
                "headers": {"Authorization": _SECRET},
                "bearer_token": _SECRET,
            },
        ),
        (
            HostedVerificationMCP,
            {"url": "https://example.invalid", "headers": {"bad name": _SECRET}},
        ),
        (ChatGPTAppsMCP, {"access_token": _SECRET + "\n", "account_id": "account"}),
        (ChatGPTAppsMCP, {"access_token": _SECRET, "account_id": " bad-account"}),
    ],
)
def test_rejected_configuration_drops_credentials_from_sdk_tracebacks(
    declaration: type[Any], options: dict[str, Any]
) -> None:
    original = copy.deepcopy(options)
    with pytest.raises((TypeError, ValueError)) as error:
        declaration(tools=[], **options)
    assert options == original
    assert_private_frames(error.value)
