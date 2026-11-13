import asyncio
import json
import os
import sys

import pytest

from agents import Agent, TurnStoppedError, function_tool
from agents.sandbox import Manifest, SandboxMemoryOptions, sandbox_environment
from agents.testing import ScriptedResponses, text, tool_call
from tests._asyncio import timeout
from tests.test_native_sandbox_memory import _workspace_gateway

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

pytestmark = pytest.mark.skipif(os.name == "nt", reason="UnixLocal memory fixture")


@pytest.mark.parametrize("capture", [False, True], ids=["flush", "suspend"])
@pytest.mark.parametrize("failed", [False, True], ids=["compact", "private-error"])
async def test_memory_keeps_source_admission_and_excludes_private_diagnostics(
    tmp_path, monkeypatch, capture, failed
):
    from agents.sandbox import LocalSnapshotSpec

    workspace = tmp_path / "workspace"
    marker = "SYNTHETIC_PRIVATE_TERMINAL_DETAIL"
    original = ValueError(marker)

    @function_tool(error_function=None)
    def failing_lookup() -> str:
        raise original

    extraction = {
        "rollout_slug": "settled",
        "rollout_summary": "Context.",
        "raw_memory": "Context.",
    }
    consolidation = {"summary": "Context.", "memory": "Context."}
    replies = (
        [[tool_call("failing_lookup", {}, call_id="private-error")]]
        if failed
        else [[text("Accepted source answer.")], [text("SYNTHETIC_COMPACTION_SUMMARY")]]
    )
    replies += [[text(json.dumps(extraction))], [text(json.dumps(consolidation))]]
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
            managed_workspace=capture,
            snapshot=LocalSnapshotSpec(base_path=tmp_path / "archives") if capture else None,
            memory=SandboxMemoryOptions(
                phase_one=Agent(instructions="Extract.", provider=peer.provider),
                phase_two=Agent(instructions="Consolidate.", provider=peer.provider),
            ),
        )
        agent = Agent(
            instructions="Answer.", provider=peer.provider, tools=[failing_lookup] if failed else []
        )
        async with agent.session(environment=environment) as session:
            turn = await session.submit("SYNTHETIC_ADMITTED_SOURCE_CONTEXT")
            if failed:
                with pytest.raises(ValueError) as caller:
                    await turn.get_result()
                assert caller.value is original and marker in str(caller.value)
            else:
                assert (await turn.get_result()).final_output == "Accepted source answer."
                await session.compact()
            if capture:
                helper_root = workspace / ".fixture-config" / "tmp"
                if helper_root.exists():
                    for helper in helper_root.iterdir():
                        if helper.is_symlink():
                            helper.unlink()
                await session.suspend_workspace()
            else:
                await session.flush_memory()
                await session.flush_memory()
            rollout = (workspace / "sessions" / f"{session.id}.jsonl").read_text()
            metadata = json.loads(rollout)["terminal_metadata"]
            assert metadata["terminal_state"] == ("failed" if failed else "completed")
            assert metadata["exception_message"] is None
            assert marker not in rollout
            phase_one = json.dumps(peer.requests[1 if failed else 2]["input"])
            assert marker not in phase_one
            assert "SYNTHETIC_ADMITTED_SOURCE_CONTEXT" in rollout
            assert "SYNTHETIC_COMPACTION_SUMMARY" not in rollout
            if failed:
                assert metadata["exception_type"] == "ValueError"
                with pytest.raises(ValueError) as after:
                    await turn.get_result()
                assert after.value is original
            else:
                assert (await turn.get_result()).final_output == "Accepted source answer."
            assert len(peer.requests) == (3 if failed else 4)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_terminal_continuation_keeps_public_root_memory_ownership(
    tmp_path, monkeypatch, cancelled
):
    from agents.observation import TurnStarted
    from tests.test_review_ii_steering_tail import ProjectContinuation

    workspace = tmp_path / "workspace"
    entered, hold = asyncio.Event(), asyncio.Event()

    @function_tool(error_function=None)
    async def continuation_lookup() -> str:
        entered.set()
        if not cancelled:
            raise ValueError("SYNTHETIC_CONTINUATION_FAILURE")
        await hold.wait()
        return "unexpected"

    extract = {
        "rollout_slug": "continuation",
        "rollout_summary": "Stopped.",
        "raw_memory": "Context.",
    }
    consolidate = {"summary": "Context.", "memory": "Context."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [text("Initial native result.")],
            [tool_call("continuation_lookup", {}, call_id="continuation-tool")],
            [text(json.dumps(extract))],
            [text(json.dumps(consolidate))],
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
        agent = Agent(instructions="Answer.", provider=peer.provider, tools=[continuation_lookup])
        async with agent.session(environment=environment) as session:
            # Existing controlled-tail projection retains real native turn IDs,
            # model requests, callbacks and the public continuation join.
            monkeypatch.setattr(session, "_native", ProjectContinuation(session._native))
            turn = await session.submit("SYNTHETIC_PUBLIC_ROOT_SOURCE")
            await entered.wait()
            if cancelled:
                await turn.cancel()
            error_type = TurnStoppedError if cancelled else ValueError
            with pytest.raises(error_type) as original:
                await turn.get_result()
            started = [event async for event in turn.events() if isinstance(event, TurnStarted)]
            assert len({event.turn_id for event in started}) == 2
            await session.flush_memory()
            await session.flush_memory()
            with pytest.raises(error_type) as after:
                await turn.get_result()
            assert after.value is original.value
            lines = (workspace / "sessions" / f"{session.id}.jsonl").read_text().splitlines()
            assert len(lines) == 1
            assert json.loads(lines[0])["terminal_metadata"]["terminal_state"] == (
                "cancelled" if cancelled else "failed"
            )
            assert "SYNTHETIC_PUBLIC_ROOT_SOURCE" in lines[0]
            assert len(peer.requests) == 4


@pytest.mark.parametrize("terminal_state", ["cancelled", "failed"])
async def test_terminal_source_flushes_once_and_preserves_the_original_outcome(
    tmp_path, monkeypatch, terminal_state
):
    workspace = tmp_path / "workspace"
    entered = asyncio.Event()
    block = asyncio.Event()
    calls = []

    @function_tool(error_function=None)
    async def pending_lookup() -> str:
        calls.append("entered")
        entered.set()
        if terminal_state == "failed":
            raise ValueError("owned source failure")
        await block.wait()
        return "unexpected"

    extract = {
        "rollout_slug": "cancelled",
        "rollout_summary": "Stopped.",
        "raw_memory": "Keep context.",
    }
    consolidate = {"summary": "Retain partial history.", "memory": "Keep context."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [tool_call("pending_lookup", {}, call_id="source-tool")],
            [text(json.dumps(extract))],
            [text(json.dumps(consolidate))],
        ) as peer,
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
        agent = Agent(instructions="Use the tool.", provider=peer.provider, tools=[pending_lookup])
        async with agent.session(environment=environment) as session:
            turn = await session.submit("Keep this context.")
            await entered.wait()
            if terminal_state == "cancelled":
                await turn.cancel()
            error_type = TurnStoppedError if terminal_state == "cancelled" else ValueError
            with pytest.raises(error_type) as original:
                await turn.get_result()
            await session.flush_memory()
            await session.flush_memory()
            with pytest.raises(error_type) as after:
                await turn.get_result()
            assert after.value is original.value
            lines = (workspace / "sessions" / f"{session.id}.jsonl").read_text().splitlines()
            assert len(lines) == 1
            rollout = json.loads(lines[0])
            assert rollout["terminal_metadata"]["terminal_state"] == terminal_state
            assert rollout["terminal_metadata"]["exception_type"] == error_type.__name__
            assert rollout["terminal_metadata"]["has_final_output"] is False
            assert (
                f"terminal_state: {terminal_state}"
                in (workspace / "memories" / "raw_memories" / f"{session.id}.md").read_text()
            )
            assert (
                f"terminal_state: {terminal_state}"
                in (workspace / "memories" / "rollout_summaries" / f"{session.id}.md").read_text()
            )
            assert terminal_state in json.dumps(peer.requests[2]["input"])
            assert len(peer.requests) == 3
            assert calls == ["entered"]


async def test_force_stop_joins_held_admission_without_overwriting_stop(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    tool_entered, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool(error_function=None)
    async def pending_source() -> str:
        tool_entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    extraction = {"rollout_slug": "held", "rollout_summary": "Stopped.", "raw_memory": "Stopped."}
    consolidation = {"summary": "Stopped.", "memory": "Stopped."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [tool_call("pending_source", {}, call_id="held-source")],
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
        async with Agent(
            instructions="Use the tool.", provider=peer.provider, tools=[pending_source]
        ).session(environment=environment) as session:
            native = session._native

            class HeldAdmission:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _record_memory_input_admission(self, turn_id):
                    entered.set()
                    await release.wait()
                    return await native._record_memory_input_admission(turn_id)

            session._native = HeldAdmission()
            turn = await session.submit("SYNTHETIC_HELD_SOURCE")
            await tool_entered.wait()
            stop = asyncio.create_task(turn.cancel())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                assert not stop.done()
                with pytest.raises(RuntimeError, match="busy|interruption"):
                    await session.flush_memory()
            finally:
                release.set()
            await stop
            with pytest.raises(TurnStoppedError) as original:
                await turn.get_result()
            await session.flush_memory()
            with pytest.raises(TurnStoppedError) as repeated:
                await turn.get_result()
            assert repeated.value is original.value
            rollout = json.loads((workspace / "sessions" / f"{session.id}.jsonl").read_text())
            assert rollout["terminal_metadata"]["terminal_state"] == "cancelled"
            assert len(peer.requests) == 3


@pytest.mark.parametrize("raises", [False, True])
async def test_output_guardrail_terminal_remains_memory_eligible(tmp_path, monkeypatch, raises):
    from agents import GuardrailResult, GuardrailTripwire, OutputGuardrail

    workspace = tmp_path / "workspace"
    failure = ValueError("SYNTHETIC_OUTPUT_GUARD_FAILURE")

    async def output_check(completion, agent):
        if raises:
            raise failure
        return GuardrailResult(True)

    extraction = {
        "rollout_slug": "output",
        "rollout_summary": "Output blocked.",
        "raw_memory": "Accepted input.",
    }
    consolidation = {"summary": "Accepted input.", "memory": "Accepted input."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [text("SYNTHETIC_GUARDED_OUTPUT")],
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
        agent = Agent(
            instructions="Answer.",
            provider=peer.provider,
            output_guardrails=[OutputGuardrail("output-check", output_check)],
        )
        async with agent.session(environment=environment) as session:
            turn = await session.submit("SYNTHETIC_ACCEPTED_OUTPUT_GUARD_SOURCE")
            with pytest.raises(ValueError if raises else GuardrailTripwire) as original:
                await turn.get_result()
            if raises:
                assert original.value is failure
            await session.flush_memory()
            await session.flush_memory()
            with pytest.raises(type(original.value)) as repeated:
                await turn.get_result()
            assert repeated.value is original.value
            rollout = json.loads((workspace / "sessions" / f"{session.id}.jsonl").read_text())
            assert rollout["terminal_metadata"]["terminal_state"] == (
                "failed" if raises else "guardrail_tripped"
            )
            assert rollout["terminal_metadata"]["has_final_output"] is False
            assert "SYNTHETIC_ACCEPTED_OUTPUT_GUARD_SOURCE" in json.dumps(rollout["items"])
            assert len(peer.requests) == 3


async def test_actual_graceful_stop(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    entered, release, marked = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool(error_function=None)
    async def pending_lookup() -> str:
        entered.set()
        await release.wait()
        return "SYNTHETIC_RESULT"

    extraction = {
        "rollout_slug": "graceful",
        "rollout_summary": "Stopped.",
        "raw_memory": "Stopped.",
    }
    consolidation = {"summary": "Stopped.", "memory": "Stopped."}
    async with (
        timeout(25),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [tool_call("pending_lookup", {}, call_id="graceful-source")],
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
        async with Agent(
            instructions="Use the tool.", provider=peer.provider, tools=[pending_lookup]
        ).session(environment=environment) as session:
            native = session._native

            class GracefulNative:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _request_graceful_interrupt(self, turn_id):
                    try:
                        return await native._request_graceful_interrupt(turn_id)
                    finally:
                        marked.set()

            session._native = GracefulNative()
            turn = await session.submit("SYNTHETIC_GRACEFUL_SOURCE")
            await entered.wait()
            stop = asyncio.create_task(turn.cancel(mode="graceful"))
            try:
                await asyncio.wait_for(marked.wait(), 3)
            finally:
                release.set()
            try:
                await stop
            except Exception:
                pass
            with pytest.raises(Exception) as original:
                await turn.get_result()
            await session.flush_memory()
            rollout = json.loads((workspace / "sessions" / f"{session.id}.jsonl").read_text())
            assert rollout["terminal_metadata"]["terminal_state"] == "cancelled", (
                type(original.value),
                type(original.value.__cause__),
                rollout["terminal_metadata"],
            )


async def test_graceful_stop_after_native_completion_preserves_completed_memory(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    entered, release = asyncio.Event(), asyncio.Event()
    extraction = {
        "rollout_slug": "natural",
        "rollout_summary": "Completed.",
        "raw_memory": "Completed.",
    }
    consolidation = {"summary": "Completed.", "memory": "Completed."}
    async with (
        timeout(30),
        _workspace_gateway(workspace, monkeypatch) as connect,
        ScriptedResponses(
            [text("Naturally completed source.")],
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
            native = session._native

            class HeldAdmission:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _record_memory_input_admission(self, turn_id):
                    entered.set()
                    await release.wait()
                    return await native._record_memory_input_admission(turn_id)

            session._native = HeldAdmission()
            turn = await session.submit("SYNTHETIC_NATURAL_COMPLETION")
            await entered.wait()
            stop = asyncio.create_task(turn.cancel(mode="graceful"))
            try:
                while not session._root_interrupts:
                    await asyncio.sleep(0)
                assert not stop.done()
            finally:
                release.set()
            await stop
            assert (await turn.get_result()).final_output == "Naturally completed source."
            await session.flush_memory()
            rollout = json.loads((workspace / "sessions" / f"{session.id}.jsonl").read_text())
            assert rollout["terminal_metadata"]["terminal_state"] == "completed"
            assert rollout["terminal_metadata"]["has_final_output"] is True
            assert len(peer.requests) == 3
