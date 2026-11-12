"""Completed local receipts and explicitly configured editor approval."""

import asyncio

import pytest

from agents import Agent, InteractionExpiredError, LocalStorage, NativeProvider, apply_patch_tool
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_apply_patch import Editor, patch_call
from tests.test_native_manual_tools import next_request


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_completed_patch_local_receipt_survives_cold_resume(responses, tmp_path, status):
    editor = Editor(status)
    original = patch_call({"type": "delete_file", "path": "application://owned"})
    responses.respond(original)
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("continued"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use the application editor.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor)],
        )
        storage = LocalStorage(tmp_path / "profile")
        async with agent.session(storage=storage) as source:
            identity = source.id
            assert (await (await source.submit("edit")).get_result()).final_output == "first"
            assert len(editor.operations) == 1
        async with agent.session(storage=storage, resume=identity) as restored:
            assert restored.id == identity
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "continued"
        assert len(editor.operations) == 1
    replay = responses.requests[2]["input"]
    assert (
        next(item for item in replay if item.get("type") == "apply_patch_call") == original["item"]
    )
    outputs = [item for item in replay if item.get("type") == "apply_patch_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "native-patch"
    assert outputs[0]["status"] == status
    assert outputs[0]["output"] == "exact editor output"
    assert len(responses.requests) == 3


@pytest.mark.parametrize("approve", [True, False])
async def test_configured_patch_approval_precedes_editor_entry(responses, approve):
    editor = Editor()
    declaration = apply_patch_tool(editor, needs_approval=True if approve else lambda _: True)
    responses.respond(patch_call({"type": "delete_file", "path": "application://owned"}))
    responses.respond(assistant_output("done"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use the application editor.",
            model="gpt-5",
            provider=provider,
            tools=[declaration],
        )
        async with agent.session() as session:
            turn = await session.submit("edit")
            request_task = asyncio.create_task(next_request(session.interactions(), declaration))
            completion = asyncio.create_task(turn.get_result())
            try:
                done, _ = await asyncio.wait(
                    (request_task, completion), timeout=10, return_when=asyncio.FIRST_COMPLETED
                )
                assert request_task in done, "The editor completed without its required approval"
                request = request_task.result()
                assert request.call_id == "native-patch"
                assert request.turn_id == turn.turn_id
                assert editor.operations == []
                assert len(responses.requests) == 1
                if approve:
                    await request.approve()
                else:
                    await request.reject("application denied edit")
                assert (await completion).final_output == "done"
                assert len(editor.operations) == int(approve)
                with pytest.raises(InteractionExpiredError):
                    await request.approve()
            finally:
                for task in (request_task, completion):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(request_task, completion, return_exceptions=True)
    output = next(
        item
        for item in responses.requests[1]["input"]
        if item.get("type") == "apply_patch_call_output"
    )
    assert output["call_id"] == "native-patch"
    assert output["status"] == ("completed" if approve else "failed")
    assert len(responses.requests) == 2
