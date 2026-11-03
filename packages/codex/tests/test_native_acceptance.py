"""Native receipt and checkpoint operations retain their exact owners."""

import asyncio
import json
from contextlib import AsyncExitStack

import pytest
from test_native_completion_lifetime import ReceiptGate

from agents import Agent, NativeProvider, _native
from tests._asyncio import timeout
from tests.native_fixtures import (
    core_owner as core_owner,
    native_engine,
    native_options,
    responses as responses,
)


@pytest.mark.parametrize("outcome", ["success", "cancel", "failure"])
async def test_public_close_and_raw_failure_wait_for_their_own_native_receipt(
    tmp_path, responses, monkeypatch, request, outcome
):
    # The failure case injects a raw Core Engine operation. Keep public close
    # cases on the ordinary owner, and select Core only for its private API.
    if outcome == "failure":
        request.getfixturevalue("core_owner")
    agent = Agent(
        instructions="Answer.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-native-publication-key"),
    )
    async with timeout(30), AsyncExitStack() as stack:
        first = await stack.enter_async_context(agent.session())
        survivor = await stack.enter_async_context(agent.session())
        task = closer = None
        gate = ReceiptGate(
            monkeypatch,
            selected=lambda owner: (
                owner is first._close_task or (outcome == "failure" and owner is task)
            ),
        )
        gate.armed = True
        try:
            if outcome == "failure":
                task = asyncio.create_task(
                    first._engine.resume_thread(str(tmp_path / "no-such-rollout.jsonl"), "{}")
                )
            else:
                task = asyncio.create_task(first._aclose())
            await asyncio.wait_for(gate.started.wait(), 10)
            assert gate.writer.fileno() >= 0
            if outcome == "failure":
                closer = asyncio.create_task(first._aclose())
            if outcome == "cancel":
                task.cancel("first cancellation")
            await survivor._aclose()
            if outcome == "cancel":
                task.cancel("later cancellation")
            async with agent.session() as later:
                assert later.id
            assert gate.writer.fileno() >= 0
            assert not task.done(), "Native completion escaped before its physical EOF"
            if closer is not None:
                # This private Engine call was started outside Session ownership.
                # Its own await must retain the failed receipt after Session closes.
                await closer
                assert not task.done() and gate.writer.fileno() >= 0
            gate.release()
            if outcome == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "failure":
                with pytest.raises(RuntimeError):
                    await task
            else:
                await task
            if closer is not None:
                await closer
            assert gate.writer.fileno() == -1
        finally:
            gate.release()
            await asyncio.gather(
                *(item for item in (task, closer) if item is not None), return_exceptions=True
            )


async def test_native_checkpoint_fork_readbacks_and_binding_errors(tmp_path, responses):
    options = native_options(tmp_path, responses)
    options["runtime"]["ephemeral"] = False
    async with timeout(25), native_engine(options, checkpoint=True) as engine:
        thread = await engine.start_replaceable_thread("{}", "[]")
        try:
            assert json.loads(await thread.read_checklist()) is None
            checkpoint = json.loads(await thread._export_checkpoint())
            assert checkpoint["source_session_id"] == thread.id
            assert _native.Engine.__module__ == "agents._native"
            with pytest.raises(RuntimeError):
                await thread.call("not_a_native_operation")
            child = await engine.fork_thread(thread.id, specs_json="[]")
            try:
                assert child.id != thread.id
                assert isinstance(json.loads(await child._read_model_context())["items"], list)
            finally:
                await child.close()
        finally:
            await thread.close()
