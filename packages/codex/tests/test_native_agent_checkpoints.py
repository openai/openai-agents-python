"""Canonical checkpoints use native storage and current application bindings."""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticSerializationError

from agents import (
    Agent,
    NativeProvider,
    OutputValidationError,
    SessionBusyError,
    ToolContext,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import core_owner as core_owner


@dataclass
class Peer:
    replies: deque[dict[str, Any]] = field(default_factory=deque)
    requests: list[dict[str, Any]] = field(default_factory=list)
    authorization: list[str | None] = field(default_factory=list)
    url: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    usage: dict[str, Any] | None = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(await request.json())
        self.authorization.append(request.headers.get("Authorization"))
        assert self.replies, "unexpected native model request"
        response_id = f"response-{len(self.requests)}"
        completion: dict[str, Any] = {"id": response_id}
        if self.usage is not None:
            completion["usage"] = self.usage
        events = [
            {"type": "response.created", "response": {"id": response_id}},
            {"type": "response.output_item.done", "item": self.replies.popleft()},
            {"type": "response.completed", "response": completion},
        ]
        response = web.StreamResponse(
            headers={"Content-Type": "text/event-stream", **self.response_headers}
        )
        await response.prepare(request)
        for event in events:
            await response.write(f"data: {json.dumps(event)}\n\n".encode())
        await response.write_eof()
        return response

    def provider(self, key: str = "synthetic-checkpoint-key") -> NativeProvider:
        return NativeProvider(base_url=self.url, api_key=key)


@asynccontextmanager
async def peer():
    fixture = Peer()
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    async with TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        yield fixture


def message(text: str, item_id: str = "native-message") -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "id": item_id,
        "content": [{"type": "output_text", "text": text}],
    }


def call(name: str, call_id: str) -> dict[str, Any]:
    return {"type": "function_call", "name": name, "call_id": call_id, "arguments": "{}"}


@pytest.mark.parametrize("malformed_json", [False, True], ids=["invalid-history", "invalid-json"])
def test_checkpoint_parse_errors_do_not_echo_stored_values(malformed_json):
    secret = "S3CRET"
    raw = json.dumps(
        {
            "format_version": 2,
            "root_metadata": {},
            "codex_revision": "saved-revision",
            "source_session_id": "saved-session",
            "history": {"message": secret},
        }
    )
    if malformed_json:
        raw = json.dumps({"message": secret}) + " trailing"
    with pytest.raises(ValidationError) as error:
        SessionState.from_json(raw)
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert error.value.errors(include_input=False)


@pytest.mark.parametrize("percent", [None, "37", "37.5"], ids=["no-header", "whole", "fractional"])
async def test_native_checkpoint_roundtrips_rate_usage_without_losing_integer_precision(percent):
    async with timeout(30), peer() as provider:
        if percent is not None:
            provider.response_headers["x-codex-primary-used-percent"] = percent
        provider.usage = {
            "input_tokens": 17,
            "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 4},
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 24,
        }
        provider.replies.extend([message("before"), message("after", "after-restore")])
        agent = Agent(instructions="Answer.", provider=provider.provider())
        async with agent.session() as original:
            submitted = await original.submit("remember original")
            assert (await (submitted).get_result()).final_output == "before"
            saved = await original.save_state()
        encoded = json.loads(saved.to_json())
        counts = [
            item["payload"]
            for item in encoded["history"]
            if item["type"] == "event_msg" and item["payload"].get("type") == "token_count"
        ]
        assert len(counts) == 1
        rate = counts[0]["rate_limits"]
        if percent is None:
            assert rate is None or rate["primary"] is None
        else:
            assert rate["primary"]["used_percent"] == float(percent)
        # Check that decoding floats does not convert unrelated i64 counts through f64.
        exact = 9_007_199_254_740_993
        counts[0]["info"]["total_token_usage"]["input_tokens"] = exact
        state = SessionState.from_json(json.dumps(encoded))
        async with agent.session(state=state) as restored:
            resaved = json.loads((await restored.save_state()).to_json())
            restored_counts = [
                item["payload"]
                for item in resaved["history"]
                if item["type"] == "event_msg" and item["payload"].get("type") == "token_count"
            ]
            assert restored_counts
            assert restored_counts[0]["info"]["total_token_usage"]["input_tokens"] == exact
            assert restored_counts[0]["rate_limits"] == rate
            next_turn = await restored.submit("continue")
            assert (await (next_turn).get_result()).final_output == "after"
        assert len(provider.requests) == 2
        assert "remember original" in json.dumps(provider.requests[1]["input"])


async def test_native_checkpoint_preserves_rate_headers_after_rejected_output():
    class Answer(BaseModel):
        value: str

    async with timeout(30), peer() as provider:
        provider.response_headers["x-codex-primary-used-percent"] = "37.5"
        # Native completion can precede rejection by the declared output type.
        provider.replies.append(message('{"unexpected": true}'))
        agent = Agent(instructions="Answer.", output_type=Answer, provider=provider.provider())
        async with agent.session() as original:
            handle = await original.submit("reject the unrequested output")
            with pytest.raises(OutputValidationError) as failure:
                await (handle).get_result()
            assert failure.value.raw_output == '{"unexpected": true}'
            saved = await original.save_state()
        encoded = json.loads(saved.to_json())
        counts = [
            item["payload"]
            for item in encoded["history"]
            if item["type"] == "event_msg" and item["payload"].get("type") == "token_count"
        ]
        assert len(counts) == 1
        assert counts[0]["rate_limits"]["primary"]["used_percent"] == 37.5
        async with agent.session(state=saved) as restored:
            resaved = json.loads((await restored.save_state()).to_json())
        restored_counts = [
            item["payload"]
            for item in resaved["history"]
            if item["type"] == "event_msg" and item["payload"].get("type") == "token_count"
        ]
        assert restored_counts == counts
        assert len(provider.requests) == 1


@pytest.mark.parametrize("window", ["primary", "secondary"])
@pytest.mark.parametrize(
    "bad_percent",
    [
        pytest.param("37.5", id="string"),
        pytest.param({"value": 37.5}, id="ordinary-object"),
        pytest.param(True, id="boolean"),
        pytest.param({"$serde_json::private::Number": "37.5"}, id="number-object"),
        pytest.param({"$serde_json::private::RawValue": "37.5"}, id="raw-object"),
        pytest.param(
            {"$serde_json::private::Number": "37.5", "other": "kept"}, id="number-key-first"
        ),
        pytest.param(
            {"other": "kept", "$serde_json::private::Number": "37.5"}, id="number-key-last"
        ),
        pytest.param(
            {"$serde_json::private::RawValue": "37.5", "other": "kept"}, id="raw-key-first"
        ),
        pytest.param(
            {"other": "kept", "$serde_json::private::RawValue": "37.5"}, id="raw-key-last"
        ),
    ],
)
async def test_native_checkpoint_rejects_non_numeric_rate_without_model_request(
    bad_percent, window
):
    async with timeout(30), peer() as provider:
        provider.response_headers[f"x-codex-{window}-used-percent"] = "37.5"
        provider.replies.append(message("before"))
        agent = Agent(instructions="Answer.", provider=provider.provider())
        async with agent.session() as original:
            handle = await original.submit("before invalid restore")
            assert (await (handle).get_result()).final_output == "before"
            saved = await original.save_state()
        changed = json.loads(saved.to_json())
        count = next(
            item["payload"]
            for item in changed["history"]
            if item["type"] == "event_msg" and item["payload"].get("type") == "token_count"
        )
        assert count["rate_limits"][window]["used_percent"] == 37.5
        count["rate_limits"][window]["used_percent"] = bad_percent
        with pytest.raises(RuntimeError, match="invalid type"):
            async with agent.session(state=SessionState.from_json(json.dumps(changed))):
                pytest.fail("Non-numeric rate was accepted")
        assert len(provider.requests) == 1


async def test_native_checkpoint_rejects_deep_unknown_json_before_a_provider_request():
    async with timeout(30), peer() as provider:
        provider.replies.append(message("before"))
        agent = Agent(instructions="Answer.", provider=provider.provider())
        async with agent.session() as original:
            handle = await original.submit("before invalid restore")
            assert (await (handle).get_result()).final_output == "before"
            saved = await original.save_state()
        changed = json.loads(saved.to_json())
        deep: object = 0
        for _ in range(128):
            deep = [deep]
        changed["history"][0]["nested_unknown"] = deep
        state = SessionState.from_json(json.dumps(changed))
        assert state.history[0]["nested_unknown"] == deep
        # Restore validates and serializes the input before native decoding.
        # Windows Pydantic has a lower depth limit than the native decoder.
        # Either rejection must happen before another provider request.
        with pytest.raises(
            (RuntimeError, PydanticSerializationError),
            match=r"recursion limit exceeded|Circular reference detected \(depth exceeded\)",
        ):
            async with agent.session(state=state):
                pytest.fail("Excessively nested JSON was accepted")
        assert len(provider.requests) == 1


@dataclass
class Application:
    record: str
    private_value: str = "application-object-must-not-be-saved"


def lookup_tool(calls: list[ToolContext[Application]]):
    @function_tool
    async def lookup(context: ToolContext[Application]) -> str:
        """Read the current application's record."""
        calls.append(context)
        return context.context.record

    return lookup


@pytest.mark.usefixtures("core_owner")
async def test_checkpoint_preserves_native_identity_and_uses_current_bindings():
    source_calls: list[ToolContext[Application]] = []
    restored_calls: list[ToolContext[Application]] = []
    conversation_value = "synthetic-conversation-secret"
    source_context = Application(f"source-record {conversation_value}")
    current_context = Application("current-record")
    async with timeout(30), peer() as source, peer() as destination:
        source.replies.extend([call("lookup", "original-call"), message("original-answer")])
        agent = Agent(
            instructions="Original application instructions.",
            tools=[lookup_tool(source_calls)],
            provider=source.provider("synthetic-source-credential"),
        )
        async with agent.session(context=source_context) as original:
            submitted = await original.submit("original-input")
            completed = await (submitted).get_result()
            assert completed.final_output == "original-answer"
            assert completed.turn is submitted
            saved = await original.save_state()
            items = [item async for item in original.items()]
            turns = [item async for item in original.turns()]
            home = Path(original._home.name)
            assert saved.source_session_id == submitted.session_id == original.id
            assert turns[0]["id"] == submitted.turn_id
            assert len(source_calls) == 1
            assert source_calls[0].submission is submitted
            assert source_calls[0].call_id == "original-call"
            encoded = saved.to_json()
            assert "synthetic-source-credential" not in encoded
            assert source_context.private_value not in encoded
            assert conversation_value in encoded
        assert not home.exists()
        assert await (submitted).get_result() is completed

        # Historical options are data. Only the current Agent selects a route.
        decoded = json.loads(encoded)
        saved_credentials = {
            "header": "synthetic-saved-header",
            "mcp": "synthetic-saved-mcp-token",
            "environment": "synthetic-saved-environment-key",
        }
        decoded["agent_options"] = {
            "instructions": "Saved options must not replace current instructions.",
            "config": {
                "model_provider": "saved-route",
                "model_providers": {
                    "saved-route": {
                        "base_url": "http://127.0.0.1:1/v1",
                        "http_headers": {"Authorization": saved_credentials["header"]},
                    }
                },
                "mcp_servers": {
                    "saved": {
                        "url": "http://127.0.0.1:1/mcp",
                        "bearer_token": saved_credentials["mcp"],
                    }
                },
            },
            "runtime": {
                "cwd": str(home),
                "env": {"OPENAI_API_KEY": saved_credentials["environment"]},
            },
            "api_key": "synthetic-saved-credential",
        }
        saved = SessionState.from_json(json.dumps(decoded))
        destination.replies.extend(
            [call("lookup", "restored-call"), message("restored-answer", "restored-message")]
        )
        current_agent = Agent(
            instructions="Original application instructions.",
            tools=[lookup_tool(restored_calls)],
            provider=destination.provider("synthetic-current-credential"),
        )
        async with current_agent.session(state=saved, context=current_context) as restored:
            assert restored.id == original.id
            assert [item async for item in restored.items()] == items
            assert [item async for item in restored.turns()] == turns
            # Completed Turns keep their result across the source Session's closure.
            assert (await submitted.get_result()).turn is submitted
            assert restored.restored_turn is None
            next_submission = await restored.submit("new-input")
            result = await (next_submission).get_result()
            assert result.final_output == "restored-answer"
            assert result.turn is next_submission
            assert next_submission.turn_id != submitted.turn_id
            assert next_submission.session_id == submitted.session_id
            assert len(restored_calls) == len(source_calls) == 1
            assert restored_calls[0].context is current_context
            assert restored_calls[0].submission is next_submission
            assert restored_calls[0].call_id == "restored-call"
            resaved = await restored.save_state()
            assert resaved.source_session_id == saved.source_session_id
            assert "synthetic-current-credential" not in resaved.to_json()
            assert "synthetic-saved-credential" not in resaved.to_json()
            assert current_context.private_value not in resaved.to_json()
            assert all(value not in resaved.to_json() for value in saved_credentials.values())
            assert conversation_value in resaved.to_json()
        assert len(source.requests) == len(destination.requests) == 2
        assert destination.authorization == ["Bearer synthetic-current-credential"] * 2
        assert "original-input" in str(destination.requests[0]["input"])
        assert "source-record" in str(destination.requests[0]["input"])
        assert "current-record" in str(destination.requests[1]["input"])
        assert "Original application instructions." in str(destination.requests[0]["input"])
        assert "Saved options must not replace" not in str(destination.requests)


async def test_checkpoint_rejects_active_callbacks_until_they_settle():
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def pending() -> str:
        entered.set()
        await release.wait()
        return "settled-tool-output"

    async with timeout(30), peer() as model:
        model.replies.extend([call("pending", "pending-call"), message("settled-answer")])
        agent = Agent(instructions="Use the tool.", tools=[pending], provider=model.provider())
        async with agent.session() as session:
            handle = await session.submit("start")
            await entered.wait()
            try:
                with pytest.raises(RuntimeError, match="requires an idle Session"):
                    await session.save_state()
            finally:
                release.set()
            assert (await (handle).get_result()).final_output == "settled-answer"
            state = await session.save_state()
            assert "settled-tool-output" in state.to_json()


@pytest.mark.parametrize("fail_export", [False, True])
@pytest.mark.usefixtures("core_owner")
async def test_cancelled_capture_keeps_ownership_and_close_joins_it(monkeypatch, fail_export):
    entered, release = asyncio.Event(), asyncio.Event()
    native_closed = asyncio.Event()
    failure = OSError("synthetic checkpoint export failure")
    async with timeout(30), peer() as model:
        agent = Agent(instructions="Answer.", provider=model.provider())
        async with agent.session() as session:
            native_thread = session._native

            class HeldExport:
                def __getattr__(self, name):
                    return getattr(native_thread, name)

                async def _export_checkpoint(self):
                    result = await native_thread._export_checkpoint()
                    entered.set()
                    await release.wait()
                    if fail_export:
                        raise failure
                    return result

                async def close(self):
                    await native_thread.close()
                    native_closed.set()

            monkeypatch.setattr(session, "_native", HeldExport())
            saving = asyncio.create_task(session.save_state())
            await entered.wait()
            saving.cancel("cancel capture")
            await asyncio.sleep(0)
            saving.cancel("cancel capture again")
            try:
                assert not saving.done()
                with pytest.raises(SessionBusyError):
                    await session.submit("must not be submitted")
                with pytest.raises(RuntimeError, match="saving a checkpoint"):
                    await session.save_state()
                closing = asyncio.create_task(session._aclose())
                await asyncio.sleep(0)
                assert not closing.done()
                assert not native_closed.is_set()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await saving
            await closing
            assert native_closed.is_set()
            assert session._checkpoint_task is None
            assert session._closed and session._reader.done()
            assert not Path(session._home.name).exists()
            assert not model.requests


async def test_failed_capture_releases_ownership_without_losing_history(monkeypatch):
    async with timeout(30), peer() as model:
        model.replies.extend([message("before"), message("after", "after-message")])
        agent = Agent(instructions="Answer.", provider=model.provider())
        async with agent.session() as session:
            first = await session.submit("first")
            await (first).get_result()
            original = session._native

            class FailedExport:
                def __getattr__(self, name):
                    return getattr(original, name)

                async def _export_checkpoint(self):
                    raise OSError("synthetic capture failure")

            monkeypatch.setattr(session, "_native", FailedExport())
            with pytest.raises(OSError, match="synthetic capture failure"):
                await session.save_state()
            monkeypatch.setattr(session, "_native", original)
            second = await session.submit("second")
            assert (await (second).get_result()).final_output == "after"
            assert [turn["id"] for turn in [item async for item in session.turns()]] == [
                first.turn_id,
                second.turn_id,
            ]
            assert (await session.save_state()).source_session_id == session.id


@pytest.mark.parametrize(
    ("change", "component"),
    [
        ("unmanaged", "missing native tool declarations"),
        ("interrupted", "interrupted native work"),
        ("descendants", "Restoring child state requires current NativeAgents"),
        ("pending", "pending tool decisions"),
    ],
)
async def test_unsupported_state_is_rejected_before_native_creation(monkeypatch, change, component):
    native = importlib.import_module("agents._native")
    async with timeout(30), peer() as model:
        agent = Agent(instructions="Answer.", provider=model.provider())
        async with agent.session() as source:
            raw = json.loads((await source.save_state()).to_json())
        if change == "unmanaged":
            raw["managed_tool_specs"] = None
        elif change == "interrupted":
            raw["interrupted_thread_ids"] = [raw["source_session_id"]]
        elif change == "descendants":
            raw["descendants"] = [
                {
                    "thread_id": "11111111-1111-1111-1111-111111111111",
                    "parent_thread_id": raw["source_session_id"],
                    "status": "active",
                    "metadata": {},
                    "history": [],
                }
            ]
        else:
            raw["pending_actions"] = [
                {
                    "type": "function_call",
                    "turn_id": "saved-turn",
                    "call_id": "saved-call",
                    "name": "lookup",
                    "arguments": {},
                }
            ]

        def forbidden_creation(*args, **kwargs):
            pytest.fail("unsupported state reached native engine creation")

        monkeypatch.setattr(native, "create_checkpoint_engine", forbidden_creation)
        with pytest.raises(ValueError) as error:
            async with agent.session(state=SessionState.from_json(json.dumps(raw))):
                pytest.fail("unsupported state restored")
        expected = (
            component
            if change == "descendants"
            else f"state must contain an idle native Agent session tree: {component}"
        )
        assert str(error.value) == expected
        assert not model.requests


async def test_restore_requires_current_context_and_exact_tool_declarations():
    calls: list[ToolContext[Application]] = []
    async with timeout(30), peer() as model:
        agent = Agent(instructions="Read.", tools=[lookup_tool(calls)], provider=model.provider())
        async with agent.session(context=Application("original")) as source:
            saved = await source.save_state()
        with pytest.raises(TypeError, match="application context"):
            async with cast(Any, agent).session(state=saved) as missing_context:
                selected = await missing_context.submit("use current lookup")
                await selected.get_result()
        changed = Agent(instructions="Read.", provider=model.provider())
        with pytest.raises(RuntimeError, match="current managed specs must match"):
            async with changed.session(state=saved):
                pytest.fail("restore accepted a different tool catalog")
        async with agent.session(state=saved, context=Application("current")) as restored:
            assert restored.id == saved.source_session_id
        assert not calls and not model.requests


@pytest.mark.parametrize("change", ["instructions", "unverifiable", "compacted"])
async def test_restore_rejects_instruction_changes_before_native_creation(monkeypatch, change):
    native = importlib.import_module("agents._native")
    async with timeout(30), peer() as model:
        model.replies.append(message("original"))
        agent = Agent(instructions="Original instructions.", provider=model.provider())
        async with agent.session() as source:
            handle = await source.submit("original-input")
            await (handle).get_result()
            raw = json.loads((await source.save_state()).to_json())
        if change == "instructions":
            agent = replace(agent, instructions="Changed instructions.")
            # Altering a separate options field must not satisfy this check.
            raw["agent_options"] = {"instructions": agent.instructions}
        elif change == "unverifiable":
            for record in raw["history"]:
                payload = record.get("payload", {})
                if record["type"] == "response_item" and payload.get("role") == "developer":
                    payload.pop("internal_chat_message_metadata_passthrough", None)
        else:
            raw["history"].append({"type": "compacted", "payload": {"message": "summary"}})

        def forbidden_creation(*args, **kwargs):
            pytest.fail("instruction mismatch reached native engine creation")

        monkeypatch.setattr(native, "create_checkpoint_engine", forbidden_creation)
        with pytest.raises(ValueError, match="instructions|Compacted"):
            async with agent.session(state=SessionState.from_json(json.dumps(raw))):
                pytest.fail("instruction mismatch restored")


@pytest.mark.parametrize("change", ["unverifiable", "compacted"])
async def test_save_rejects_histories_that_this_api_cannot_restore(monkeypatch, change):
    async with timeout(30), peer() as model:
        model.replies.append(message("original"))
        agent = Agent(instructions="Original instructions.", provider=model.provider())
        async with agent.session() as session:
            handle = await session.submit("original-input")
            await (handle).get_result()
            native_thread = session._native

            class UnsupportedHistory:
                def __getattr__(self, name):
                    return getattr(native_thread, name)

                async def _export_checkpoint(self):
                    raw = json.loads(await native_thread._export_checkpoint())
                    if change == "compacted":
                        raw["history"].append(
                            {"type": "compacted", "payload": {"message": "summary"}}
                        )
                    else:
                        for record in raw["history"]:
                            payload = record.get("payload", {})
                            if (
                                record["type"] == "response_item"
                                and payload.get("role") == "developer"
                            ):
                                payload.pop("internal_chat_message_metadata_passthrough", None)
                    return json.dumps(raw)

            monkeypatch.setattr(session, "_native", UnsupportedHistory())
            with pytest.raises(ValueError, match="instructions|Compacted"):
                await session.save_state()
            monkeypatch.setattr(session, "_native", native_thread)
            assert (await session.save_state()).source_session_id == session.id


async def test_cancelled_restore_joins_late_native_thread_and_engine_cleanup(monkeypatch):
    native = importlib.import_module("agents._native")
    create_engine = native.create_checkpoint_engine
    close_orphan = native.NativeProviderLease.close_orphan
    restored, release_restore = asyncio.Event(), asyncio.Event()
    closing, release_close = asyncio.Event(), asyncio.Event()
    closed: list[str] = []
    homes: list[Path] = []
    owners = []
    async with timeout(30), peer() as model:
        agent = Agent(instructions="Answer.", provider=model.provider())
        async with agent.session() as source:
            saved = await source.save_state()

        class Engine:
            def __init__(self, inner):
                self.inner = inner

            async def restore_checkpoint(self, *args):
                result = await self.inner.restore_checkpoint(*args)
                owners.append(result)
                restored.set()
                await release_restore.wait()
                return result

        async def create(*args, **kwargs):
            homes.append(Path(json.loads(args[1])["codex_home"]))
            result = await create_engine(*args, **kwargs)
            owners.append(result)
            return Engine(result)

        async def held_retirement(lease):
            closing.set()
            await release_close.wait()
            await close_orphan(lease)
            closed.append("native retirement")

        async def open_session():
            async with agent.session(state=saved):
                pytest.fail("cancelled restore was adopted")

        monkeypatch.setattr(native, "create_checkpoint_engine", create)
        monkeypatch.setattr(native.NativeProviderLease, "close_orphan", held_retirement)
        opening = asyncio.create_task(open_session())
        try:
            await restored.wait()
            opening.cancel("cancel restore")
            release_restore.set()
            await closing.wait()
            opening.cancel("cancel cleanup again")
            await asyncio.sleep(0)
            assert not opening.done()
            assert not closed
            release_close.set()
            with pytest.raises(asyncio.CancelledError):
                await opening
        finally:
            release_restore.set()
            release_close.set()
            await asyncio.gather(opening, return_exceptions=True)
        assert closed == ["native retirement"]
        assert len(owners) == 2
        with pytest.raises(RuntimeError, match="closed"):
            await owners[1].read_thread_history(None)
        with pytest.raises(RuntimeError, match="closed"):
            await owners[0].start_replaceable_thread("{}", "[]")
        assert len(homes) == 1 and not homes[0].exists()
        assert not model.requests
        await agent.provider.aclose()


@pytest.mark.parametrize("started", [False, True])
async def test_native_empty_and_never_started_instruction_records(started):
    async with timeout(30), peer() as model:
        agent = Agent(instructions="", provider=model.provider())
        async with agent.session() as original:
            if started:
                model.replies.append(message("first"))
                await (await original.submit("first-input")).get_result()
            saved = await original.save_state()
        # Initial publication makes even a zero-turn empty instruction verifiable.
        if not started:
            agent = replace(agent, instructions="First application instructions.")
            with pytest.raises(ValueError, match="latest accepted instructions"):
                async with agent.session(state=saved):
                    pytest.fail("empty initial instructions were overwritten during restore")
            agent = replace(agent, instructions="")
        async with agent.session(state=saved) as restored:
            model.replies.append(message("next", "next-message"))
            assert (await (await restored.submit("next-input")).get_result()).final_output == "next"
            assert (await restored.save_state()).source_session_id == saved.source_session_id
        assert "First application instructions." not in str(model.requests[-1]["input"])


async def test_native_restore_rejection_closes_owner_and_allows_valid_retry(monkeypatch):
    native = importlib.import_module("agents._native")
    create_engine = native.create_checkpoint_engine
    homes: list[Path] = []
    owners = []
    async with timeout(30), peer() as model:
        agent = Agent(instructions="Answer.", provider=model.provider())
        async with agent.session() as original:
            saved = await original.save_state()

        async def create(*args, **kwargs):
            homes.append(Path(json.loads(args[1])["codex_home"]))
            engine = await create_engine(*args, **kwargs)
            owners.append(engine)
            return engine

        monkeypatch.setattr(native, "create_checkpoint_engine", create)
        invalid = saved.model_copy(update={"codex_revision": "incompatible-native-revision"})
        with pytest.raises(RuntimeError, match="checkpoint core revision differs"):
            async with agent.session(state=invalid):
                pytest.fail("invalid native state restored")
        assert not owners
        assert len(homes) == 1 and not homes[0].exists()
        assert not model.requests
        async with agent.session(state=saved) as restored:
            model.replies.append(message("retry"))
            assert (await (await restored.submit("continue")).get_result()).final_output == "retry"
            assert restored.id == saved.source_session_id
        assert len(owners) == 1 and len(homes) == 2
        for engine in owners:
            with pytest.raises(RuntimeError, match="closed"):
                await engine.start_replaceable_thread("{}", "[]")
        assert all(not home.exists() for home in homes)
        assert len(model.requests) == 1
        await agent.provider.aclose()


def test_checkpoint_declarations_import_without_runtime_or_legacy_modules():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import agents
from agents import Agent, Session
from agents.state import SessionState
Agent(instructions='Declare only.')
assert {"Agent", "Session", "MCPResultContext"} <= set(agents.__all__)
assert 'agents._native' not in sys.modules
assert 'agents.sdk' not in sys.modules
assert not any(name == 'codex_agents' or name.startswith('codex_agents.') for name in sys.modules)
""",
        ],
        check=True,
    )


@pytest.mark.usefixtures("core_owner")
async def test_idle_saved_subset_restores_without_native_agents(tmp_path, monkeypatch):
    from agents import NativeAgents, ToolRef
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_agent import function_declarations
    from tests.test_native_child_agents import trust
    from tests.test_native_executor_tools import declaration

    fixed = [ToolRef(name, "functions") for name in ("exec_command", "write_stdin", "apply_patch")]
    fixed += [
        ToolRef(name, "collaboration")
        for name in (
            "spawn_agent",
            "send_message",
            "wait_agent",
            "list_agents",
            "interrupt_agent",
            "followup_task",
        )
    ]
    source_calls = []
    current_calls = []
    first = lookup_tool(source_calls)
    current_first = lookup_tool(current_calls)

    @function_tool
    async def second() -> str:
        """Read the second current binding."""
        return "current-second"

    async with (
        timeout(30),
        peer() as model,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        source = Agent(
            instructions="Read records.",
            model="gpt-5.5",
            tools=[first, second, NativeAgents()],
            provider=model.provider(),
        )
        model.replies.append(message("selected result"))
        async with source.session(
            context=Application("source-record"),
            # The destination drops its Environment along with NativeAgents.
            environment=declaration(executor, write_policy="legacy"),
        ) as session:
            await (await session.submit("Select first.", tools=[first, *fixed])).get_result()
            # Capture a legacy selected generation without republishing save defaults.
            saved = await session._checkpoint(None, probe=True)
        assert not saved.descendants
        assert [spec["name"] for spec in saved.managed_tool_specs] == ["lookup"]
        destination = Agent(
            instructions="Read records.",
            model="gpt-5.5",
            tools=[current_first, second],
            provider=model.provider(),
        )
        model.replies.extend(
            [
                call("lookup", "current-first"),
                call("second", "current-second"),
                message("restored result"),
            ]
        )
        before = len(model.requests)
        async with destination.session(
            state=saved, context=Application("current-record")
        ) as restored:
            completed = await (
                await restored.submit("Use current bindings.", tools=[current_first, second])
            ).get_result()
            assert completed.final_output == "restored result"
        assert not source_calls
        assert len(current_calls) == 1 and current_calls[0].context.record == "current-record"
        assert {tool["name"] for tool in function_declarations(model.requests[before])} == {
            "lookup",
            "second",
        }
        wire = json.dumps(model.requests[before:])
        assert "current-record" in wire and "current-second" in wire
        assert "source-record" not in wire
