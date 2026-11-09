"""Computer effects survive real process loss and require real native receipts."""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from agents import (
    Agent,
    ComputerProvider,
    LocalRecoveryRequired,
    LocalStorage,
    NativeProvider,
    computer_tool,
)
from tests.native_fixtures import responses as responses
from tests.test_native_computer import computer_call
from tests.test_native_computer_adapter import Driver
from tests.test_native_local_storage import _collect

CHECKS = [{"id": "screen-policy", "code": "screen", "message": "Confirm screen"}]


async def worker():
    url, directory, outcome = sys.argv[1:]
    root = Path(directory)

    class CrashDriver(Driver):
        def wait(self):
            if outcome == "applied":
                with (root / "effects").open("a") as stream:
                    stream.write("physical device effect\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            os._exit(67)

    provider = NativeProvider(base_url=url, api_key="synthetic")
    agent = Agent(
        instructions="Use computer.",
        provider=provider,
        model="gpt-5",
        tools=[computer_tool(CrashDriver(), on_safety_check=lambda *_: True)],
    )
    async with agent.session(storage=LocalStorage(root / "profile")) as session:
        print(json.dumps({"opened": session.id}), flush=True)
        await (await session.submit("Use the computer")).get_result()


@pytest.mark.parametrize("outcome", ["applied", "not_applied"])
async def test_computer_local_reopen_requires_screenshot_receipt_without_replay(
    responses, tmp_path, outcome
):
    responses.respond(computer_call(action={"type": "wait"}, pending_safety_checks=CHECKS))
    test_root = str(Path(__file__).resolve().parents[1])
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-m",
        "tests.test_native_computer_recovery",
        responses.url,
        str(tmp_path),
        outcome,
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
    acquired = []
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    agent = Agent(
        instructions="Use computer.",
        provider=provider,
        tools=[computer_tool(ComputerProvider(lambda _: acquired.append(True) or Driver()))],
    )
    storage = LocalStorage(tmp_path / "profile")
    try:
        with pytest.raises(LocalRecoveryRequired) as missing:
            async with agent.session(storage=storage, resume=thread_id):
                pytest.fail("Unknown device effects cannot reopen silently")
        (call,) = missing.value.calls
        assert call.name == "computer"
        assert isinstance(call.arguments, dict)
        assert call.arguments["type"] == "computer_call"
        assert call.arguments["pending_safety_checks"] == CHECKS
        assert len(responses.requests) == 1
        result = json.dumps(
            {
                "output": {
                    "type": "computer_screenshot",
                    "image_url": "data:image/png;base64,aA==",
                },
                "acknowledged_safety_checks": CHECKS,
            }
        )
        receipt = call.reconcile(outcome=outcome, result=result)
        for receipts in ([receipt], [], [receipt]):
            async with agent.session(
                storage=storage, resume=thread_id, reconciled_calls=receipts
            ) as session:
                outputs = [
                    item
                    async for item in session.items()
                    if item["type"] == "function_call_output" and item["call_id"] == call.call_id
                ]
                assert len(outputs) == 1
                assert outputs[0]["turn_id"] == call.turn_id
                assert outputs[0]["reconciliation"] == outcome
                assert json.loads(outputs[0]["output"]) == json.loads(result)
        assert acquired == []
        assert len(responses.requests) == 1
        (rollout,) = (tmp_path / "profile").rglob(f"*{thread_id}.jsonl")
        saved = [json.loads(line) for line in rollout.read_text().splitlines()]
        computer_outputs = [
            row["payload"]
            for row in saved
            if row["type"] == "response_item" and row["payload"]["type"] == "computer_call_output"
        ]
        assert len(computer_outputs) == 1
        assert computer_outputs[0]["output"]["image_url"] == "data:image/png;base64,aA=="
        assert computer_outputs[0]["acknowledged_safety_checks"] == CHECKS
        assert (tmp_path / "effects").exists() == (outcome == "applied")
    finally:
        await provider.aclose()


async def test_native_close_joins_sync_driver_issued_task_before_disposal(responses):
    entered, release = threading.Event(), threading.Event()
    finish = asyncio.Event()
    loop = asyncio.get_running_loop()
    order = []

    async def operation():
        await finish.wait()
        order.append("operation finished")

    async def issue():
        return asyncio.create_task(operation())

    class IssuedDriver(Driver):
        def wait(self):
            task = asyncio.run_coroutine_threadsafe(issue(), loop).result(5)
            entered.set()
            assert release.wait(5)
            return task

    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    responses.respond(computer_call(action={"type": "wait"}))
    agent = Agent(
        instructions="Use computer.",
        provider=provider,
        tools=[
            computer_tool(
                ComputerProvider(lambda _: IssuedDriver(), lambda _: order.append("disposed"))
            )
        ],
    )
    context = agent.session()
    session = await context.__aenter__()
    try:
        await session.submit("wait")
        assert await asyncio.to_thread(entered.wait, 5)
        closing = asyncio.create_task(session.aclose())
        await asyncio.sleep(0)
        release.set()
        await asyncio.sleep(0.05)
        assert order == []
        assert not closing.done()
        finish.set()
        await asyncio.wait_for(closing, 5)
        assert order == ["operation finished", "disposed"]
        assert len(responses.requests) == 1
    finally:
        release.set()
        finish.set()
        await context.__aexit__(None, None, None)
        await provider.aclose()


def test_computer_native_completion_without_canonical_output_remains_uncertain():
    from agents._history import project_history

    original = {
        "type": "computer_call",
        "id": "computer-item",
        "call_id": "computer-id",
        "actions": [{"type": "wait"}],
        "pending_safety_checks": CHECKS,
    }
    history = [
        {"type": "turn_context", "payload": {"turn_id": "original-turn"}},
        {"type": "response_item", "payload": original},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "original-turn",
                "item": {
                    "type": "DynamicToolCall",
                    "id": "computer-id",
                    "tool": "computer",
                    "status": "completed",
                    "success": True,
                    "arguments": {"normalized": True},
                },
            },
        },
    ]
    uncertain = []
    _, items = project_history(history, "thread", uncertain_calls=uncertain)
    assert len(uncertain) == 1
    assert uncertain[0].arguments == original
    assert items[0]["arguments"] == {"normalized": True}
    assert items[0]["status"] == "in_progress"


if __name__ == "__main__":
    asyncio.run(worker())
