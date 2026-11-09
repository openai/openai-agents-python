"""Reject foreign native function grants before application code enters."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents import Agent, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import function_call, responses as responses
from tests.test_native_agent import provider


@pytest.mark.parametrize("identity", ["other-session", "old-catalog"])
async def test_native_request_identity_is_checked_before_the_function(
    responses, monkeypatch, identity
):
    effects: list[str] = []

    @function_tool
    async def lookup(count: int) -> str:
        effects.append("body")
        return str(count)

    responses.respond(function_call("lookup", {"count": 4}, "expired-function"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(30), agent.session() as owner, agent.session() as other:
        native = owner._native

        class Admission:
            def __getattr__(self, name: str) -> Any:
                return getattr(native, name)

            def _check_python_tool(self, raw: str) -> bool:
                accepted = native._check_python_tool(raw)
                if not accepted:
                    return False
                if identity == "other-session":
                    other._native._check_python_tool(raw)
                else:
                    stale = json.loads(raw)
                    stale["_sdk_python_tool"]["generation"] += 1
                    native._check_python_tool(json.dumps(stale))
                return accepted

        monkeypatch.setattr(owner, "_native", Admission())
        handle = await owner.submit("use lookup")
        thin_error = (
            "Python tool turn has ended"
            if identity == "other-session"
            else "Python tool request belongs to an expired owner"
        )
        with pytest.raises(
            RuntimeError,
            match=f"^(?:Expired or invalid managed Python tool request|{thin_error})$",
        ):
            await handle.get_result()
    assert effects == []
    assert len(responses.requests) == 1
