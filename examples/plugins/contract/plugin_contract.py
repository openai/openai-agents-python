"""Check one SDK function tool from proposal through the next model request."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from agents import RunConfig
from agents.testing import ScriptedModel, assistant_message, function_call


@asynccontextmanager
async def expect_tool_output(
    name: str,
    arguments: dict[str, Any],
    expected: str,
    *,
    codex_workspace: Path | None = None,
) -> AsyncIterator[tuple[ScriptedModel | str, RunConfig]]:
    """Yield a model and run config. Drive Runner and any approvals inside the block."""
    if not __debug__:
        raise RuntimeError("Run this check without Python optimization.")
    if codex_workspace is None:
        model = ScriptedModel(
            [[function_call(name, arguments, call_id="call_0")], [assistant_message("done")]]
        )
        yield model, RunConfig(tracing_disabled=True)
        model.assert_complete()
        proposal, followup = model.calls
        names = [tool.name for tool in proposal.tools]
        items = followup.input
    else:
        from loopback import MODEL, route

        requests: list[dict[str, Any]] = []
        async with route(True, codex_workspace, requests, calls=[(name, arguments)]) as provider:
            yield MODEL, RunConfig(tracing_disabled=True, model_provider=provider)
        first, second = requests
        names = [tool.get("name") for tool in first["tools"]]
        items = second["input"]

    assert names.count(name) == 1, f"Expected one advertised {name} tool"
    outputs = [
        (item["call_id"], item["output"])
        for item in items
        if item.get("type") == "function_call_output"
    ]
    assert outputs == [("call_0", expected)], f"Unexpected {name} output on the next model turn"
