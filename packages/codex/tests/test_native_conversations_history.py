"""Provider-item storage through real native Session orchestration."""

import asyncio
import json

import pytest

from _agents_codex._lifecycle import _error_contexts
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
        contexts = _error_contexts(failure.value)
        assert any(
            isinstance(error, asyncio.CancelledError) and error.args == ("caller cancellation",)
            for error in contexts
        )
        assert retired.is_set()
        if fail_append:
            error = next(
                (error for error in contexts if isinstance(error, ConversationsWriteError)), None
            )
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


async def test_environment_acquisition_reentry_rejects_before_session_exists(responses, endpoint):
    from contextlib import asynccontextmanager

    peer, client, _transport = endpoint
    history = ConversationsHistory(client)
    checked = []

    @asynccontextmanager
    async def environment():
        with pytest.raises(RuntimeError, match="owning operation"):
            await asyncio.wait_for(history.get_items(), 0.2)
        checked.append("rejected")
        raise RuntimeError("synthetic acquisition complete")
        yield  # establish the public async context-manager protocol

    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(instructions="Reply.", provider=provider, model="gpt-5")
        with pytest.raises(RuntimeError, match="synthetic acquisition complete"):
            await asyncio.wait_for(history.run(agent, "unused", environment=environment()), 2)
    assert checked == ["rejected"]
    assert peer.requests == []
    assert responses.requests == []


async def test_remote_image_urls_survive_public_native_history_replay(responses, endpoint):
    peer, client, _transport = endpoint
    image_url = "https://images.example.invalid/user-image.png"
    screenshot_url = "https://images.example.invalid/computer-screenshot.png"
    peer.items = [
        {
            "type": "message",
            "id": "image-message",
            "role": "user",
            "content": [{"type": "input_image", "image_url": image_url, "detail": "auto"}],
        },
        {
            "type": "computer_call",
            "id": "computer-item",
            "call_id": "computer-call",
            "action": {"type": "screenshot"},
            "pending_safety_checks": [],
            "status": "completed",
        },
        {
            "type": "computer_call_output",
            "id": "computer-output",
            "call_id": "computer-call",
            "output": {"type": "computer_screenshot", "image_url": screenshot_url},
        },
    ]
    history = ConversationsHistory(client, conversation_id="conv_existing")
    assert await history.get_items() == peer.items
    responses.respond(assistant_output("images retained"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(instructions="Inspect prior images.", provider=provider, model="gpt-5")
        assert (await history.run(agent, "continue")).final_output == "images retained"
    wire = responses.requests[0]["input"]
    assert any(
        content.get("image_url") == image_url
        for item in wire
        for content in item.get("content", [])
    )
    assert any(
        item.get("type") == "computer_call_output"
        and item.get("call_id") == "computer-call"
        and item["output"]["image_url"] == screenshot_url
        for item in wire
    )


@pytest.mark.parametrize("excess", ["items", "bytes"])
async def test_run_rejects_remote_import_before_native_serialization(
    responses, endpoint, monkeypatch, excess
):
    from agents.session import Session

    peer, client, _transport = endpoint
    peer.page_size = 100
    peer.items = [message(i) for i in range(4097)] if excess == "items" else [message(0)]
    if excess == "bytes":
        peer.items[0]["content"][0]["text"] = "é" * (4 * 1024 * 1024)
    reached = []

    async def serialization_boundary(*args, **kwargs):
        reached.append("serialization")
        raise AssertionError("oversized history reached native serialization")

    monkeypatch.setattr(Session, "transact_history", serialization_boundary)
    history = ConversationsHistory(client, conversation_id="conv_existing")
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(instructions="Reply.", provider=provider, model="gpt-5")
        expected = "4096 items" if excess == "items" else "8 MiB"
        with pytest.raises(ValueError, match=expected):
            await asyncio.wait_for(history.run(agent, "unused"), 10)
    assert reached == []
    assert responses.requests == []
    assert peer.batches == []
    assert len(peer.items) == (4097 if excess == "items" else 1)
    assert await history.get_items(1) == [peer.items[-1]]


async def test_queued_run_captures_mutable_input_before_history_lock(responses, endpoint):
    peer, client, _transport = endpoint
    history = ConversationsHistory(client, conversation_id="conv_existing")
    peer.release = asyncio.Event()
    append = asyncio.create_task(history.add_items([message(0)]))
    await peer.entered.wait()
    supplied = {
        "role": "user",
        "content": [{"type": "input_text", "text": "captured before queue"}],
    }
    responses.respond(assistant_output("done"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(instructions="Reply.", provider=provider, model="gpt-5")
        running = asyncio.create_task(history.run(agent, supplied))
        await asyncio.sleep(0)
        supplied["content"][0]["text"] = "mutated after queue"
        peer.release.set()
        await append
        assert (await running).final_output == "done"
    wire = json.dumps(responses.requests[0]["input"])
    assert "captured before queue" in wire
    assert "mutated after queue" not in wire
    assert "captured before queue" in json.dumps(peer.items)
    assert "mutated after queue" not in json.dumps(peer.items)


@pytest.mark.parametrize("fail_final", [False, True])
async def test_cancelled_final_save_retains_all_batches_and_result(responses, endpoint, fail_final):
    peer, client, _transport = endpoint
    history = ConversationsHistory(client, conversation_id="conv_existing")
    effects = []

    @function_tool
    async def effect() -> str:
        effects.append("once")
        return "accepted once"

    responses.respond(function_call("effect", {}, "final-save-call"))
    messages = [assistant_output(f"done-{i}") for i in range(19)]
    for i, event in enumerate(messages):
        event["item"]["id"] = f"tail-{i}"
    responses.respond(*messages)
    peer.release = asyncio.Event()
    if fail_final:
        peer.fail_batch = 2
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use effect once.", provider=provider, model="gpt-5", tools=[effect]
        )
        running = asyncio.create_task(history.run(agent, "perform effect"))
        await asyncio.wait_for(peer.entered.wait(), 5)
        running.cancel("cancel completed append")
        peer.release.set()
        with pytest.raises(asyncio.CancelledError) as failed:
            await asyncio.wait_for(running, 5)
        contexts = _error_contexts(failed.value)
        assert any(
            isinstance(error, asyncio.CancelledError) and error.args == ("cancel completed append",)
            for error in contexts
        )
        assert len(peer.batches) == 2
        if fail_final:
            error = next(
                (error for error in contexts if isinstance(error, ConversationsWriteError)), None
            )
            assert isinstance(error, ConversationsWriteError)
            assert error.acknowledged == 20
            assert error.result is not None
            assert any(item.get("id") == "tail-18" for item in error.items)
            assert await history.get_items() == peer.items
            peer.fail_batch = None
            await history.add_items(error.items[error.acknowledged :])
        assert any(item.get("id") == "tail-18" for item in peer.items)
        responses.respond(assistant_output("continued without replay"))
        assert (await history.run(agent, "continue")).final_output == "continued without replay"
    assert effects == ["once"]


@pytest.mark.parametrize("manual", [False, True])
async def test_unattended_interaction_fails_and_releases_history(responses, endpoint, manual):
    from pydantic import BaseModel

    from agents import FunctionTool, InteractionRequired

    class Args(BaseModel):
        key: str

    effects = []

    @function_tool(needs_approval=True)
    async def effect(key: str) -> str:
        effects.append(key)
        return "unreachable"

    tool = (
        FunctionTool(name="effect", description="Manual effect", input_type=Args, output_type=str)
        if manual
        else effect
    )
    peer, client, _transport = endpoint
    history = ConversationsHistory(client, conversation_id="conv_existing")
    responses.respond(function_call("effect", {"key": "never"}, "unattended-call"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(instructions="Use effect.", provider=provider, model="gpt-5", tools=[tool])
        with pytest.raises(InteractionRequired):
            await asyncio.wait_for(history.run(agent, "begin"), 2)
        assert await asyncio.wait_for(history.get_items(), 2) == peer.items
    assert effects == []


async def test_configured_interaction_handler_keeps_supported_approval(responses, endpoint):
    effects = []

    @function_tool(needs_approval=True)
    async def effect() -> str:
        effects.append("once")
        return "approved effect"

    async def approve(request):
        return request.reply(True)

    _peer, client, _transport = endpoint
    history = ConversationsHistory(client, conversation_id="conv_existing")
    responses.respond(function_call("effect", {}, "approved-call"))
    responses.respond(assistant_output("approved"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(instructions="Use effect.", provider=provider, model="gpt-5", tools=[effect])
        assert (
            await history.run(agent, "begin", on_interaction=approve)
        ).final_output == "approved"
    assert effects == ["once"]


async def test_retryable_close_exposes_existing_cleanup_owner(responses, endpoint, monkeypatch):
    from agents import get_cleanup
    from agents.session import Session

    original_exit = Session._exit_scope
    attempts = []

    async def fail_once(owner):
        attempts.append(owner)
        if len(attempts) == 1:
            raise RuntimeError("synthetic retryable native close")
        await original_exit(owner)

    monkeypatch.setattr(Session, "_exit_scope", fail_once)
    peer, client, _transport = endpoint
    history = ConversationsHistory(client, conversation_id="conv_existing")
    responses.respond(assistant_output("already appended"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(instructions="Reply.", provider=provider, model="gpt-5")
        with pytest.raises(RuntimeError, match="synthetic retryable native close") as failed:
            await history.run(agent, "begin")
        cleanup = get_cleanup(failed.value)
        assert cleanup is attempts[0]
        with pytest.raises(RuntimeError, match="open or opening"):
            await provider.aclose()
        assert "already appended" in json.dumps(await history.get_items())
        await cleanup.aclose()
        await provider.aclose()
    assert attempts == [cleanup, cleanup]
    assert len(responses.requests) == 1
    assert len(peer.batches) == 1
