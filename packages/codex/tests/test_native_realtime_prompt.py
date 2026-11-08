"""Initial voice instructions inherit the accepted native Agent unless overridden."""

from __future__ import annotations

import pytest

from agents import Agent, NativeProvider
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("instructions", [None, "", "Explicit voice instructions."])
async def test_initial_voice_prompt_preserves_inheritance_and_explicit_overrides(
    realtime_peer: RealtimePeer, instructions: str | None
) -> None:
    peer = realtime_peer
    inherited = "Inherited native Agent instructions."
    async with (
        timeout(10),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-inherited-prompt-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions=inherited, provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", instructions=instructions)
        ),
    ):
        update = await peer.frame(lambda frame: frame["type"] == "session.update")
        if instructions is None:
            assert inherited in update["session"]["instructions"]
        else:
            assert update["session"]["instructions"] == instructions
        assert peer.responses.requests == []
