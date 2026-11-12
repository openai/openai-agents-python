"""Native typed editor dispatch, accepted history and cancellation ownership."""

import asyncio
import json

import pytest

from agents import (
    Agent,
    ApplyPatchOperation,
    ApplyPatchResult,
    NativeError,
    NativeProvider,
    SessionState,
    ToolRef,
    apply_patch_tool,
)
from tests.native_fixtures import assistant_output, responses as responses


def patch_call(operation):
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "apply_patch_call",
            "id": "apc_native",
            "call_id": "native-patch",
            "status": "completed",
            "operation": operation,
            "future_provider_field": "preserved",
        },
    }


class Editor:
    def __init__(self, status="completed"):
        self.status = status
        self.operations = []

    def create_file(self, operation: ApplyPatchOperation):
        self.operations.append(operation)
        return ApplyPatchResult(self.status, "exact editor output")

    update_file = create_file
    delete_file = create_file


@pytest.mark.parametrize("kind", ["create_file", "update_file", "delete_file"])
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_native_editor_preserves_typed_operation_result_and_current_selection(
    responses, kind, status
):
    editor = Editor(status)
    operation = {"type": kind, "path": "tenant://one/../application-path"}
    if kind != "delete_file":
        operation["diff"] = "application-specific diff"
    if kind == "update_file":
        operation["move_to"] = "remote://destination"
    responses.respond(patch_call(operation))
    responses.respond(assistant_output("done"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use the editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor)],
            model_settings={"tool_choice": ToolRef("apply_patch")},
        )
        async with agent.session() as session:
            assert (await (await session.submit("edit")).get_result()).final_output == "done"
    assert len(editor.operations) == 1
    assert editor.operations[0].path == operation["path"]
    assert editor.operations[0].ctx_wrapper.call_id == "native-patch"
    assert [
        tool for tool in responses.requests[0]["tools"] if tool.get("type") == "apply_patch"
    ] == [{"type": "apply_patch"}]
    assert not any(
        tool.get("name") == "apply_patch" and tool.get("type") in ("function", "custom")
        for tool in responses.requests[0]["tools"]
    )
    assert responses.requests[0]["tool_choice"] == "required"
    original = next(
        item for item in responses.requests[1]["input"] if item.get("type") == "apply_patch_call"
    )
    assert original["operation"] == operation
    assert original["future_provider_field"] == "preserved"
    result = next(
        item
        for item in responses.requests[1]["input"]
        if item.get("type") == "apply_patch_call_output"
    )
    assert result["call_id"] == "native-patch"
    assert result["status"] == status
    assert result["output"] == "exact editor output"
    assert len(responses.requests) == 2


async def test_native_editor_checkpoint_retains_accepted_result_without_replay(responses):
    editor = Editor()
    responses.respond(patch_call({"type": "delete_file", "path": "application-owned"}))
    responses.respond(assistant_output("done"))
    responses.respond(assistant_output("continued"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor)],
        )
        async with agent.session() as session:
            assert (await (await session.submit("edit")).get_result()).final_output == "done"
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with agent.session(state=saved) as restored:
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "continued"
    assert len(editor.operations) == 1
    retained = responses.requests[2]["input"]
    assert sum(item.get("type") == "apply_patch_call_output" for item in retained) == 1
    assert len(responses.requests) == 3


async def test_function_payload_cannot_enter_typed_editor(responses):
    editor = Editor()
    raw = {
        "type": "apply_patch_call",
        "call_id": "native-patch",
        "operation": {"type": "delete_file", "path": "must-not-run"},
    }
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_spoof",
                "call_id": "native-patch",
                "name": "apply_patch",
                "arguments": json.dumps(raw),
            },
        }
    )
    responses.respond(assistant_output("refused"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor)],
        )
        try:
            await agent.run("edit")
        except NativeError:
            pass
    assert editor.operations == []
    assert any(tool.get("type") == "apply_patch" for tool in responses.requests[0]["tools"])


async def test_editor_cancellation_joins_callback_before_provider_retirement(responses):
    entered, retired = asyncio.Event(), asyncio.Event()
    effects = []
    editor = Editor()

    async def blocked(operation):
        entered.set()
        try:
            await asyncio.Event().wait()
            effects.append("late")
        finally:
            retired.set()

    editor.create_file = blocked
    responses.respond(patch_call({"type": "create_file", "path": "application-owned", "diff": "d"}))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor)],
        )
        task = asyncio.create_task(agent.run("edit"))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert retired.is_set()
    assert effects == []


async def _loss_worker():
    import os
    import sys
    from pathlib import Path

    from agents import LocalStorage

    url, root_arg = sys.argv[1:]
    root = Path(root_arg)

    class CrashEditor(Editor):
        def create_file(self, operation):
            with (root / "effects").open("a") as stream:
                stream.write("one editor effect\n")
                stream.flush()
                os.fsync(stream.fileno())
            os._exit(67)

    provider = NativeProvider(base_url=url, api_key="synthetic")
    agent = Agent(
        instructions="Use editor.",
        model="gpt-5",
        provider=provider,
        tools=[apply_patch_tool(CrashEditor())],
    )
    async with agent.session(storage=LocalStorage(root / "profile")) as session:
        print(json.dumps({"opened": session.id}), flush=True)
        await (await session.submit("edit")).get_result()


async def test_local_interrupted_edit_keeps_durable_owner_and_refuses_silent_reopen(
    responses, tmp_path
):
    import os
    import sys
    from pathlib import Path

    from agents import LocalRecoveryRequired, LocalStorage
    from tests.test_native_local_storage import _collect

    responses.respond(patch_call({"type": "create_file", "path": "application-owned", "diff": "d"}))
    test_root = str(Path(__file__).resolve().parents[1])
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "tests.test_native_apply_patch",
        responses.url,
        str(tmp_path),
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(filter(None, [test_root, os.environ.get("PYTHONPATH")])),
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    code, rows, error = await _collect(process)
    assert code == 67 and rows, (code, rows, error)
    thread_id = rows[0]["opened"]
    editor = Editor()
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor)],
        )
        with pytest.raises(LocalRecoveryRequired) as missing:
            async with agent.session(storage=LocalStorage(tmp_path / "profile"), resume=thread_id):
                pytest.fail("Unknown editor effects require explicit reconciliation")
    (call,) = missing.value.calls
    assert call.name == "apply_patch"
    assert call.arguments["type"] == "apply_patch_call"
    assert editor.operations == []
    assert (tmp_path / "effects").read_text().splitlines() == ["one editor effect"]
    assert len(responses.requests) == 1
    (rollout,) = (tmp_path / "profile").rglob(f"*{thread_id}.jsonl")
    records = [json.loads(line) for line in rollout.read_text().splitlines()]
    owned = [
        record
        for record in records
        if record.get("type") == "response_item"
        and record.get("payload", {}).get("type") == "apply_patch_call"
    ]
    assert len(owned) == 1
    assert owned[0]["metadata"]["sdk_application_tool"] == "apply_patch"


if __name__ == "__main__":
    asyncio.run(_loss_worker())
