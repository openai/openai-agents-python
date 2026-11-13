import asyncio
import json
import os
import sys

import pytest

from agents import Agent, GuardrailResult, InputGuardrail
from agents.sandbox import Manifest, SandboxMemoryOptions, sandbox_environment
from agents.testing import ScriptedResponses, text
from tests._asyncio import timeout
from tests.test_native_sandbox_memory import _workspace_gateway

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

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


@pytest.mark.parametrize("failure", ["trip", "raise", "invalid"])
async def test_rejected_source_does_not_block_managed_workspace_capture(
    tmp_path, monkeypatch, failure
):
    from agents.sandbox import LocalSnapshotSpec

    workspace = tmp_path / "workspace"

    def check(turn, context, agent, value):
        if failure == "raise":
            raise ValueError("SYNTHETIC_REJECTED_CAPTURE_INPUT")
        if failure == "invalid":
            return None
        return GuardrailResult(True)

    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses() as peer,
    ):
        environment = sandbox_environment(
            UnixLocalSandboxClient(),
            manifest=Manifest(root=str(workspace)),
            options=UnixLocalSandboxClientOptions(),
            connect=connect,
            managed_workspace=True,
            snapshot=LocalSnapshotSpec(base_path=tmp_path / "archives"),
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Extract.", provider=peer.provider),
                phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
            ),
        )
        agent = Agent(
            instructions="Answer.",
            provider=peer.provider,
            input_guardrails=[InputGuardrail("source-check", check, run_in_parallel=False)],
        )
        async with agent.session(environment=environment) as session:
            turn = await session.submit("SYNTHETIC_REJECTED_CAPTURE_INPUT")
            with pytest.raises(Exception) as original:
                await turn.get_result()
            with pytest.raises(type(original.value)) as refused:
                await session.flush_memory()
            assert refused.value is original.value
            helper_root = workspace / ".fixture-config" / "tmp"
            if helper_root.exists():
                for helper in helper_root.iterdir():
                    if helper.is_symlink():
                        helper.unlink()
            pair = await session.suspend_workspace()
            assert pair.workspace["provider_state"] is not None
            assert not peer.requests
            assert not (workspace / "sessions").exists()


@pytest.mark.parametrize("edit", [False, True], ids=["import", "edit"])
async def test_imported_turn_id_does_not_authorize_changed_memory_content(
    tmp_path, monkeypatch, edit
):
    workspace = tmp_path / "workspace"
    marker = "SYNTHETIC_UNADMITTED_HISTORY_EDIT"
    extraction = {
        "rollout_slug": "edited",
        "rollout_summary": "Accepted.",
        "raw_memory": "Accepted.",
    }
    consolidation = {"summary": "Accepted.", "memory": "Accepted."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [text("First accepted answer.")],
            [text("Next accepted answer.")],
            [text(json.dumps(extraction))],
            [text(json.dumps(consolidation))],
        ) as peer,
    ):
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
        async with Agent(instructions="Answer.", provider=peer.provider).session(
            environment=environment
        ) as session:
            first = await session.submit("SYNTHETIC_FIRST_ACCEPTED_SOURCE")
            await first.get_result()
            before = await session.history_snapshot()
            imported = json.loads(json.dumps(before.items[-1]))
            imported["content"] = [{"type": "output_text", "text": marker}]
            if edit:
                keep = before.total - 1
                items = [imported]
            else:
                imported["id"] = "msg_synthetic_unadmitted_import"
                keep, items = before.total, [imported]
            await session.transact_history(
                expected_revision=before.revision, keep=keep, items=items
            )
            await (await session.submit("SYNTHETIC_FRESH_ACCEPTED_SOURCE")).get_result()
            assert marker in json.dumps((await session.history_snapshot()).items)
            await session.flush_memory()
            rollout = (workspace / "sessions" / f"{session.id}.jsonl").read_text()
            assert marker not in rollout
            assert marker not in json.dumps(peer.requests[2]["input"])
            assert "SYNTHETIC_FRESH_ACCEPTED_SOURCE" in rollout
            assert "SYNTHETIC_FIRST_ACCEPTED_SOURCE" in rollout
