"""Exercise native submission ownership without a Python model controller."""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import sys

from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    native_options,
    responses as responses,
    tool_output,
)


async def test_native_startup_and_close_do_not_import_compatibility_controller(tmp_path, responses):
    script = """
import asyncio
import json
import sys
from agents import _native

async def main():
    options = json.loads(sys.argv[1])
    engine = await _native.create_engine(
        json.dumps(options['config']), json.dumps(options['runtime'])
    )
    try:
        thread = await engine.start_replaceable_thread(json.dumps(options), '[]')
        await thread.close()
    finally:
        await engine.close()
    assert _native.Engine.__module__ == 'agents._native'
    forbidden = ('agents.models', 'agents.run_internal', 'codex_agents')
    assert not any(name.startswith(forbidden) for name in sys.modules)

asyncio.run(main())
"""
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            script,
            json.dumps(native_options(tmp_path, responses)),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


async def test_start_if_idle_preserves_pending_tool_and_rejects_steering(tmp_path, responses):
    effects = []

    options = native_options(tmp_path, responses)
    specs = json.dumps(
        [
            {
                "type": "function",
                "name": "lookup",
                "description": "Read an account.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"account": {"type": "string"}},
                    "required": ["account"],
                    "additionalProperties": False,
                },
                "deferLoading": False,
            }
        ]
    )
    native = importlib.import_module("agents._native")
    engine = await native.create_engine(
        json.dumps(options["config"]), json.dumps(options["runtime"])
    )
    thread = None
    responses.respond(function_call("lookup", {"account": "account-a"}, "lookup-a"))
    responses.respond(assistant_output("first-result"))
    responses.respond(assistant_output("second-result"))

    async def submit(text):
        return json.loads(
            await thread._submit(
                json.dumps(
                    {
                        "mode": "start_if_idle",
                        "input": [{"type": "text", "text": text}],
                    }
                )
            )
        )

    async def until(kind):
        async with timeout(15):
            while True:
                event = json.loads(await thread.next_event())
                assert event is not None, "Native thread closed before the expected event"
                assert event["msg"]["type"] != "error", event
                if event["msg"]["type"] == kind:
                    return event

    try:
        thread = await engine.start_replaceable_thread(
            json.dumps(
                {
                    **options,
                    "instructions": "Use the declared account lookup tool.",
                    "tool_policy": {
                        "allowed_tools": [{"name": "lookup", "namespace": "functions"}]
                    },
                }
            ),
            specs,
        )
        first = await submit("first-input")
        assert first["status"] == "started"
        call = (await until("dynamic_tool_call_request"))["msg"]
        assert call["turnId"] == first["turn_id"]
        assert call["callId"] == "lookup-a"

        rejected = await submit("rejected-input-must-not-steer")
        assert rejected == {"status": "not_submitted", "reason": {"type": "not_idle"}}
        assert effects == []
        assert len(responses.requests) == 1

        thread._check_python_tool(json.dumps(call))
        arguments = call["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        effects.append(arguments["account"])
        output = {"contentItems": [{"type": "inputText", "text": "account-found"}], "success": True}
        reply = json.dumps({"request": call, "response": output})
        assert await thread._respond_python_tool(reply) is True
        assert await thread._respond_python_tool(reply) is False
        terminal = (await until("task_complete"))["msg"]
        assert terminal["turn_id"] == first["turn_id"]
        assert terminal["last_agent_message"] == "first-result"
        assert effects == ["account-a"]
        assert tool_output(responses.requests[1], "lookup-a") == "account-found"
        assert "rejected-input-must-not-steer" not in json.dumps(responses.requests)

        second = await submit("second-input")
        assert second["status"] == "started"
        assert second["turn_id"] != first["turn_id"]
        terminal = (await until("task_complete"))["msg"]
        assert terminal["turn_id"] == second["turn_id"]
        assert terminal["last_agent_message"] == "second-result"
        assert effects == ["account-a"]

        history = json.loads(await thread.read_thread_history(None))
        assert "rejected-input-must-not-steer" not in json.dumps(history)
        records = [
            record["payload"]
            for record in history
            if record["type"] == "response_item"
            and not record.get("metadata", {}).get("client_authored", False)
            and record["payload"].get("call_id") == "lookup-a"
        ]
        [admitted] = [record for record in records if record["type"] == "function_call"]
        [receipt] = [record for record in records if record["type"] == "function_call_output"]
        assert admitted["name"] == "lookup"
        assert json.loads(admitted["arguments"]) == {"account": "account-a"}
        assert "account-found" in json.dumps(receipt["output"])
    finally:
        if thread is not None:
            await thread.close()
        await engine.close()
