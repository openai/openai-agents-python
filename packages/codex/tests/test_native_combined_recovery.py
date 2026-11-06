"""Restored typed turns retain selected tools and accepted native results."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agents import Agent, Environment, FunctionTool, NativeProvider, ToolRef
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_policies import OutputEvents
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_schemas import Answer
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import declarations
from tests.test_native_vm_state import run_worker


@pytest.mark.requires_native_linux_sandbox
async def test_typed_static_restore_retains_accepted_result_without_replaying_effects(
    responses: ResponsesFixture, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    raw = "combined-private-7319"
    policies = []
    replies = []

    async def accept(ctx, value):
        policies.append(ctx)
        assert ctx.tool_name == "exec_command" and raw in json.dumps(value)
        return {"accepted": "combined-public-result"}

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        (executor.root / "private.txt").write_text(raw)
        environment = declaration(executor, transform_result=accept)
        agent = Agent(
            instructions="Read the records and return the typed answer.",
            tools=[lookup],
            provider=provider(responses),
            output_type=Answer,
        )
        responses.respond(
            function_call(
                "exec_command", {"cmd": "cat private.txt; printf effect >> effect.txt"}, "direct"
            )
        )
        responses.respond(
            function_call("lookup", {"key": "A"}, "manual-A"),
            function_call("lookup", {"key": "B"}, "manual-B"),
        )
        async with agent.session(environment=environment) as source:
            source_events = OutputEvents(source._native)
            source._native = source_events
            turn = await source.submit(
                "Read both records", tools=[lookup, ToolRef("exec_command", "functions")]
            )
            stream = source.interactions()
            pending = [await next_request(stream, lookup), await next_request(stream, lookup)]
            by_call = {request.call_id: request for request in pending}
            replies.append(by_call["manual-A"].arguments.key)
            await by_call["manual-A"].respond("accepted-A")
            saved = SessionState.from_json((await source.suspend()).to_json())
            assert saved.native_continuation is not None
            assert [item.call_id for item in saved.native_continuation.pending_manual] == [
                "manual-B"
            ]
            assert set(saved.operation_tools) == {
                lookup.ref,
                ToolRef("exec_command", "functions"),
            }
        expected_format = responses.requests[0]["text"]["format"]
        assert expected_format["strict"] is True
        assert saved.native_continuation.output_schema == expected_format["schema"]
        assert "combined-public-result" in saved.to_json()
        assert raw not in saved.to_json()
        responses.respond(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "unselected",
                    "input": "*** Begin Patch\n*** Add File: forbidden.txt\n+effect\n*** End Patch",
                },
            }
        )
        responses.respond(assistant_output('{"value":"done"}'))
        async with agent.session(state=saved, environment=environment) as restored:
            restored_events = OutputEvents(restored._native)
            restored._native = restored_events
            handle = restored.restored_turn
            assert handle is not None and handle.turn_id == turn.turn_id
            request = await next_request(restored.interactions(), lookup)
            assert request.call_id == "manual-B"
            replies.append(request.arguments.key)
            await request.respond("accepted-B")
            assert (await handle.get_result()).final_output == Answer(value="done")
            history = json.loads(await restored_events.read_thread_history(None))
        assert replies == ["A", "B"]
        assert len(policies) == 1 and policies[0].call_id == "direct"
        assert (executor.root / "effect.txt").read_text() == "effect"
        assert not (executor.root / "forbidden.txt").exists()
        assert "combined-public-result" in tool_output(responses.requests[1], "direct")
        assert tool_output(responses.requests[-1], "manual-A") == "accepted-A"
        assert tool_output(responses.requests[-1], "manual-B") == "accepted-B"
        assert raw not in json.dumps(
            [source_events.events, restored_events.events, history, responses.requests]
        )
        assert all(request["text"]["format"] == expected_format for request in responses.requests)
        assert all(
            set(declarations(request)) == {("functions", "lookup"), ("functions", "exec_command")}
            for request in responses.requests
        )


async def combined_vm_worker() -> None:
    request = json.load(sys.stdin)
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    policies = []

    async def accept(ctx, value):
        policies.append(ctx.tool_name)
        assert "combined-vm-private-4197" in json.dumps(value)
        return {"accepted": "combined-vm-public"}

    remote = request["executor"]
    environment = Environment(
        url=remote["url"],
        cwd=remote["cwd"],
        headers={"Authorization": "Bearer " + remote["token"]},
        tools=["exec_command", "write_stdin", "apply_patch"],
        transform_result=accept,
    )
    agent = Agent(
        instructions="Read the stored values and return a typed answer.",
        tools=[lookup],
        provider=NativeProvider(base_url=request["url"], api_key=request["key"]),
        programmatic_tool_calling=True,
        output_type=Answer,
        model="gpt-5.5",
    )
    state_path = Path(request["state"])
    state = SessionState.from_json(state_path.read_text()) if request["stage"] == "import" else None
    async with agent.session(state=state, environment=environment) as session:
        captured = OutputEvents(session._native)
        session._native = captured
        if request["stage"] == "export":
            assert (await (await session.submit("Store once")).get_result()).final_output == Answer(
                value="stored"
            )
            turn = await session.submit("Read both", tools=[lookup])
            stream = session.interactions()
            pending = [await next_request(stream, lookup), await next_request(stream, lookup)]
            by_call = {item.call_id: item for item in pending}
            await by_call["vm-A"].respond("accepted-A")
            saved = await session.suspend()
            assert saved.native_continuation is not None
            assert saved.native_continuation.turn_id == turn.turn_id
            assert [item.call_id for item in saved.native_continuation.pending_manual] == ["vm-B"]
            assert saved.format_version == 7 and saved.root_code_mode is not None
            assert saved.root_code_mode["stored_values"]["accepted"] == {
                "accepted": "combined-vm-public"
            }
            state_path.write_text(saved.to_json())
            assert policies == ["exec_command"]
            history = saved.history
        else:
            assert state is not None and state.native_continuation is not None
            turn = session.restored_turn
            assert turn is not None and turn.turn_id == state.native_continuation.turn_id
            current = await next_request(session.interactions(), lookup)
            assert current.call_id == "vm-B"
            await current.respond("accepted-B")
            assert (await turn.get_result()).final_output == Answer(value="done")
            assert not policies
            copied = await session.save_state()
            assert copied.root_code_mode is not None and state.root_code_mode is not None
            assert copied.root_code_mode["stored_values"] == state.root_code_mode["stored_values"]
            assert copied.root_code_mode["next_cell_id"] > state.root_code_mode["next_cell_id"]
            history = json.loads(await captured.read_thread_history(None))
    Path(request["events"]).write_text(json.dumps([captured.events, history]))
    await agent.provider.aclose()


@pytest.mark.requires_native_linux_sandbox
async def test_settled_vm_strict_manual_subset_survives_source_process_exit(
    responses: ResponsesFixture, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with timeout(90), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        (executor.root / "private.txt").write_text("combined-vm-private-4197")
        responses.respond(
            code_call(
                "store('accepted',await tools.exec_command({"
                "cmd:'cat private.txt; printf effect >> effect.txt'}));"
                "store('structured',{n:7,a:[null,true,'雪']});",
                "stored-effect",
            )
        )
        responses.respond(assistant_output('{"value":"stored"}'))
        responses.respond(
            function_call("lookup", {"key": "A"}, "vm-A"),
            function_call("lookup", {"key": "B"}, "vm-B"),
        )
        state_path = tmp_path / "state.json"
        source_events, destination_events = tmp_path / "source.json", tmp_path / "destination.json"
        request = {
            "url": responses.url,
            "state": str(state_path),
            "executor": {"url": executor.url, "cwd": str(executor.root), "token": executor.token},
        }
        await run_worker(
            {
                **request,
                "stage": "export",
                "key": "source-combined-key",
                "events": str(source_events),
            },
            module=__name__,
        )
        saved = SessionState.from_json(state_path.read_text())
        expected = responses.requests[-1]["text"]["format"]
        assert expected["strict"] is True
        assert saved.native_continuation.output_schema == expected["schema"]
        assert set(saved.operation_tools) == {
            ToolRef("lookup"),
            ToolRef("exec", "functions"),
            ToolRef("wait", "functions"),
        }
        assert (executor.root / "effect.txt").read_text() == "effect"
        responses.respond(
            code_call("text(JSON.stringify([load('accepted'),load('structured')]));", "read-stored")
        )
        responses.respond(
            function_call("exec_command", {"cmd": "printf forbidden >> forbidden.txt"}, "excluded")
        )
        responses.respond(assistant_output('{"value":"done"}'))
        await run_worker(
            {
                **request,
                "stage": "import",
                "key": "destination-combined-key",
                "events": str(destination_events),
            },
            module=__name__,
        )
        assert (executor.root / "effect.txt").read_text() == "effect"
        assert not (executor.root / "forbidden.txt").exists()
        output = code_output(responses.requests[-1], "read-stored")
        assert '"accepted":"combined-vm-public"' in output and '"a":[null,true,"雪"]' in output
        assert tool_output(responses.requests[-1], "vm-A") == "accepted-A"
        assert tool_output(responses.requests[-1], "vm-B") == "accepted-B"
        assert (
            sum(
                item.get("type") == "function_call_output" and item.get("call_id") == "vm-A"
                for item in responses.requests[-1]["input"]
            )
            == 1
        )
        assert all(
            # CodeMode exec is a top-level Responses custom tool.
            set(declarations(body))
            | {
                ("functions", tool["name"])
                for tool in body.get("tools", [])
                if tool["type"] == "custom"
            }
            == {("functions", "lookup"), ("functions", "exec"), ("functions", "wait")}
            and body["text"]["format"] == expected
            for body in responses.requests[2:]
        )
        assert all(
            headers["authorization"] == "Bearer destination-combined-key"
            for headers in responses.request_headers[3:]
        )
        assert "combined-vm-private-4197" not in (
            state_path.read_text()
            + source_events.read_text()
            + destination_events.read_text()
            + json.dumps(responses.requests)
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(combined_vm_worker())
