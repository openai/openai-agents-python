"""Cloudflare example CLI credentials reach the resumed HTTP session."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("cli_key", "environment_key", "expected"),
    [
        ("cli-token", None, "Bearer cli-token"),
        (None, "environment-token", "Bearer environment-token"),
        ("cli-token", "stale-token", "Bearer cli-token"),
        (None, None, None),
        ("", "environment-token", "Bearer environment-token"),
        ("", None, None),
    ],
)
def test_cloudflare_cli_authenticates_resumed_session(
    cli_key: str | None, environment_key: str | None, expected: str | None
) -> None:
    requests: list[tuple[str, str | None]] = []

    class Worker(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.command, self.headers.get("Authorization")))
            body = json.dumps({"running": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self) -> None:
            requests.append((self.command, self.headers.get("Authorization")))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    program = """
import asyncio
import runpy
import sys

sys.path.insert(0, sys.argv[1])
from agents.classic.extensions.sandbox.cloudflare.sandbox import (
    CloudflareSandboxClient, CloudflareSandboxSessionState,
)
from agents.classic.sandbox import Manifest
from agents.classic.sandbox.snapshot import resolve_snapshot

worker_url = sys.argv[2]
original_run = asyncio.run

async def resume():
    state = CloudflareSandboxSessionState(
        manifest=Manifest(), snapshot=resolve_snapshot(None, "cli-resume"),
        worker_url=worker_url, sandbox_id="existing",
    )
    client = CloudflareSandboxClient()
    session = await client.resume(state)
    await client.delete(session)

def run_resume_probe(coroutine):
    # Exercise CLI configuration with a real resume, without a model or snapshot service.
    coroutine.close()
    return original_run(resume())

asyncio.run = run_resume_probe
sys.argv = ["cloudflare_runner", "--worker-url", worker_url, *sys.argv[3:]]
runpy.run_module("examples.classic.sandbox.extensions.cloudflare_runner", run_name="__main__")
"""
    worker = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
    thread = threading.Thread(target=worker.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment.pop("CLOUDFLARE_SANDBOX_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    if environment_key is not None:
        environment["CLOUDFLARE_SANDBOX_API_KEY"] = environment_key
    root = Path(__file__).resolve().parents[3]
    args = ["--api-key", cli_key] if cli_key is not None else []
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                program,
                str(root),
                f"http://127.0.0.1:{worker.server_port}",
                *args,
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        worker.shutdown()
        worker.server_close()
        thread.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert requests == [("GET", expected), ("DELETE", expected)]
