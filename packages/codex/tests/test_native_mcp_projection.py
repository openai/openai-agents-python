"""Native catalog name parity and inert declared tool references."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from _agents_codex.mcp import _project_tool_names
from agents import HttpMCP


def test_projection_matches_core_golden_vectors_in_any_input_order() -> None:
    vectors = json.loads((Path(__file__).parent / "fixtures/mcp-projection.json").read_text())
    for rows in (vectors, list(reversed(vectors))):
        servers: dict[str, list[str]] = {}
        for row in rows:
            servers.setdefault(row["server"], []).append(row["raw"])
        names = _project_tool_names(list(servers.items()))
        for row in rows:
            assert names[row["server"], row["raw"]] == {
                "namespace": row["namespace"],
                "name": row["name"],
            }


def test_declared_collision_refs_remain_distinct_without_native_loading() -> None:
    server = HttpMCP(name="docs", tools=["a.b", "a_b"], url="http://127.0.0.1:1/mcp")
    assert server.tool("a.b").name == "a_b_98fddac4920d"
    assert server.tool("a_b").name != server.tool("a.b").name


def test_narrowed_selector_keeps_captured_collision_identity() -> None:
    server = HttpMCP(name="docs", tools=["a.b", "a_b"], url="http://127.0.0.1:1/mcp")
    selected = replace(server, tools=("a.b",))
    assert selected.tool("a.b") == server.tool("a.b")


@pytest.mark.parametrize("raw", ["\ud800", "nul\0name"])
def test_projection_rejects_invalid_raw_unicode_before_capture(raw: str) -> None:
    with pytest.raises(ValueError) as rejected:
        _project_tool_names([("docs", [raw])], prefix=False)
    assert rejected.value.__context__ is None


def test_projection_rejects_duplicate_raw_identity() -> None:
    with pytest.raises(ValueError, match="unique"):
        _project_tool_names([("docs", ["lookup", "lookup"])])


@pytest.mark.asyncio
async def test_child_mcp_observations_keep_the_captured_callable_identity() -> None:
    from types import SimpleNamespace
    from typing import Any, cast

    from _agents_codex.session import Session, _ChildCallbacks
    from agents import ToolRef
    from agents.observation import ToolFinished, ToolStarted

    remote = HttpMCP(name="docs", url="http://localhost/mcp", tools=["a.b"])
    state = _ChildCallbacks(
        handle=cast(
            Any,
            SimpleNamespace(
                turn_id="turn", child_id="child", instance_id="instance", submission_id="submission"
            ),
        )
    )
    actual = ToolRef("a_b_98fddac4920d", "mcp__docs")
    state.mcp_names[("docs", "a.b")] = actual
    state.step_mcp = (remote,)
    harness = SimpleNamespace(id="session", _mcp_tools=(), _observations=[])
    begin = {
        "subagent_id": "child",
        "subagent_incarnation": "instance",
        "msg": {
            "type": "mcp_tool_call_begin",
            "turn_id": "turn",
            "call_id": "call",
            "invocation": {"server": "docs", "tool": "a.b"},
        },
    }
    Session._observe_mcp(cast(Any, harness), begin, state)
    # A subsequent mapping replacement must not rename an already observed call.
    state.mcp_names = {}
    end = {**begin, "msg": {**begin["msg"], "type": "mcp_tool_call_end", "result": {"Ok": {}}}}
    Session._observe_mcp(cast(Any, harness), end, state)
    assert [type(item) for item in harness._observations] == [ToolStarted, ToolFinished]
    assert [(item.namespace, item.name) for item in harness._observations] == [
        (actual.namespace, actual.name)
    ] * 2
    assert state.mcp_call_names == {}
