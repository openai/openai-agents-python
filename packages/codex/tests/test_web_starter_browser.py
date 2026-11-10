"""Optional real Chromium check for the exact pinned browser client."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

pytest.importorskip("fcntl", reason="The starter requires POSIX flock and private directory modes")

from examples.codex.web_starter.app import create_app

CLIENT = Path(__file__).resolve().parents[3] / "examples" / "codex" / "web_starter"


@pytest.mark.skipif(
    os.environ.get("OPENAI_AGENTS_BROWSER_TESTS") != "1"
    and os.environ.get("AGENTS_REQUIRE_WEB_BROWSER") != "1",
    reason="Set OPENAI_AGENTS_BROWSER_TESTS=1 to run the real browser witness",
)
async def test_browser_client_over_real_http(tmp_path):
    node = shutil.which("node")
    if (
        node is None
        or not (CLIENT / "client.js").exists()
        or not (CLIENT / "node_modules/playwright").exists()
    ):
        pytest.fail(
            "In examples/codex/web_starter run npm ci, npm run build, "
            "and npx playwright install chromium"
        )
    async with TestServer(create_app(tmp_path)) as server:
        process = await asyncio.create_subprocess_exec(
            node,
            str(CLIENT / "browser-test.mjs"),
            str(server.make_url("/")).rstrip("/"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode == 0, stderr.decode()
        assert "typed-final" in json.loads(stdout)["verified"]
