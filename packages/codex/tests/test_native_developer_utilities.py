"""Native developer utilities through public declarations and real Sessions."""

from __future__ import annotations

import json

import pytest

from agents import Agent, function_tool, run_demo_loop
from agents.testing import ScriptedResponse, ScriptedResponses, event, text, tool_call


async def test_remaining_steps_and_captured_default_usage_follow_queue_edits():
    original = {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}
    peer = ScriptedResponses([text("discard")], default_usage=original)
    original["input_tokens"] = 999
    assert peer.remaining_steps == 1
    peer.clear()
    assert peer.remaining_steps == 0
    peer.append([text("first")], [text("second")])
    assert peer.remaining_steps == 2
    async with peer:
        agent = Agent(instructions="Respond.", provider=peer.provider)
        result = await agent.run("first synthetic input")
        assert result.thread_usage.last_token_usage.input_tokens == 4
        assert peer.remaining_steps == 1
        changed = {"input_tokens": 6, "output_tokens": 2, "total_tokens": 8}
        peer.set_default_usage(changed)
        changed["input_tokens"] = 999
        peer.append([text("discard later")])
        assert peer.remaining_steps == 2
        result = await agent.run("second synthetic input")
        assert result.thread_usage.last_token_usage.input_tokens == 6
        assert peer.remaining_steps == 1
        peer.clear()
        assert peer.remaining_steps == 0
        assert len(peer.requests) == 2
    assert peer.remaining_steps == 0


async def test_dynamic_responder_selects_current_default_and_explicit_usage_wins():
    peer = ScriptedResponses(
        default_usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    )

    def dynamic(body):
        assert peer.remaining_steps == 1
        peer.set_default_usage({"input_tokens": 8, "output_tokens": 1, "total_tokens": 9})
        return ScriptedResponse([text("dynamic")])

    peer.append(
        dynamic,
        ScriptedResponse(
            [text("explicit")], usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
        ),
    )
    async with peer:
        agent = Agent(instructions="Respond.", provider=peer.provider)
        assert (await agent.run("first")).thread_usage.last_token_usage.input_tokens == 8
        assert (await agent.run("second")).thread_usage.last_token_usage.input_tokens == 3


@pytest.mark.parametrize("explicit", [False, True])
async def test_clearing_default_and_empty_explicit_usage_remain_distinct(explicit):
    from tests.test_testing import request

    peer = ScriptedResponses(
        ScriptedResponse([text("answer")], usage={} if explicit else None),
        default_usage={"input_tokens": 4},
    )
    if not explicit:
        peer.set_default_usage(None)
    async with peer:
        _, _, payload = await request(peer.provider, "POST", "/v1/responses", {"input": "test"})
        completed = json.loads(payload.strip().split("\n\n")[-1][6:])["response"]
        if explicit:
            assert completed["usage"] == {}
        else:
            assert "usage" not in completed


@pytest.mark.parametrize("stream", [False, True])
async def test_demo_loop_retains_native_history_and_borrowed_provider(monkeypatch, capsys, stream):
    inputs = iter([" ", "first synthetic question", "second synthetic question", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    async with ScriptedResponses(
        [text("first answer")], [text("second answer")], [text("later")]
    ) as peer:
        agent = Agent(instructions="Respond.", provider=peer.provider)
        await run_demo_loop(agent, stream=stream)
        output = capsys.readouterr().out
        assert "first answer" in output and "second answer" in output
        assert len(peer.requests) == 2
        assert "first synthetic question" in json.dumps(peer.requests[1])
        assert "first answer" in json.dumps(peer.requests[1])
        # Session cleanup must not retire the supplied provider.
        assert (await agent.run("later synthetic question")).final_output == "later"


async def test_demo_loop_prints_streamed_deltas_without_repeating_final_text(monkeypatch, capsys):
    inputs = iter(["synthetic question", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    async with ScriptedResponses(
        [
            event(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "type": "message",
                        "id": "msg_stream",
                        "role": "assistant",
                        "content": [],
                    },
                }
            ),
            event(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_stream",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "live ",
                }
            ),
            event(
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_stream",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "answer",
                }
            ),
            text("live answer", item_id="msg_stream"),
        ]
    ) as peer:
        await run_demo_loop(Agent(instructions="Respond.", provider=peer.provider))
        assert capsys.readouterr().out == "live answer\n"


async def test_demo_loop_prints_native_tool_status(monkeypatch, capsys):
    calls = []

    @function_tool
    def lookup() -> str:
        """Find a synthetic value."""
        calls.append("lookup")
        return "synthetic value"

    inputs = iter(["synthetic question", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    async with ScriptedResponses(
        [tool_call("lookup", {}, call_id="call_lookup")], [text("accepted answer")]
    ) as peer:
        await run_demo_loop(Agent(instructions="Respond.", provider=peer.provider, tools=[lookup]))
        output = capsys.readouterr().out
        assert calls == ["lookup"]
        assert "[tool called]" in output and "[tool completed]" in output
        assert "accepted answer" in output


async def test_demo_loop_refuses_unhandled_approval_without_retiring_provider(monkeypatch):
    calls = []

    @function_tool(needs_approval=True)
    def lookup() -> str:
        """Find a synthetic value."""
        calls.append("lookup")
        return "synthetic value"

    monkeypatch.setattr("builtins.input", lambda _: "synthetic question")
    async with ScriptedResponses([tool_call("lookup", {}, call_id="call_approval")]) as peer:
        agent = Agent(instructions="Respond.", provider=peer.provider, tools=[lookup])
        with pytest.raises(RuntimeError, match="Pass on_interaction"):
            await run_demo_loop(agent)
        assert calls == []
        peer.append([text("still available")])
        assert (await agent.run("later synthetic question")).final_output == "still available"


def test_graph_preserves_distinct_labels_and_resolves_factories_without_effects():
    pytest.importorskip("graphviz")
    from agents.extensions.visualization import draw_graph

    calls = []

    @function_tool
    def lookup() -> str:
        """Find a synthetic value."""
        pytest.fail("Drawing must not execute tools")

    root = None

    def back():
        calls.append("back")
        return root

    child = Agent(
        name="Same", instructions="private-child-instruction", tools=[lookup], handoffs=[back]
    )

    def onward():
        calls.append("onward")
        return child

    root = Agent(
        name="Same", instructions="private-root-instruction", tools=[lookup], handoffs=[onward]
    )
    graph = draw_graph(root)
    assert calls == ["onward", "back"]
    assert "agent_0 -> agent_1" in graph.source
    assert "agent_1 -> agent_0" in graph.source
    assert graph.source.count("label=Same") == 2
    assert graph.source.count("label=lookup") == 1
    assert "private-" not in graph.source
    assert "agent_0 -> tool_0" in graph.source and "agent_1 -> tool_0" in graph.source
