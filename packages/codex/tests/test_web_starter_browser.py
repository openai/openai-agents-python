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

from examples.web_starter.app import create_app

CLIENT = Path(__file__).resolve().parents[1] / "examples" / "web_starter"


async def test_browser_client_over_real_http(tmp_path):
    node = shutil.which("node")
    if (
        node is None
        or not (CLIENT / "client.js").exists()
        or not (CLIENT / "node_modules/playwright").exists()
    ):
        if os.environ.get("AGENTS_REQUIRE_WEB_BROWSER") == "1":
            pytest.fail(
                "In packages/codex/examples/web_starter run npm ci, npm run build, "
                "and npx playwright install chromium"
            )
        pytest.skip("optional: install web starter browser dependencies and Chromium")
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
