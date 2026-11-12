"""Native typed editor dispatch, accepted history and cancellation ownership."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import (
    Agent,
    ApplyPatchOperation,
    ApplyPatchResult,
    ChatCompletionsModel,
    ModelRequest,
    NativeError,
    NativeProvider,
    SessionState,
    ToolRef,
    apply_patch_batch_tool,
    apply_patch_tool,
)
from agents.session import _call_callback
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


@pytest.mark.parametrize(
    ("status", "batch"), [("completed", False), ("failed", False), ("failed", True)]
)
async def test_chat_completions_replays_native_editor_operation_and_result(status, batch):
    editor = Editor("completed" if batch else status)
    operation = {
        "type": "update_file",
        "path": "tenant://source",
        "diff": "application-specific diff",
        "move_to": "tenant://destination",
    }
    name = "apply_patch"
    arguments = {"operation": operation}
    operations = [operation]
    expected = {"status": status, "output": "exact editor output"}
    tool = apply_patch_tool
    if batch:
        loop_thread = threading.get_ident()

        def sync_edit(op):
            assert threading.get_ident() != loop_thread
            return Editor.create_file(editor, op)

        async def failed_update(op):
            assert threading.get_ident() == loop_thread
            editor.operations.append(op)
            return ApplyPatchResult("failed", "editor refused update")

        editor.create_file = sync_edit
        editor.update_file = failed_update
        editor.delete_file = sync_edit
        operations = [
            {"type": "create_file", "path": "tenant://source", "diff": "initial"},
            operation,
            {"type": "delete_file", "path": "tenant://destination"},
        ]
        name = "apply_patch_batch"
        arguments = {"operations": operations}
        expected = {
            "results": [
                {"status": "completed", "output": "exact editor output"},
                {"status": "failed", "output": "editor refused update"},
                {"status": "completed", "output": "exact editor output"},
            ]
        }
        tool = apply_patch_batch_tool
    requests = []

    async def create(**body):
        requests.append(body)
        if len(requests) == 1:
            [declaration] = body["tools"]
            assert declaration["type"] == "function"
            assert declaration["function"]["name"] == name
            assert arguments.keys() <= declaration["function"]["parameters"]["properties"].keys()
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_editor",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            assistant = next(item for item in body["messages"] if item.get("tool_calls"))
            [call] = assistant["tool_calls"]
            assert call["id"] == "call_editor"
            assert json.loads(call["function"]["arguments"]) == arguments
            result = next(
                item for item in body["messages"] if item.get("tool_call_id") == "call_editor"
            )
            assert json.loads(result["content"]) == expected
            message = {"content": "done"}
            finish = "stop"
        return {
            "id": f"chat_editor_{len(requests)}",
            "choices": [{"finish_reason": finish, "message": message}],
        }

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    async with NativeProvider(
        base_url="https://example.invalid/v1",
        api_key="synthetic",
        model_backend=ChatCompletionsModel(client),
    ) as provider:
        agent = Agent(
            instructions="Use the editor.",
            model="gpt-5",
            provider=provider,
            tools=[tool(editor)],
            model_settings={"tool_choice": ToolRef(name)},
        )
        assert (await agent.run("edit")).final_output == "done"

    assert len(requests) == 2
    assert [
        (op.type, op.path, op.diff, op.move_to, op.ctx_wrapper.call_id) for op in editor.operations
    ] == [
        (op["type"], op["path"], op.get("diff"), op.get("move_to"), "call_editor")
        for op in operations
    ]


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


@pytest.mark.parametrize("batch", [False, True])
async def test_editor_cancellation_joins_callback_before_provider_retirement(responses, batch):
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
    operation = {"type": "create_file", "path": "application-owned", "diff": "d"}
    if batch:
        responses.respond(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "fc_editor_batch",
                    "call_id": "editor-batch",
                    "name": "apply_patch_batch",
                    "arguments": json.dumps(
                        {"operations": [operation, {"type": "delete_file", "path": "must-not-run"}]}
                    ),
                    "status": "completed",
                },
            }
        )
    else:
        responses.respond(patch_call(operation))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use editor.",
            model="gpt-5",
            provider=provider,
            tools=[(apply_patch_batch_tool if batch else apply_patch_tool)(editor)],
        )
        task = asyncio.create_task(agent.run("edit"))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert retired.is_set()
    assert effects == []
    assert editor.operations == []


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


@pytest.mark.parametrize(
    "arguments",
    [
        '{"operation":{"path":"synthetic-private-output","diff":',
        json.dumps({"operation": {"type": "invalid", "path": "synthetic-private-output"}}),
    ],
    ids=["malformed-json", "invalid-operation"],
)
@pytest.mark.parametrize("boundary", ["adapter", "native"])
async def test_chat_editor_failure_releases_validator_context_and_payload_frames(
    arguments, boundary
):
    async def create(**body):
        return {
            "id": "chat_invalid_editor",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_invalid_editor",
                                "type": "function",
                                "function": {"name": "apply_patch", "arguments": arguments},
                            }
                        ],
                    },
                }
            ],
        }

    def assert_released(error):
        pending, seen = [error], set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            assert type(current).__name__ != "ValidationError"
            trace = current.__traceback__
            while trace is not None:
                frame = trace.tb_frame
                if frame.f_code.co_filename == ChatCompletionsModel.complete.__code__.co_filename:
                    assert not {"call", "arguments", "response", "value"}.intersection(
                        frame.f_locals
                    )
                trace = trace.tb_next
            pending.extend(
                item for item in (current.__cause__, current.__context__) if item is not None
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    backend = ChatCompletionsModel(client)
    if boundary == "adapter":
        with pytest.raises(ValueError, match="requires a valid editor operation") as direct:
            await backend.complete(
                ModelRequest(
                    {
                        "model": "fixture",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "edit"}],
                            }
                        ],
                        "tools": [{"type": "apply_patch"}],
                    },
                    None,
                    "session",
                    "turn",
                )
            )
        assert_released(direct.value)
        return
    editor = Editor()
    async with NativeProvider(
        base_url="https://example.invalid/v1", api_key="synthetic", model_backend=backend
    ) as provider:
        agent = Agent(
            instructions="Use the editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor)],
        )
        with pytest.raises(ValueError, match="requires a valid editor operation") as native:
            await agent.run("edit")
    assert_released(native.value)
    assert editor.operations == []


@pytest.mark.parametrize("needs_approval", [False, True])
@pytest.mark.parametrize(
    "operation",
    [
        {"type": "synthetic-private-kind", "path": "synthetic-private-path"},
        {
            "type": "update_file",
            "path": {"private": "synthetic-private-path"},
            "diff": "synthetic-private-diff",
        },
    ],
)
async def test_batch_invalid_input_is_bounded_before_editor_or_approval(
    responses, needs_approval, operation
):
    editor = Editor()
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_invalid_batch",
                "call_id": "invalid-batch",
                "name": "apply_patch_batch",
                "arguments": json.dumps({"operations": [operation]}),
            },
        }
    )
    responses.respond(assistant_output("refused invalid batch"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_batch_tool(editor, needs_approval=needs_approval)],
        )
        await agent.run("edit", tool_error_formatter=lambda _: "bounded run error")
    assert editor.operations == []
    assert len(responses.requests) == 2
    replies = [
        item for item in responses.requests[1]["input"] if item["type"] == "function_call_output"
    ]
    assert len(replies) == 1
    assert "Invalid Apply Patch batch arguments." in json.dumps(replies[0])
    assert "synthetic-private" not in json.dumps(replies[0])


@pytest.mark.parametrize("fail_at", [0, 1])
@pytest.mark.parametrize("invalid_result", [False, True])
async def test_editor_batch_failure_releases_unexecuted_payloads(fail_at, invalid_result):
    failure = RuntimeError("synthetic editor failure")
    entered = []

    async def edit(operation):
        entered.append(operation.path)
        if len(entered) - 1 == fail_at:
            if invalid_result:
                return ApplyPatchResult("synthetic-private-status", "synthetic-private-output")
            raise failure
        return "synthetic-private-prior-output"

    editor = SimpleNamespace(create_file=edit, update_file=edit, delete_file=edit)
    tool = apply_patch_batch_tool(editor)
    operations = [
        {"type": "create_file", "path": f"synthetic-{i}", "diff": "synthetic-private-later-diff"}
        for i in range(3)
    ]

    async def create(**body):
        return {
            "id": "chat_batch_failure",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "batch_failure",
                                "type": "function",
                                "function": {
                                    "name": "apply_patch_batch",
                                    "arguments": json.dumps({"operations": operations}),
                                },
                            }
                        ],
                    },
                }
            ],
        }

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    async with NativeProvider(
        base_url="https://example.invalid/v1",
        api_key="synthetic",
        model_backend=ChatCompletionsModel(client),
    ) as provider:
        agent = Agent(
            instructions="Use editor.",
            model="gpt-5",
            provider=provider,
            tools=[tool],
        )
        with pytest.raises(TypeError if invalid_result else RuntimeError) as caught:
            await agent.run("edit", tool_error_formatter=lambda _: "must not recover editor body")
    if not invalid_result:
        assert caught.value is failure
    assert len(entered) == fail_at + 1
    trace = caught.value.__traceback__
    inspected = False
    while trace is not None:
        frame = trace.tb_frame
        if Path(frame.f_code.co_filename).parts[-2:] == ("_agents_codex", "apply_patch.py"):
            if frame.f_code.co_name == "apply":
                inspected = True
                assert not {
                    "context",
                    "operations",
                    "operation",
                    "results",
                    "value",
                    "text",
                    "status",
                    "output",
                    "task",
                    "admit",
                }.intersection(frame.f_locals)
            if frame.f_code.co_name == "_editor_result":
                assert frame.f_locals.get("value") is None
                assert frame.f_locals.get("status") is None
        trace = trace.tb_next
    assert inspected
    entry = tool._capture(object())
    parsed = entry._parse_arguments({"operations": [operations[0]]})
    reused = await _call_callback(
        entry.callback, lambda: True, object(), **entry._arguments(parsed)
    )
    assert reused.success
    assert len(entered) == fail_at + 2


if __name__ == "__main__":
    asyncio.run(_loss_worker())
