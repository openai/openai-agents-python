"""Portable CodeMode values come from native state without replaying cells."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agents import Agent, NativeProvider, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_code_mode import code_call, code_output


def agent_for(url: str, key: str, tools=()) -> Agent:
    return Agent(
        instructions="Use only the requested synthetic stored values.",
        tools=list(tools),
        provider=NativeProvider(base_url=url, api_key=key),
        programmatic_tool_calling=True,
        model="gpt-5.5",
    )


async def run_worker(request: dict, *, module: str = "tests.test_native_vm_state") -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
        "runpy.run_module(sys.argv[2],run_name='__main__')",
        str(Path(__file__).resolve().parents[1]),
        module,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        stdout, stderr = await process.communicate(json.dumps(request).encode())
    except BaseException:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, (stdout.decode(), stderr.decode())


async def worker() -> None:
    request = json.load(sys.stdin)
    receipt = Path(request["receipt"])

    @function_tool
    def effect() -> str:
        """Record one synthetic external effect."""
        with receipt.open("a") as output:
            output.write("effect\n")
        return "captured-once"

    agent = agent_for(request["url"], request["key"], [effect])
    if request["stage"] == "export":
        async with agent.session() as session:
            assert (
                await (await session.submit("Store the receipt")).get_result()
            ).final_output == "stored"
            Path(request["state"]).write_text(
                (await session.save_state()).to_json(), encoding="utf-8"
            )
    else:
        state = SessionState.from_json(Path(request["state"]).read_text(encoding="utf-8"))
        async with agent.session(state=state) as session:
            assert (
                await (await session.submit("Read without replay")).get_result()
            ).final_output == "read"
    await agent.provider.aclose()


async def test_vm_values_restore_after_source_process_exit_without_replay(responses, tmp_path):
    responses.respond(
        code_call(
            "store('receipt',await tools.effect({})); store('value',{n:7,a:[null,true,'雪']});",
            "save-vm",
        )
    )
    responses.respond(assistant_output("stored"))
    state = tmp_path / "state.json"
    receipt = tmp_path / "effects.txt"
    request = {"url": responses.url, "state": str(state), "receipt": str(receipt)}
    async with timeout(60):
        await run_worker({**request, "stage": "export", "key": "synthetic-source-key"})
        assert receipt.read_text() == "effect\n"
        saved = SessionState.from_json(state.read_text(encoding="utf-8"))
        assert saved.format_version == 6
        assert saved.root_code_mode is not None
        assert saved.root_code_mode["stored_values"]["receipt"] == "captured-once"
        responses.respond(
            code_call(
                "text(JSON.stringify([load('receipt'),load('value').n,load('value').a]));",
                "read-vm",
            )
        )
        responses.respond(assistant_output("read"))
        await run_worker({**request, "stage": "import", "key": "synthetic-destination-key"})
    assert receipt.read_text() == "effect\n"
    assert '["captured-once",7,[null,true,"雪"]]' in code_output(responses.requests[-1], "read-vm")
    assert responses.request_headers[-1]["authorization"] == "Bearer synthetic-destination-key"
    assert all(
        "source-key" not in headers.get("authorization", "")
        for headers in responses.request_headers[2:]
    )


async def test_vm_forks_copy_values_independently(responses):
    agent = agent_for(responses.url, "synthetic-vm-forks")
    responses.respond(code_call("store('value','original');", "write-original"))
    responses.respond(assistant_output("stored"))
    async with timeout(60), agent.session() as source:
        await (await source.submit("Store")).get_result()
        first = await source.fork()
        second = await source.fork()
        async with agent.session(state=first) as left, agent.session(state=second) as right:
            for session, value in [(left, "left"), (right, "right"), (source, "source")]:
                responses.respond(
                    code_call(f"store('value',{json.dumps(value)});", "update-" + value)
                )
                responses.respond(assistant_output("updated"))
                await (await session.submit("Update")).get_result()
            for session, value in [(left, "left"), (right, "right"), (source, "source")]:
                call_id = "read-" + value
                responses.respond(code_call("text(load('value'));", call_id))
                responses.respond(assistant_output("read"))
                await (await session.submit("Read")).get_result()
                assert value in code_output(responses.requests[-1], call_id)
    await agent.provider.aclose()


@pytest.mark.parametrize("mutate", ["version", "counter", "terminal"])
async def test_vm_malformed_snapshot_rejects_before_provider_request(responses, mutate):
    agent = agent_for(responses.url, "synthetic-vm-invalid")
    responses.respond(code_call("store('value',7);", "write-invalid"))
    responses.respond(assistant_output("stored"))
    async with timeout(35), agent.session() as source:
        await (await source.submit("Store")).get_result()
        payload = json.loads((await source.save_state()).to_json())
    snapshot = payload["root_code_mode"]
    if mutate == "version":
        snapshot["format_version"] = 2
    elif mutate == "counter":
        snapshot["next_cell_id"] = 0
    else:
        snapshot["completed_cells"] = {
            "1": {
                "event": {"Pending": {"content_items": [], "pending_tool_call_ids": []}},
                "pending_initial_yield_items": None,
            }
        }
    before = len(responses.requests)
    with pytest.raises((RuntimeError, ValueError), match="snapshot|saved|terminal"):
        async with agent.session(state=SessionState.from_json(json.dumps(payload))):
            pytest.fail("Invalid native snapshot opened a Session")
    assert len(responses.requests) == before
    await agent.provider.aclose()


async def test_vm_busy_export_preserves_source_cell(responses):
    agent = agent_for(responses.url, "synthetic-vm-busy")
    responses.respond(
        code_call("store('before',1); yield_control(); await new Promise(()=>{});", "live-vm")
    )
    responses.respond(assistant_output("yielded"))
    async with timeout(40), agent.session() as source:
        await (await source.submit("Yield a cell")).get_result()
        output = code_output(responses.requests[-1], "live-vm")
        cell = output.removeprefix("Script running with cell ID ").split()[0].rstrip(".")
        before = len(responses.requests)
        with pytest.raises(RuntimeError, match="active cells"):
            await source.save_state()
        assert len(responses.requests) == before
        responses.respond(function_call("wait", {"cell_id": cell, "yield_time_ms": 1}, "same-cell"))
        responses.respond(assistant_output("still live"))
        await (await source.submit("Read original cell")).get_result()
        from tests.native_fixtures import tool_output

        assert tool_output(responses.requests[-1], "same-cell").startswith(
            "Script running with cell ID "
        )
        responses.respond(
            function_call("wait", {"cell_id": cell, "terminate": True}, "terminate-cell")
        )
        responses.respond(assistant_output("terminated"))
        await (await source.submit("Terminate")).get_result()
        state = await source.save_state()
        assert state.root_code_mode is not None
        assert "before" not in state.root_code_mode["stored_values"]
    await agent.provider.aclose()


async def test_vm_prior_values_survive_direct_manual_suspend(responses):
    from agents import FunctionTool
    from tests.test_native_manual_profiles import save_at_manual_boundary
    from tests.test_native_manual_tools import LookupArgs, next_request

    lookup = FunctionTool(
        name="lookup", description="Read a synthetic value", input_type=LookupArgs, output_type=str
    )
    agent = agent_for(responses.url, "synthetic-vm-manual", [lookup])
    responses.respond(code_call("store('prior','kept');", "prior-cell"))
    responses.respond(assistant_output("stored"))
    async with timeout(45), agent.session() as source:
        await (await source.submit("Store")).get_result()
        responses.respond(function_call("lookup", {"key": "manual"}, "manual-after-code"))
        await source.submit("Pause on a direct reply")
        old = await next_request(source.interactions(), lookup)
        saved = await save_at_manual_boundary(source)
        assert saved.format_version == 7
    async with timeout(45), agent.session(state=saved) as target:
        turn = target.restored_turn
        assert turn is not None
        current = await next_request(target.interactions(), lookup)
        with pytest.raises((ValueError, RuntimeError), match="foreign|expired|closed"):
            await old.respond("stale")
        responses.respond(code_call("text(load('prior'));", "read-prior"))
        responses.respond(assistant_output("continued"))
        await current.respond("current")
        assert (await turn.get_result()).final_output == "continued"
        assert "kept" in code_output(responses.requests[-1], "read-prior")
    await agent.provider.aclose()


async def test_vm_descendant_values_restore_lazily_with_current_authority(tmp_path, monkeypatch):
    from tests.native_vm_fixtures import followup, vm_family
    from tests.test_native_child_agents import handles
    from tests.test_native_executor_tools import declaration

    async with timeout(70), vm_family(tmp_path, monkeypatch) as family:
        peer, executor, agent, saved, effects = family
        [child] = saved.descendants
        # Capture records the proven unused root namespace alongside the child.
        assert saved.root_code_mode == {
            "format_version": 1,
            "next_cell_id": 1,
            "stored_values": {},
            "completed_cells": {},
        }
        assert child.code_mode is not None
        assert child.code_mode["stored_values"]["kept"] == "source:seed"
        before = len(peer.requests)
        async with agent.session(
            state=saved, context="destination", environment=declaration(executor)
        ) as destination:
            rows = [row async for row in destination.subagents()]
            assert len(rows) == 1 and not rows[0].is_live and rows[0].turn is None
            assert len(peer.requests) == before
            followup(peer, call="read-child", result="read", value="fresh")
            assert (
                await (await destination.submit("Resume alpha")).get_result()
            ).final_output == "root read"
            [turn] = await handles(destination, 1)
            assert turn.child_id == child.thread_id
            assert (await turn.get_result()).final_output == "child read"
            assert '["source:seed","destination:fresh"]' in code_output(
                peer.by_thread["/root/alpha"][-1], "read-child"
            )
            copied = await destination.save_state()
            [retained] = copied.descendants
            assert retained.code_mode is not None
            assert retained.code_mode["stored_values"]["kept"] == "source:seed"
        assert effects == [("source", "seed"), ("destination", "fresh")]
        assert all(
            headers["authorization"] == "Bearer destination-vm-key"
            for headers in peer.request_headers[before:]
        )


@pytest.mark.parametrize("malformed", ["text", "image", "active", "extra"])
async def test_vm_nested_descendant_snapshot_rejects_before_root_publication(
    tmp_path, monkeypatch, malformed
):
    from tests.native_vm_fixtures import vm_family
    from tests.test_native_executor_tools import declaration

    async with timeout(60), vm_family(tmp_path, monkeypatch) as family:
        peer, executor, agent, saved, effects = family
        payload = json.loads(saved.to_json())
        snapshot = payload["descendants"][0]["code_mode"]
        terminal = {
            "pending_initial_yield_items": None,
            "event": {"Completed": {"content_items": [], "error_text": None}},
        }
        if malformed == "text":
            terminal["event"]["Completed"]["content_items"] = [{"Text": {"text": 7}}]
        elif malformed == "image":
            terminal["event"]["Completed"]["content_items"] = [
                {"Image": {"image_url": "synthetic", "detail": "Invalid"}}
            ]
        elif malformed == "active":
            terminal["event"] = {"Pending": {"content_items": [], "pending_tool_call_ids": []}}
        else:
            terminal["unexpected"] = "not a native field"
        snapshot["completed_cells"] = {"1": terminal}
        changed = SessionState.from_json(json.dumps(payload))
        before = len(peer.requests)
        with pytest.raises((RuntimeError, ValueError)):
            async with agent.session(
                state=changed, context="destination", environment=declaration(executor)
            ):
                pytest.fail("Malformed descendant state published root authority")
        assert len(peer.requests) == before
        assert effects == [("source", "seed")]


@pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "pidfd_open"),
    reason="The transparent helper gate uses Linux executable pipes and process-exit descriptors",
)
@pytest.mark.parametrize("mode", ["cancel", "cancel-before-ack", "fail"])
async def test_vm_lazy_child_import_joins_cleanup_and_retry_uses_a_fresh_receiver(
    tmp_path, monkeypatch, mode
):
    from agents import NativeError, TurnStoppedError
    from tests.native_vm_fixtures import followup, state_transfer_gate, vm_family
    from tests.test_native_child_agents import handles
    from tests.test_native_executor_tools import declaration

    async with timeout(100), vm_family(tmp_path, monkeypatch) as family:
        peer, executor, agent, saved, effects = family
        [child_state] = saved.descendants
        assert child_state.code_mode is not None
        assert saved.root_code_mode is not None
        assert saved.root_code_mode["stored_values"] == {}
        assert child_state.code_mode["stored_values"]["kept"] == "source:seed"
        async with state_transfer_gate(
            tmp_path, monkeypatch, mode, import_snapshot=child_state.code_mode
        ) as gate:
            async with agent.session(
                state=saved, context="destination", environment=declaration(executor)
            ) as destination:
                assert not any(
                    event["event"] == "import_applied" and event.get("target") is True
                    for event in gate.history
                ), "Opening the root eagerly imported the saved child VM"
                try:
                    followup(peer, call="first-import", result="rejected")
                    root = await destination.submit("Resume alpha")
                    applied = await gate.wait("import_applied", target=True)
                    assert (await root.get_result()).final_output == "root rejected"
                    [old_turn] = await handles(destination, 1)
                    if mode.startswith("cancel"):
                        completion = asyncio.create_task(destination.cancel_all(mode="force"))
                    else:
                        completion = asyncio.create_task(old_turn.get_result())
                    shutdown = await gate.wait("shutdown_applied", target=True)
                    assert shutdown["session"] == applied["session"]
                    assert not completion.done(), (
                        "The public operation returned before native import cleanup joined"
                    )
                    assert effects == [("source", "seed")]
                    if mode == "cancel-before-ack":
                        await gate.release("import")
                        forwarded = await gate.wait("import_forwarded", target=True)
                        assert forwarded["session"] == applied["session"]
                        assert not completion.done(), (
                            "Cancellation returned before the shared cleanup acknowledgement"
                        )
                    await gate.release_shutdown()
                    result = await completion
                    if mode.startswith("cancel"):
                        with pytest.raises((NativeError, TurnStoppedError, asyncio.CancelledError)):
                            await old_turn.get_result()
                        # The aborted attempt never asked for its queued final sample.
                        peer.programs["/root/alpha"].clear()
                    else:
                        assert result.final_output == "child rejected"
                        assert "synthetic-code-import-failure" in code_output(
                            peer.by_thread["/root/alpha"][-1], "first-import"
                        )
                    await gate.wait("shutdown_forwarded", target=True)
                    followup(peer, call="retry-import", result="recovered", value="fresh")
                    assert (
                        await (await destination.submit("Retry alpha")).get_result()
                    ).final_output == "root recovered"
                    while True:
                        [turn] = await handles(destination, 1)
                        if turn.turn_id != old_turn.turn_id:
                            break
                        await asyncio.sleep(0)
                    assert (await turn.get_result()).final_output == "child recovered"
                    assert '["source:seed","destination:fresh"]' in code_output(
                        peer.by_thread["/root/alpha"][-1], "retry-import"
                    )
                    assert effects == [("source", "seed"), ("destination", "fresh")]
                    imports = [
                        event
                        for event in gate.history
                        if event["event"] == "import_applied" and event.get("target") is True
                    ]
                    assert len(imports) == 2 and imports[0]["session"] != imports[1]["session"]
                finally:
                    await gate.release_shutdown()
            await agent.provider.aclose()
            await gate.wait_for_helper_exit()


@pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "pidfd_open"),
    reason="The transparent helper gate uses Linux executable pipes and process-exit descriptors",
)
@pytest.mark.parametrize("successor", ["active", "completed"])
async def test_canceling_vm_export_keeps_same_source_and_newer_values(
    responses, tmp_path, monkeypatch, successor
):
    from tests.native_vm_fixtures import state_transfer_gate

    entered = asyncio.Event()
    finish = asyncio.Event()

    @function_tool
    async def next_value() -> str:
        """Return the current synthetic value after the caller releases it."""
        entered.set()
        await finish.wait()
        return "newer-value"

    agent = agent_for(responses.url, "synthetic-export-cancel", [next_value])
    responses.respond(code_call("store('value','original-value');", "seed-export-source"))
    responses.respond(assistant_output("source ready"))
    async with timeout(70), state_transfer_gate(tmp_path, monkeypatch, "export") as gate:
        async with agent.session() as source:
            assert (
                await (await source.submit("Store original")).get_result()
            ).final_output == "source ready"
            saving = asyncio.create_task(source.save_state())
            resuming = None
            try:
                original = await gate.wait("export_observed")
                saving.cancel()
                done, _ = await asyncio.wait({saving}, timeout=3)
                assert saving in done, "Canceled save waited for the held export acknowledgement"
                with pytest.raises(asyncio.CancelledError):
                    await saving
                assert not any(event["event"] == "export_forwarded" for event in gate.history)
                responses.respond(
                    code_call("store('value',await tools.next_value({}));", "newer-export-source")
                )
                responses.respond(assistant_output("source updated"))

                async def resume_source():
                    turn = await source.submit(
                        "Update the same source while the old snapshot is held"
                    )
                    await entered.wait()
                    return turn

                resuming = asyncio.create_task(resume_source())
                done, _ = await asyncio.wait({resuming}, timeout=3)
                assert resuming in done, "The source could not run before the late export reply"
                turn = resuming.result()
                assert not any(event["event"] == "export_forwarded" for event in gate.history)
                if successor == "completed":
                    finish.set()
                    assert (await turn.get_result()).final_output == "source updated"
                await gate.release_export()
                forwarded = await gate.wait("export_forwarded")
                assert forwarded["session"] == original["session"]
                if successor == "active":
                    assert source.active_turn is not None
                    finish.set()
                    assert (await turn.get_result()).final_output == "source updated"
                responses.respond(code_call("text(load('value'));", "read-export-source"))
                responses.respond(assistant_output("source survived"))
                assert (
                    await (await source.submit("Read the same source")).get_result()
                ).final_output == "source survived"
                assert "newer-value" in code_output(responses.requests[-1], "read-export-source")
                copied = await source.save_state()
                assert copied.root_code_mode is not None
                assert copied.root_code_mode["stored_values"]["value"] == "newer-value"
                current = await gate.wait("export_observed")
                assert current["session"] == original["session"], (
                    "Export cancellation replaced the source VM"
                )
                assert not any(event["event"] == "shutdown_applied" for event in gate.history)
            finally:
                finish.set()
                await gate.release_export()
                if resuming is not None:
                    if not resuming.done():
                        resuming.cancel()
                    await asyncio.gather(resuming, return_exceptions=True)
                if not saving.done():
                    saving.cancel()
                    await asyncio.gather(saving, return_exceptions=True)
        await agent.provider.aclose()
        await gate.wait_for_helper_exit()


if __name__ == "__main__":
    asyncio.run(worker())
