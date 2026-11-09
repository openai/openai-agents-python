"""Native declarations and real subprocess delegation with a synthetic CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agents import Agent, FunctionTool
from agents.codex import CodexOptions, TurnOptions, codex_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Synthetic executable uses a Unix shebang")


def cli(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic-codex"
    events = [
        {"type": "thread.started", "thread_id": "synthetic-thread"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-1",
                "type": "agent_message",
                "text": "CLI completed",
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 3,
                "cached_input_tokens": 1,
                "output_tokens": 2,
            },
        },
    ]
    path.write_text(
        f"#!{sys.executable}\nimport json, sys\nsys.stdin.read()\n"
        f"events = {events!r}\n"
        "for event in events: print(json.dumps(event), flush=True)\n"
    )
    path.chmod(0o700)
    return path


async def test_cli_tool_uses_native_binding_and_real_cli_stream(tmp_path: Path) -> None:
    events = []
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(cli(tmp_path))),
        turn_options=TurnOptions(idle_timeout_seconds=5),
        persist_session=True,
        on_event=events.append,
    )
    assert isinstance(declaration, FunctionTool)
    binding = declaration._bind(None, None, declaration)
    result = await binding.callback(prompt="Do the task", thread_id=None)
    assert result == {
        "thread_id": "synthetic-thread",
        "response": "CLI completed",
        "usage": {"input_tokens": 3, "cached_input_tokens": 1, "output_tokens": 2},
    }
    assert [event.type for event in events] == [
        "thread.started",
        "item.completed",
        "turn.completed",
    ]


async def test_native_agent_delegates_to_external_cli(tmp_path: Path, responses) -> None:
    responses.replies.extend(
        [
            [function_call("codex", {"prompt": "Do the task", "thread_id": None}, "cli-1")],
            [assistant_output("done")],
        ]
    )
    declaration = codex_tool(codex_options=CodexOptions(codex_path_override=str(cli(tmp_path))))
    async with Agent(
        instructions="Use codex.", provider=provider(responses), tools=[declaration]
    ).session() as session:
        result = await (await session.submit("Do the task")).wait()
        assert result.output == "done"
    assert "CLI completed" in json.dumps(responses.requests[1])


async def test_cli_idle_timeout_closes_the_subprocess(tmp_path: Path) -> None:
    path = tmp_path / "idle-codex"
    pid_path = tmp_path / "pid"
    path.write_text(
        f"#!{sys.executable}\nimport os, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    path.chmod(0o700)
    declaration = codex_tool(
        codex_options={"codex_path_override": str(path)},
        turn_options={"idle_timeout_seconds": 0.1},
    )
    binding = declaration._bind(None, None, declaration)
    with pytest.raises(RuntimeError, match="idle"):
        await binding.callback(prompt="Do the task", thread_id=None)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)


async def test_cli_callback_cancellation_joins_the_subprocess(tmp_path: Path) -> None:
    path = tmp_path / "blocking-codex"
    pid_path = tmp_path / "pid"
    path.write_text(
        f"#!{sys.executable}\nimport json, os, time\n"
        f"open({str(pid_path)!r}, 'w').write(str(os.getpid()))\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'blocking'}), flush=True)\n"
        "time.sleep(30)\n"
    )
    path.chmod(0o700)
    started = asyncio.Event()
    declaration = codex_tool(
        codex_options=CodexOptions(codex_path_override=str(path)),
        on_event=lambda event: started.set(),
    )
    binding = declaration._bind(None, None, declaration)
    task = asyncio.create_task(binding.callback(prompt="Do the task", thread_id=None))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)
