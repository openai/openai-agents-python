"""Physical, multi-process enrollment/approval witness for the new binding."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


async def child(mode: str, url: str, path: Path, variant: str = "current") -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if "proxy" not in key.lower() and key != "OPENAI_API_KEY"
    }
    environment["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.runtime_handoff_enrollment_process",
        mode,
        url,
        str(path),
        variant,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 35)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, output.decode()


async def test_explicit_enrollment_restores_current_owner_without_factory_replay(
    responses, tmp_path
):
    responses.respond(function_call("delegate", {"input": "late"}, "route"))
    responses.respond(function_call("write_once", {"value": "first"}, "effect"))
    await child("write", responses.url, tmp_path)
    assert len(responses.requests) == 2 and not (tmp_path / "effects.jsonl").exists()

    # Every refusal runs in a fresh interpreter before another physical model request.
    for variant in ("missing", "changed", "root"):
        await child("read", responses.url, tmp_path, variant)
        assert len(responses.requests) == 2

    responses.respond(assistant_output("done"))
    await child("read", responses.url, tmp_path)
    assert len(responses.requests) == 3
    assert tool_output(responses.requests[2], "effect") == "read:first"

    # An explicit current-caller verifier retains the approved target's policy.
    # The distinct later call must not repeat the first callback or factory.
    responses.respond(function_call("write_once", {"value": "second"}, "effect-later"))
    responses.respond(assistant_output("done again"))
    await child("again", responses.url, tmp_path)
    assert len(responses.requests) == 5
    assert tool_output(responses.requests[4], "effect-later") == "again:second"
    assert [json.loads(line) for line in (tmp_path / "effects.jsonl").read_text().splitlines()] == [
        {"owner": "read", "value": "first"},
        {"owner": "again", "value": "second"},
    ]
    receipts = [json.loads(p.read_text()) for p in tmp_path.glob("*.receipt.json")]
    assert len(receipts) == 6 and len({receipt["pid"] for receipt in receipts}) == 6


async def test_enrollment_refuses_opening_name_collision_before_capture(responses, tmp_path):
    responses.respond(function_call("delegate", {"input": "late"}, "route"))
    responses.respond(function_call("write_once", {"value": "first"}, "effect"))
    await child("write", responses.url, tmp_path, "collision")
    assert len(responses.requests) == 2
    assert not (tmp_path / "effects.jsonl").exists()
    assert not (tmp_path / "pending.json").exists()
