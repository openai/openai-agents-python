"""Interactive handles stay within their issuing native environment selection."""

from __future__ import annotations

import base64
import re

import pytest

from agents import Agent, Environment
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_environment_leases_checklist import trust
from tests.test_native_agent import provider
from tests.test_native_executor_tools import tool_output


@pytest.mark.parametrize(
    ("distinct", "chars"),
    [(True, "target-input"), (True, ""), (False, "target-input")],
    ids=["foreign-write", "foreign-poll", "inherited-environment"],
)
async def test_interactive_handle_requires_current_environment(
    tmp_path, monkeypatch, responses, distinct, chars
):
    native = provider(responses)
    try:
        async with (
            timeout(45),
            ExecutorPeer(
                tmp_path / "executor",
                synthetic_process_output="source-output",
                synthetic_process_stays_running=True,
            ).serve() as peer,
        ):
            trust(peer, tmp_path, monkeypatch)
            other = peer.root / "other"
            other.mkdir()

            def environment(cwd):
                return Environment(
                    url=peer.url,
                    headers={"Authorization": "Bearer " + peer.token},
                    cwd=str(cwd),
                    tools=("exec_command", "write_stdin"),
                    write_policy="legacy",
                )

            target = Agent(
                name="Target",
                instructions="Continue the supplied terminal.",
                provider=native,
                environment=environment(other) if distinct else None,
                handoffs=[lambda: root],
            )
            root = Agent(
                name="Root",
                instructions="Start a terminal then transfer.",
                provider=native,
                handoffs=[target],
            )
            emitted = False

            async def inject_late_output(event):
                nonlocal emitted
                if event.get("item", {}).get("call_id") == "target-use" and not emitted:
                    emitted = True
                    await peer.emit_synthetic_output(
                        peer.synthetic_process_starts[0], "late-source-output"
                    )

            responses.before_event = inject_late_output
            responses.respond(
                function_call(
                    "exec_command",
                    {
                        "cmd": "held-command",
                        "tty": True,
                        "login": False,
                        "yield_time_ms": 1,
                    },
                    "start",
                )
            )
            responses.respond(assistant_output("ready"))
            try:
                async with root.session(environment=environment(peer.root)) as session:
                    await (await session.submit("Start the terminal.")).get_result()
                    output = tool_output(responses.requests[-1], "start")
                    found = re.search(r"session ID (\d+)", output)
                    assert found is not None, output
                    process_id = int(found.group(1))
                    responses.respond(
                        function_call(
                            "write_stdin",
                            {
                                "session_id": process_id,
                                "chars": "source-input",
                                "yield_time_ms": 1,
                            },
                            "source-write",
                        )
                    )
                    responses.respond(function_call("transfer_to_target", {}, "handoff"))
                    responses.respond(
                        function_call(
                            "write_stdin",
                            {
                                "session_id": process_id,
                                "chars": chars,
                                "yield_time_ms": 1,
                            },
                            "target-use",
                        ),
                        function_call(
                            "write_stdin",
                            {
                                "session_id": -1,
                                "chars": chars,
                                "yield_time_ms": 1,
                            },
                            "missing-use",
                        ),
                    )
                    responses.respond(function_call("transfer_to_root", {}, "return"))
                    responses.respond(
                        function_call(
                            "write_stdin",
                            {
                                "session_id": process_id,
                                "chars": "",
                                "yield_time_ms": 1,
                            },
                            "source-return",
                        )
                    )
                    responses.respond(assistant_output("done"))
                    result = await (await session.submit("Continue, then transfer.")).get_result()
                    assert result.final_output == "done"
                    source_output = tool_output(responses.requests[-5], "source-write")
                    assert "Process running with session ID" in source_output
                    target_output = tool_output(responses.requests[-3], "target-use")
                    missing_output = tool_output(responses.requests[-3], "missing-use")
                    returned_output = tool_output(responses.requests[-1], "source-return")
                    assert emitted
                    if distinct:
                        assert "late-source-output" not in target_output, target_output
                        assert "late-source-output" in returned_output, returned_output
                        assert target_output.replace(
                            str(process_id), "<id>"
                        ) == missing_output.replace("-1", "<id>")
                    else:
                        assert "late-source-output" in target_output, target_output
                        assert "late-source-output" not in returned_output, returned_output
                        assert "Process running with session ID" in target_output
                    writes = [
                        request
                        for request in peer.requests
                        if request.get("method") == "process/write"
                    ]
                    assert [
                        base64.b64decode(request["params"]["chunk"]).decode() for request in writes
                    ] == (["source-input"] if distinct else ["source-input", "target-input"])
                    peer.synthetic_process_stays_running = False
            finally:
                peer.synthetic_process_stays_running = False
    finally:
        await native.aclose()
