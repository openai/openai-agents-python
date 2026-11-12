"""Provider-item storage through real native Session orchestration."""

import asyncio
import json

import pytest

from agents import (
    Agent,
    ConversationsHistory,
    ConversationsWriteError,
    NativeProvider,
    function_tool,
)
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_conversations_history import endpoint as endpoint, message


def prior_items():
    return [
        message("past"),
        {
            "type": "function_call",
            "id": "fc_original",
            "call_id": "original-call",
            "name": "echo",
            "arguments": '{"value":"past"}',
            "status": "completed",
            "future": {"retained": True},
        },
        {
            "type": "function_call_output",
            "id": "fco_original",
            "call_id": "original-call",
            "output": "past receipt",
            "status": "completed",
        },
        {
            "type": "message",
            "id": "msg_original",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "past completion", "annotations": []}],
        },
    ]


@pytest.mark.parametrize("fail_append", [False, True])
async def test_native_conversation_history_preserves_identity_without_tool_replay(
    responses, endpoint, fail_append
):
    peer, client, transport = endpoint
    peer.items = prior_items()
    before = list(peer.items)
    history = ConversationsHistory(client, conversation_id="conv_existing")
    effects = []
    reentry_errors = []

    @function_tool
    async def echo(value: str) -> str:
        effects.append(value)
        try:
            await history.get_items()
        except RuntimeError as error:
            reentry_errors.append(str(error))
        return "fresh receipt"

    responses.respond(function_call("echo", {"value": "fresh"}, "fresh-call"))
    responses.respond(assistant_output("done"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="native declaration instruction sentinel",
            model="gpt-5",
            provider=provider,
            tools=[echo],
        )
        if fail_append:
            peer.fail_batch = 1
            with pytest.raises(ConversationsWriteError) as caught:
                await history.run(agent, "next")
            assert caught.value.result.final_output == "done"
            assert caught.value.acknowledged == 0
            assert peer.items == before
            peer.fail_batch = None
            await history.add_items(caught.value.items)
        else:
            assert (await history.run(agent, "next")).final_output == "done"
        assert effects == ["fresh"]
        assert reentry_errors == ["A tool callback cannot access its own Conversations history"]
        assert peer.items[: len(before)] == before
        added = peer.items[len(before) :]
        assert any(
            item.get("call_id") == "fresh-call" and item["type"] == "function_call"
            for item in added
        )
        assert any(
            item.get("call_id") == "fresh-call" and item["type"] == "function_call_output"
            for item in added
        )
        assert any(
            item.get("call_id") == "fresh-call" and "fresh receipt" in str(item.get("output"))
            for item in added
        )
        assert "native declaration instruction sentinel" not in json.dumps(peer.items)
        assert "runtime_revision" not in json.dumps(peer.items)
        assert "schema_version" not in json.dumps(peer.items)
        responses.respond(assistant_output("continued"))
        assert (await history.run(agent, "continue")).final_output == "continued"
    assert effects == ["fresh"]
    assert not transport.is_closed and client.max_retries == 3


async def test_native_failure_retains_readable_accepted_originals(responses):
    effects = []

    @function_tool
    def effect() -> str:
        effects.append("once")
        return "accepted partial receipt"

    responses.respond(function_call("effect", {}, "partial-call"))
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {"code": "invalid_prompt", "message": "synthetic continuation failure"}
            },
        }
    )
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use effect once.", provider=provider, model="gpt-5", tools=[effect]
        )
        async with agent.session() as session:
            turn = await session.submit("perform one effect")
            try:
                await asyncio.wait_for(turn.get_result(), 10)
            except TimeoutError:
                raise
            except Exception as error:
                print("native failure", type(error).__name__, str(error))
            else:
                raise AssertionError("Expected synthetic model failure")
            snapshot = await session.history_snapshot()
            assert any(
                item.get("type") == "function_call_output"
                and item.get("call_id") == "partial-call"
                and "accepted partial receipt" in str(item.get("output"))
                for item in snapshot.items
            )
    assert effects == ["once"]


async def test_failed_run_persists_accepted_receipt_before_retry(responses, endpoint):
    peer, client, _transport = endpoint
    history = ConversationsHistory(client, conversation_id="conv_existing")
    effects = []

    @function_tool
    def effect() -> str:
        effects.append("once")
        return "accepted partial receipt"

    responses.respond(function_call("effect", {}, "partial-call"))
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {"code": "invalid_prompt", "message": "synthetic continuation failure"}
            },
        }
    )
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use effect once.", provider=provider, model="gpt-5", tools=[effect]
        )
        with pytest.raises(Exception, match="synthetic continuation failure"):
            await asyncio.wait_for(history.run(agent, "perform one effect"), 10)
        assert any(
            item.get("type") == "function_call_output"
            and item.get("call_id") == "partial-call"
            and "accepted partial receipt" in str(item.get("output"))
            for item in peer.items
        )
        responses.respond(assistant_output("continued after failed turn"))
        assert (await history.run(agent, "continue")).final_output == "continued after failed turn"
    assert effects == ["once"]


@pytest.mark.parametrize("hold_submit", [False, True])
@pytest.mark.parametrize("fail_append", [False, True])
async def test_cancelled_run_joins_callback_and_saves_accepted_call(
    responses, endpoint, monkeypatch, hold_submit, fail_append
):
    peer, client, _transport = endpoint
    history = ConversationsHistory(client, conversation_id="conv_existing")
    entered, retired = asyncio.Event(), asyncio.Event()
    effects = []
    if fail_append:
        peer.fail_batch = 1
    if hold_submit:
        from agents.session import Session

        original_submit = Session.submit
        first = True

        async def gated_submit(owner, *args, **kwargs):
            nonlocal first
            turn = await original_submit(owner, *args, **kwargs)
            if first:
                first = False
                await asyncio.Event().wait()
            return turn

        monkeypatch.setattr(Session, "submit", gated_submit)

    @function_tool
    async def effect() -> str:
        effects.append("once")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            retired.set()
        return "unreachable"

    responses.respond(function_call("effect", {}, "interrupted-call"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use effect once.", provider=provider, model="gpt-5", tools=[effect]
        )
        running = asyncio.create_task(history.run(agent, "perform one effect"))
        await asyncio.wait_for(entered.wait(), 5)
        running.cancel("caller cancellation")
        with pytest.raises(asyncio.CancelledError) as failure:
            await asyncio.wait_for(running, 5)
        assert failure.value.args == ("caller cancellation",)
        assert retired.is_set()
        if fail_append:
            error = failure.value.__context__
            assert isinstance(error, ConversationsWriteError)
            assert error.acknowledged == 0
            assert any(item.get("call_id") == "interrupted-call" for item in error.items)
            assert len(peer.batches) == 1
            assert await history.get_items() == []
            peer.fail_batch = None
            await history.add_items(error.items)
        assert any(item.get("call_id") == "interrupted-call" for item in peer.items)
        assert await history.get_items() == peer.items
        responses.respond(assistant_output("continued after cancellation"))
        assert (await history.run(agent, "continue")).final_output == "continued after cancellation"
    assert effects == ["once"]
