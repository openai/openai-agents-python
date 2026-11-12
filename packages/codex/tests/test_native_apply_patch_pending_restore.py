"""Pending typed editor approval through a fresh-process manual owner."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agents import (
    Agent,
    ApplyPatchResult,
    InteractionExpiredError,
    NativeProvider,
    SessionState,
    apply_patch_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_apply_patch import patch_call
from tests.test_native_manual_tools import next_request

OPERATION = {
    "type": "update_file",
    "path": "tenant://source/../exact",
    "diff": "application-owned exact diff",
    "move_to": "tenant://destination",
}


def selected_model():
    catalog = json.loads(
        (Path(__file__).parent / "fixtures/request-control-models.json").read_text()
    )
    return next(row["slug"] for row in catalog["models"] if row.get("apply_patch_tool_type"))


async def checkpoint_stage(stage, endpoint, directory, decision):
    root = Path(directory)

    class Editor:
        def update_file(self, operation):
            assert stage == "restore", "A retired or replay editor entered"
            assert operation.path == OPERATION["path"]
            assert operation.diff == OPERATION["diff"]
            assert operation.move_to == OPERATION["move_to"]
            with (root / "effects.jsonl").open("a") as output:
                output.write(
                    json.dumps({"editor": "fresh", "call_id": operation.ctx_wrapper.call_id}) + "\n"
                )
            return ApplyPatchResult("completed", "fresh editor output")

        create_file = delete_file = update_file

    declaration = apply_patch_tool(Editor(), needs_approval=True)
    async with timeout(20), NativeProvider(base_url=endpoint, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use the current application editor.",
            model=selected_model(),
            provider=provider,
            tools=[declaration],
        )
        if stage == "capture":
            async with agent.session() as session:
                turn = await session.submit("edit")
                request = await next_request(session.interactions(), declaration)
                assert request.call_id == "native-patch"
                assert request.turn_id == turn.turn_id
                assert request.arguments.operation.model_dump(exclude_none=True) == OPERATION
                assert not (root / "effects.jsonl").exists()
                saved = await session.suspend()
                (root / "pending.json").write_text(saved.to_json())
                with pytest.raises(InteractionExpiredError):
                    await request.approve()
        elif stage == "refuse":
            raw = json.loads((root / "pending.json").read_text())
            pending = raw["native_continuation"]["pending_manual"][0]
            if decision == "arguments":
                arguments = json.loads(pending["arguments"])
                arguments["operation"]["diff"] = "changed saved action"
                pending["arguments"] = json.dumps(arguments)
            elif decision in {"effective-operation", "effective-type", "effective-id"}:
                effective = json.loads(pending["effective_arguments"])
                if decision == "effective-operation":
                    effective["operation"]["path"] = "tenant://changed"
                elif decision == "effective-type":
                    effective["type"] = "function_call"
                else:
                    effective["call_id"] = "other-call"
                pending["effective_arguments"] = json.dumps(effective)
            elif decision == "ownership":
                for row in raw["history"]:
                    if row.get("payload", {}).get("type") == "apply_patch_call":
                        row["metadata"].pop("sdk_application_tool")
            elif decision == "catalog":
                agent = Agent(
                    instructions=agent.instructions, model=selected_model(), provider=provider
                )
            else:
                raise AssertionError("Unknown refusal case")
            saved = SessionState.from_json(json.dumps(raw))
            with pytest.raises((ValueError, RuntimeError)):
                async with agent.session(state=saved):
                    pytest.fail("An altered action or missing editor entered")
            assert not (root / "effects.jsonl").exists()
        elif stage == "restore":
            saved = SessionState.from_json((root / "pending.json").read_text())
            async with agent.session(state=saved) as restored:
                request = await next_request(restored.interactions(), declaration)
                assert request.call_id == "native-patch"
                assert (
                    request.turn_id == json.loads(saved.to_json())["native_continuation"]["turn_id"]
                )
                assert request.arguments.operation.model_dump(exclude_none=True) == OPERATION
                assert not (root / "effects.jsonl").exists()
                if decision == "approve":
                    await request.approve()
                else:
                    await request.reject("fresh application decision")
                assert (await restored.restored_turn.get_result()).final_output == "done"
                with pytest.raises(InteractionExpiredError):
                    await request.approve()
                (root / "completed.json").write_text((await restored.save_state()).to_json())
        else:
            saved = SessionState.from_json((root / "completed.json").read_text())
            async with agent.session(state=saved) as resumed:
                assert (
                    await (await resumed.submit("continue")).get_result()
                ).final_output == "continued"


async def run_stage(stage, endpoint, directory, decision):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import asyncio,sys; "
        "from tests.test_native_apply_patch_pending_restore import checkpoint_stage; "
        "asyncio.run(checkpoint_stage(*sys.argv[1:]))",
        stage,
        endpoint,
        str(directory),
        decision,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stdout.decode() + stderr.decode()


@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_pending_patch_cold_restore_rebinds_editor_and_requires_fresh_decision(
    responses, tmp_path, decision
):
    original = patch_call(OPERATION)
    responses.respond(original)
    responses.respond(assistant_output("done"))
    responses.respond(assistant_output("continued"))
    await run_stage("capture", responses.url, tmp_path, decision)
    assert len(responses.requests) == 1
    await run_stage("restore", responses.url, tmp_path, decision)
    assert len(responses.requests) == 2
    await run_stage("replay", responses.url, tmp_path, decision)
    assert len(responses.requests) == 3
    effects = (
        (tmp_path / "effects.jsonl").read_text().splitlines()
        if (tmp_path / "effects.jsonl").exists()
        else []
    )
    assert len(effects) == int(decision == "approve")
    if effects:
        assert json.loads(effects[0]) == {"editor": "fresh", "call_id": "native-patch"}
    for request in responses.requests[1:]:
        assert (
            next(item for item in request["input"] if item.get("type") == "apply_patch_call")
            == original["item"]
        )
        outputs = [
            item for item in request["input"] if item.get("type") == "apply_patch_call_output"
        ]
        assert len(outputs) == 1
        assert outputs[0]["call_id"] == "native-patch"
        assert outputs[0]["status"] == ("completed" if decision == "approve" else "failed")
        if decision == "approve":
            assert outputs[0]["output"] == "fresh editor output"


@pytest.mark.parametrize(
    "mutation",
    ["arguments", "effective-operation", "effective-type", "effective-id", "ownership", "catalog"],
)
async def test_pending_patch_restore_refuses_changed_action_or_current_authority(
    responses, tmp_path, mutation
):
    responses.respond(patch_call(OPERATION))
    await run_stage("capture", responses.url, tmp_path, "approve")
    await run_stage("refuse", responses.url, tmp_path, mutation)
    assert len(responses.requests) == 1
    assert not (tmp_path / "effects.jsonl").exists()


@pytest.mark.parametrize("mutation", [None, "cross-kind", "ownership", "turn"])
async def test_mixed_native_approvals_keep_typed_partial_receipt_across_another_restore(
    responses,
    tmp_path,
    mutation,
):
    from dataclasses import replace

    from agents import ShellCommandOutput, ShellExit, ShellOutput, shell_tool
    from tests.test_native_application_shell import shell_call
    from tests.test_native_apply_patch import Editor

    source_editor = Editor()
    current_editor = Editor()
    shell_effects = []

    def source_shell(_context, _call):
        raise AssertionError("The original Shell owner entered")

    def current_shell(context, call):
        shell_effects.append((context.context, call.action.commands))
        return ShellOutput(
            output=[ShellCommandOutput(stdout="ok", stderr="", outcome=ShellExit(exit_code=0))]
        )

    original_shell = shell_tool(source_shell, needs_approval=True)
    restored_shell = shell_tool(current_shell, needs_approval=True)
    original_patch = apply_patch_tool(source_editor, needs_approval=True)
    restored_patch = apply_patch_tool(current_editor, needs_approval=True)
    responses.respond(shell_call(), patch_call(OPERATION))
    responses.respond(assistant_output("mixed done"))
    async with timeout(30), NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        source_agent = Agent(
            instructions="Use the declared Shell and editor.",
            model=selected_model(),
            provider=provider,
            tools=[original_shell, original_patch],
        )
        async with source_agent.session() as source:
            await source.submit("execute both")
            shell_stream, patch_stream = source.interactions(), source.interactions()
            await next_request(shell_stream, original_shell)
            await next_request(patch_stream, original_patch)
            saved = SessionState.from_json((await source.suspend()).to_json())
        current = replace(source_agent, tools=[restored_shell, restored_patch])
        async with current.session(state=saved, context="first") as restored:
            shell_stream, patch_stream = restored.interactions(), restored.interactions()
            old_shell = await next_request(shell_stream, restored_shell)
            patch = await next_request(patch_stream, restored_patch)
            await patch.approve()
            while not current_editor.operations:
                await asyncio.sleep(0.01)
            partial = SessionState.from_json((await restored.suspend()).to_json())
            (tmp_path / "partial.json").write_text(partial.to_json())
            with pytest.raises(InteractionExpiredError):
                await old_shell.approve()
        assert len(current_editor.operations) == 1
        assert source_editor.operations == []
        assert shell_effects == []
        assert len(responses.requests) == 1
        if mutation is not None:
            raw = json.loads(partial.to_json())
            receipt = raw["native_continuation"]["accepted_replies"]["native-patch"]["payload"]
            if mutation == "cross-kind":
                receipt["type"] = "function_call_output"
                receipt.pop("status")
            elif mutation == "ownership":
                for row in raw["history"]:
                    if row.get("payload", {}).get("type") == "apply_patch_call":
                        row["metadata"].pop("sdk_application_tool")
            else:
                receipt["internal_chat_message_metadata_passthrough"]["turn_id"] = "other-turn"
            with pytest.raises((ValueError, RuntimeError)):
                async with current.session(state=SessionState.from_json(json.dumps(raw))):
                    pytest.fail("An unmatched partial receipt entered")
            assert len(current_editor.operations) == 1
            assert shell_effects == []
            assert len(responses.requests) == 1
            return
        async with current.session(state=partial, context="second") as restored:
            request = await next_request(restored.interactions(), restored_shell)
            assert request.call_id == "shell-local"
            await request.approve()
            assert (await restored.restored_turn.get_result()).final_output == "mixed done"
    assert len(current_editor.operations) == 1
    assert shell_effects == [("second", ["printf sample"])]
    replay = responses.requests[1]["input"]
    assert len([row for row in replay if row.get("type") == "apply_patch_call_output"]) == 1
    assert len([row for row in replay if row.get("type") == "shell_call_output"]) == 1
    assert not any(row.get("type") == "function_call_output" for row in replay)
