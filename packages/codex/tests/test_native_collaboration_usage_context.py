"""Collaboration approval captures the current native speech usage snapshot."""

from contextlib import aclosing
from dataclasses import replace

import pytest

from agents import NativeAgents, NativeError, ToolContext
from agents.realtime import RealtimeConfig, RealtimeResponse
from agents.testing import RealtimeStep, ScriptedRealtimeAdapter
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import agent_for, child_peer, trust
from tests.test_native_executor_tools import declaration


@pytest.mark.parametrize("with_usage", [False, True])
async def test_collaboration_approval_captures_detached_realtime_usage(
    tmp_path, monkeypatch, with_usage
):
    application = {"marker": object()}
    contexts: list[ToolContext] = []

    def deny(context: ToolContext, _call) -> bool:
        assert context.context is application
        contexts.append(context)
        return False

    adapter = ScriptedRealtimeAdapter(
        [
            RealtimeStep(
                "session.update",
                lambda frame: [
                    {"type": "session.updated", "session": {**frame["session"], "id": "fixture"}}
                ],
            ),
            RealtimeStep("conversation.item.create"),
            RealtimeStep(
                "response.create",
                [
                    {"type": "response.created", "response": {"id": "usage"}},
                    {
                        "type": "response.done",
                        "response": {
                            "id": "usage",
                            **(
                                {
                                    "usage": {
                                        "total_tokens": 7,
                                        "input_token_details": {"audio_tokens": 7},
                                    }
                                }
                                if with_usage
                                else {}
                            ),
                        },
                    },
                ],
            ),
        ]
    )
    async with (
        timeout(20),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        agent = replace(
            agent,
            tools=[
                replace(tool, approval=deny) if isinstance(tool, NativeAgents) else tool
                for tool in agent.tools
            ],
        )
        async with agent.session(context=application, environment=declaration(executor)) as session:
            async with session.realtime(
                config=RealtimeConfig(model="fixture", adapter=adapter)
            ) as voice:
                await voice.send_text("Record speech usage")
                async with aclosing(voice.events()) as events:
                    async for event in events:
                        if isinstance(event, RealtimeResponse) and event.status == "done":
                            break
                expected = session.realtime_usage
                assert (None if expected is None else expected.total_tokens) == (
                    7 if with_usage else None
                )
                with pytest.raises(NativeError, match="interception failed or was denied"):
                    await (await session.submit("Use two children")).get_result()
                assert contexts
                assert all(context.realtime_usage == expected for context in contexts)
                if with_usage:
                    snapshot = contexts[0].realtime_usage
                    assert snapshot is not None
                    details = snapshot.input_token_details
                    assert isinstance(details, dict)
                    details["audio_tokens"] = 999
                    current = session.realtime_usage
                    assert current is not None
                    assert current.input_token_details == {"audio_tokens": 7}
                assert peer.child_requests == {}
            assert session.realtime_usage is None
            assert all(
                (None if context.realtime_usage is None else context.realtime_usage.total_tokens)
                == (7 if with_usage else None)
                for context in contexts
            )
    adapter.assert_complete()
