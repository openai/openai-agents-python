"""Run the public persistent-storage example in independent processes."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from tests.native_fixtures import assistant_output, function_call, responses as responses

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")


@pytest.mark.asyncio
async def test_example_creates_and_reopens_history_in_a_fresh_process(responses, tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = root / "examples/canonical_storage/main.py"
    database = tmp_path / "example.sqlite"
    environment = {
        **os.environ,
        "OPENAI_API_KEY": "synthetic-example",
        "PYTHONPATH": str(root / "src"),
    }
    responses.respond(function_call("order_status", {"order_id": "example-123"}, "example-call"))
    responses.respond(assistant_output("The order shipped."))

    async def run(*arguments):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(script),
            str(database),
            "--base-url",
            responses.url,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        try:
            output, error = await asyncio.wait_for(process.communicate(), 45)
            assert process.returncode == 0, (output.decode(), error.decode())
            return output.decode()
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

    first = await run()
    identity = first.splitlines()[0].removeprefix("Session ID: ")
    assert first.splitlines()[1] == "The order shipped."
    assert len(responses.requests) == 2
    responses.respond(assistant_output("The earlier lookup said shipped."))
    second = await run("--resume", identity, "--prompt", "What did you find?")
    assert second.splitlines() == ["Session ID: " + identity, "The earlier lookup said shipped."]
    assert len(responses.requests) == 3
    receipts = [
        item
        for item in responses.requests[-1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(receipts) == 1
    assert "example-123" in str(receipts[0])
