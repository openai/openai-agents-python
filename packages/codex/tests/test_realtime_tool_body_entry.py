"""Exercise the real callback owner without loading a native product."""

import pytest

from _agents_codex.session import _call_callback_entered


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("admitted", [False, True])
async def test_tool_body_entry_follows_final_callback_admission(asynchronous, admitted):
    events = []

    def enter():
        events.append("executing")

    def synchronous():
        events.append("body")
        return "accepted"

    async def coroutine():
        events.append("body")
        return "accepted"

    result = await _call_callback_entered(
        coroutine if asynchronous else synchronous,
        lambda: admitted,
        (),
        {},
        enter,
    )
    assert events == (["executing", "body"] if admitted else [])
    assert result == ("accepted" if admitted else None)
