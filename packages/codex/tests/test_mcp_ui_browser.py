"""Exercise official UI clients against the native local HTTP transports."""

import asyncio
import os
from pathlib import Path

import pytest

from examples.codex.mcp_ui.server import start


@pytest.mark.skipif(
    os.environ.get("OPENAI_AGENTS_BROWSER_TESTS") != "1",
    reason="Set OPENAI_AGENTS_BROWSER_TESTS=1 to run the real browser witness",
)
async def test_native_counter_stream_and_iframe_action():
    directory = Path(__file__).resolve().parents[3] / "examples/codex/mcp_ui"
    assert (directory / "dist/client.js").exists(), "Build examples/codex/mcp_ui first"
    runner, url = await start()
    try:
        process = await asyncio.create_subprocess_exec(
            os.environ.get("NODE_BINARY", "node"),
            str(directory / "browser-check.mjs"),
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode() + stdout.decode()
        assert '"iframeCount":2' in stdout.decode()
    finally:
        await runner.cleanup()
