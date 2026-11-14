"""Manual checkpoints keep the canonical writer until native capture has joined."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agents import (
    Agent,
    CustomStorage,
    FunctionTool,
    NativeProvider,
    SQLiteSessionStore,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, next_request


def lookup_tool():
    return FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)


def provider_for(url: str):
    return NativeProvider(base_url=url, api_key="synthetic-manual-store", supports_websockets=False)


async def resume_child(url: str, state_path: str) -> None:
    lookup = lookup_tool()
    async with timeout(15), provider_for(url) as provider:
        agent = Agent(
            instructions="Read exactly once.", model="gpt-5.5", provider=provider, tools=[lookup]
        )
        async with agent.session(
            state=SessionState.from_json(Path(state_path).read_text())
        ) as session:
            request = await next_request(session.interactions(), lookup)
            assert request.call_id == "manual-A"
            await request.respond("answer-A")
            assert session.restored_turn is not None
            assert (await session.restored_turn.get_result()).final_output == "done"


@pytest.mark.parametrize("independent_process", [False, True])
async def test_manual_custom_storage_round_trip(responses, tmp_path, independent_process):
    backend = SQLiteSessionStore(tmp_path / "manual.sqlite")
    storage = CustomStorage(backend)
    lookup = lookup_tool()
    async with timeout(25), provider_for(responses.url) as provider:
        agent = Agent(
            instructions="Read exactly once.", model="gpt-5.5", provider=provider, tools=[lookup]
        )
        responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
        responses.respond(assistant_output("done"))
        async with agent.session(storage=storage) as source:
            await source.submit("read")
            request = await next_request(source.interactions(), lookup)
            assert request.call_id == "manual-A"
            captured = await source.suspend()
            assert (await source.suspend()).to_json() == captured.to_json()
            saved = SessionState.from_json(captured.to_json())
            token = backend.acquire(source.id)
            try:
                assert backend.read(source.id, token) is not None
            finally:
                backend.release(source.id, token)
        if independent_process:
            state_path = tmp_path / "manual-state.json"
            state_path.write_text(saved.to_json())
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import asyncio,sys; "
                "from tests.test_native_custom_storage_manual import resume_child; "
                "asyncio.run(resume_child(sys.argv[1], sys.argv[2]))",
                responses.url,
                str(state_path),
                env=os.environ.copy(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await child.communicate()
            assert child.returncode == 0, stdout.decode() + stderr.decode()
        else:
            async with agent.session(state=saved) as restored:
                request = await next_request(restored.interactions(), lookup)
                await request.respond("answer-A")
                assert restored.restored_turn is not None
                assert (await restored.restored_turn.get_result()).final_output == "done"
    assert len(responses.requests) == 2


async def test_manual_custom_storage_keeps_completed_sibling_once(responses, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @function_tool
    async def record() -> str:
        """Record an application effect once."""
        effects.append("recorded")
        entered.set()
        await release.wait()
        return "sibling-result"

    lookup = lookup_tool()
    responses.respond(
        function_call("record", {}, "sibling"),
        function_call("lookup", {"key": "A"}, "manual-A"),
    )
    responses.respond(assistant_output("done"))
    async with timeout(25), provider_for(responses.url) as provider:
        agent = Agent(
            instructions="Read exactly once.",
            model="gpt-5.5",
            provider=provider,
            tools=[record, lookup],
        )
        async with agent.session(
            storage=CustomStorage(SQLiteSessionStore(tmp_path / "siblings.sqlite"))
        ) as source:
            await source.submit("record and read")
            await entered.wait()
            await next_request(source.interactions(), lookup)
            pending = asyncio.create_task(source.suspend())
            await asyncio.sleep(0)
            assert not pending.done()
            release.set()
            saved = SessionState.from_json((await pending).to_json())
        async with agent.session(state=saved) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("answer-A")
            assert restored.restored_turn is not None
            assert (await restored.restored_turn.get_result()).final_output == "done"
    assert effects == ["recorded"]
    assert len(responses.requests) == 2
    outputs = [
        item
        for item in responses.requests[-1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert sum(item.get("call_id") == "sibling" for item in outputs) == 1
    assert (
        next(item["output"] for item in outputs if item.get("call_id") == "sibling")
        == "sibling-result"
    )


@pytest.mark.parametrize("invalid", ["frame", "version"])
async def test_custom_manual_restore_rejects_invalid_state_before_sampling(
    responses, tmp_path, invalid
):
    lookup = lookup_tool()
    responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
    responses.respond(assistant_output("done"))
    async with timeout(20), provider_for(responses.url) as provider:
        agent = Agent(
            instructions="Read exactly once.", model="gpt-5.5", provider=provider, tools=[lookup]
        )
        async with agent.session(
            storage=CustomStorage(SQLiteSessionStore(tmp_path / "tamper.sqlite"))
        ) as source:
            await source.submit("read")
            await next_request(source.interactions(), lookup)
            good = await source.suspend()
        raw = json.loads(good.to_json())
        if invalid == "version":
            raw["format_version"] = 999
            with pytest.raises(ValueError):
                SessionState.from_json(json.dumps(raw))
        else:
            raw["native_continuation"]["pending_manual"][0]["arguments"] = '{"key":"other"}'
            forged = SessionState.from_json(json.dumps(raw))
            with pytest.raises(RuntimeError, match="differ from accepted history"):
                async with agent.session(state=forged):
                    pytest.fail("Invalid manual state opened")
        assert len(responses.requests) == 1
        async with agent.session(state=good) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("answer-A")
            assert restored.restored_turn is not None
            assert (await restored.restored_turn.get_result()).final_output == "done"
    assert len(responses.requests) == 2


async def test_manual_custom_storage_refuses_lost_writer_before_capture(responses, tmp_path):
    class ReplacedOwnerStore(SQLiteSessionStore):
        armed = False
        successor = None

        def flush(self, session_id, token):
            if self.armed:
                self.armed = False
                super().release(session_id, token)
                self.successor = super().acquire(session_id)
            return super().flush(session_id, token)

    backend = ReplacedOwnerStore(tmp_path / "fenced.sqlite")
    lookup = lookup_tool()
    responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
    async with timeout(20), provider_for(responses.url) as provider:
        agent = Agent(
            instructions="Read exactly once.", model="gpt-5.5", provider=provider, tools=[lookup]
        )
        manager = agent.session(storage=CustomStorage(backend))
        source = await manager.__aenter__()
        try:
            await source.submit("read")
            await next_request(source.interactions(), lookup)
            backend.armed = True
            with pytest.raises(Exception, match="Canonical storage failed"):
                await source.suspend()
            assert backend.successor is not None
            assert backend.read(source.id, backend.successor) is not None
            assert len(responses.requests) == 1
        finally:
            try:
                await manager.__aexit__(None, None, None)
            except Exception as error:
                assert "Canonical storage failed" in str(error)
            if backend.successor is not None:
                # A stale native cleanup must not have released the successor.
                assert backend.read(source.id, backend.successor) is not None
                backend.release(source.id, backend.successor)
