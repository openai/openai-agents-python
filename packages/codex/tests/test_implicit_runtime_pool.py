"""Check real implicit setup reuse and orderly process cleanup without a model send."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform != "linux", reason="implicit process runtime admission is Linux-only"
)
@pytest.mark.parametrize("mode", ["short", "fork", "idle"])
def test_implicit_runtime_pool_short_process_exit(tmp_path: Path, mode: str) -> None:
    native = importlib.import_module("agents._native")
    environment = dict(os.environ)
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "REQUEST_METHOD",
        "CODEX_SANDBOX",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    ):
        environment.pop(name, None)
    environment.update(OPENAI_API_KEY="runtime-pool-test-key", TMPDIR=str(tmp_path), POOL_MODE=mode)
    program = r"""
import asyncio, importlib, json, os, time
from pathlib import Path
from agents import Agent, NativeProvider
native = importlib.import_module("agents._native")
session = importlib.import_module("_agents_codex.session")
providers = []
class TrackedProvider(NativeProvider):
    def __init__(self):
        super().__init__()
        providers.append(self)
session.NativeProvider = TrackedProvider
prepare = native._prepare_run_conversation
calls = 0
def malformed(*args, **kwargs):
    global calls
    assert kwargs.get("_process_private_http") is True
    calls += 1
    args = list(args)
    # NativeRun parses this only after opening its real managed scope,
    # and before submit_replaceable: this cannot send a model request.
    args[5] = "{"
    return prepare(*args, **kwargs)
native._prepare_run_conversation = malformed
async def run():
    try:
        await Agent(instructions="Reply plainly.", model="gpt-5.1-codex").run("probe")
    except Exception as error:
        assert "EOF while parsing an object" in str(error), str(error)
    else:
        raise AssertionError("malformed native input unexpectedly completed")
asyncio.run(run())
homes = list(Path(os.environ["TMPDIR"]).glob("native-agent-*"))
assert len(homes) == 1, homes
home = homes[0]
if os.environ["POOL_MODE"] == "short":
    # A new closed asyncio loop must retain neither callbacks nor old loop state.
    asyncio.run(run())
    assert list(Path(os.environ["TMPDIR"]).glob("native-agent-*")) == homes
    assert calls == 2
elif os.environ["POOL_MODE"] == "idle":
    deadline = time.monotonic() + 12
    while home.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not home.exists(), "idle retirement must clean the generated home"
else:
    child = os.fork()
    if child == 0:
        try:
            native._close_implicit_runtime()
        except RuntimeError as error:
            assert "another process" in str(error)
        else:
            os._exit(2)
        native._close_implicit_runtime_at_exit()
        os._exit(0 if home.exists() else 3)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 0, status
    assert home.exists(), "fork child must leave the parent's home untouched"
assert all(provider.api_key is None and provider._closed for provider in providers)
if os.environ["POOL_MODE"] != "idle":
    assert home.exists(), "parent cleanup must still belong to orderly exit"
print(json.dumps({"home":str(home), "native":native.__file__}), flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["native"] == native.__file__
    assert not Path(report["home"]).exists(), "orderly exit must retire the idle generated home"
