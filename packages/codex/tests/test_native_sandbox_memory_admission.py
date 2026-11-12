import asyncio
import json
import os

import pytest

from agents import Agent, GuardrailResult, InputGuardrail
from agents.classic.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxClient,
    UnixLocalSandboxClientOptions,
)
from agents.sandbox import Manifest, SandboxMemoryOptions, sandbox_environment
from agents.testing import ScriptedResponses, text
from tests._asyncio import timeout
from tests.test_native_sandbox_memory import _workspace_gateway

pytestmark = pytest.mark.skipif(os.name == "nt", reason="UnixLocal memory fixture")


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("failure", ["trip", "raise", "invalid"])
@pytest.mark.parametrize("later", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
async def test_rejected_input_never_enters_memory(
    tmp_path, monkeypatch, failure, later, parallel, previous
):
    allowed = False
    model_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    marker = "SYNTHETIC_REJECTED_MEMORY_INPUT"
    accepted = "SYNTHETIC_ACCEPTED_MEMORY_INPUT"
    error = ValueError("synthetic input check failure")

    async def check(turn, context, agent, value):
        if allowed:
            return GuardrailResult(False)
        if parallel:
            await model_requested.wait()
        if failure == "raise":
            raise error
        if failure == "invalid":
            return None
        return GuardrailResult(True)

    extraction = {
        "rollout_slug": "input-admission",
        "rollout_summary": "Accepted context.",
        "raw_memory": "Accepted context.",
    }
    consolidation = {"summary": "Accepted context.", "memory": "Accepted context."}

    def rejected_model(request):
        loop.call_soon_threadsafe(model_requested.set)
        return [text("Rejected parallel candidate.")]

    replies = ([rejected_model] if parallel else []) + (
        [
            [text("Accepted source answer.")],
            [text(json.dumps(extraction))],
            [text(json.dumps(consolidation))],
        ]
        if later
        else []
    )
    if previous:
        replies.insert(0, [text("Previously admitted source answer.")])
    offset = int(previous)
    workspace = tmp_path / "workspace"
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(*replies) as peer,
    ):
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Extract memory.", provider=peer.provider),
                phase_two=Agent(instructions="Consolidate memory.", provider=peer.provider),
            ),
        )
        agent = Agent(
            instructions="Answer.",
            provider=peer.provider,
            input_guardrails=[InputGuardrail("source-check", check, run_in_parallel=parallel)],
        )
        async with agent.session(environment=environment) as session:
            if previous:
                allowed = True
                await (await session.submit("SYNTHETIC_PREVIOUSLY_ADMITTED_SOURCE")).get_result()
                allowed = False
            rejected = await session.submit(marker)
            with pytest.raises(Exception) as original:
                await rejected.get_result()
            assert rejected.turn_id is not None
            assert len(peer.requests) == offset + int(parallel)
            if not later:
                with pytest.raises(type(original.value)) as after:
                    await session.flush_memory()
                assert after.value is original.value
                assert len(peer.requests) == offset + int(parallel)
                assert not (workspace / "sessions").exists()
                return
            allowed = True
            await (await session.submit(accepted)).get_result()
            history = await session.history_snapshot()
            if parallel:
                assert marker in json.dumps(history.items)
            await session.flush_memory()
            records = (workspace / "sessions" / f"{session.id}.jsonl").read_text()
            assert marker not in records
            assert accepted in records
            assert marker not in json.dumps(peer.requests[1 + offset + int(parallel)]["input"])
            assert len(peer.requests) == 3 + offset + int(parallel)


@pytest.mark.parametrize("failure", ["raise", "invalid"])
async def test_pending_root_input_check_cannot_be_admitted_by_unguarded_handoff(
    tmp_path, monkeypatch, failure
):
    from agents.testing import tool_call

    workspace = tmp_path / "workspace"
    worker_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    rejected = "SYNTHETIC_REJECTED_ROOT_HANDOFF_INPUT"
    original_failure = ValueError("SYNTHETIC_ROOT_PARALLEL_CHECK_FAILURE")

    async def root_check(turn, context, agent, value):
        await worker_requested.wait()
        if failure == "raise":
            raise original_failure
        return None

    def worker_response(request):
        loop.call_soon_threadsafe(worker_requested.set)
        return [text("Worker output before the input check settles.")]

    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [tool_call("transfer_to_worker", {}, call_id="root-handoff")],
            worker_response,
        ) as peer,
    ):
        worker = Agent(
            name="Worker", instructions="Answer.", model="gpt-5.5", provider=peer.provider
        )
        root = Agent(
            name="Root",
            instructions="Hand off to Worker.",
            model="gpt-5.5",
            provider=peer.provider,
            handoffs=[worker],
            input_guardrails=[InputGuardrail("root-check", root_check, run_in_parallel=True)],
        )
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Extract.", provider=peer.provider),
                phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
            ),
        )
        async with root.session(environment=environment) as session:
            turn = await session.submit(rejected)
            with pytest.raises(Exception) as original:
                await turn.get_result()
            if failure == "raise":
                assert original.value is original_failure
            assert len(peer.requests) == 2
            with pytest.raises(type(original.value)) as refused:
                await session.flush_memory()
            assert refused.value is original.value
            assert len(peer.requests) == 2
            assert not (workspace / "sessions").exists()
