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


@pytest.mark.skipif(sys.platform != "linux", reason="implicit runtime admission is Linux-only")
def test_managed_function_scope_reuses_parent_before_model_send(tmp_path: Path) -> None:
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
    environment.update(OPENAI_API_KEY="managed-scope-test-key", TMPDIR=str(tmp_path))
    program = r"""
import asyncio, importlib, json, os
from pathlib import Path
from agents import Agent, NativeProvider, function_tool
native = importlib.import_module("agents._native")
session = importlib.import_module("_agents_codex.session")
providers = []
class TrackedProvider(NativeProvider):
    def __init__(self):
        super().__init__()
        providers.append(self)
session.NativeProvider = TrackedProvider
open_conversation = native._open_conversation
thread_ids = []
class OpenedWithoutSend(Exception):
    pass
async def capture_scope(*args, **kwargs):
    assert kwargs["_process_private_runtime"] is True
    assert kwargs["_share_model_http"] is False
    assert kwargs["_private_http"] is False
    assert json.loads(args[2])["_sdk_websocket_tls"] is True
    args = list(args)
    config = json.loads(args[0])
    # This is a native lifetime witness, not the real WSS protocol witness.
    # Disable transport before Core startup and stop before the first submission.
    provider = config["model_providers.native_agent"]
    provider["supports_websockets"] = False
    provider["base_url"] = "http://127.0.0.1:1/v1"
    args[0] = json.dumps(config)
    options = json.loads(args[2])
    options["config"] = config
    args[2] = json.dumps(options)
    engine, thread, home = await open_conversation(*args, **kwargs)
    assert home is None, "a managed scope must retain the parent-owned home"
    thread_ids.append(thread.id)
    # The registered opening lease must join these real owners after this failure.
    raise OpenedWithoutSend
native._open_conversation = capture_scope
@function_tool
def lookup(value: str) -> str:
    "Return the supplied synthetic value."
    raise AssertionError("no model request or callback is allowed in this witness")
async def run():
    agent = Agent(instructions="Use lookup.", model="gpt-5.1-codex", tools=[lookup])
    try:
        await agent.run("probe")
    except OpenedWithoutSend:
        pass
    else:
        raise AssertionError("the real native scope was not intercepted")
asyncio.run(run())
homes = list(Path(os.environ["TMPDIR"]).glob("native-agent-*"))
assert len(homes) == 1, homes
asyncio.run(run())
assert len(thread_ids) == 2 and len(set(thread_ids)) == 2, thread_ids
assert list(Path(os.environ["TMPDIR"]).glob("native-agent-*")) == homes
assert len(providers) == 2 and all(provider._closed for provider in providers)
async def drain():
    await native._close_implicit_runtime()
asyncio.run(drain())
assert not homes[0].exists(), "the joined drain must retire the retained parent/home"
print(json.dumps({"threads":thread_ids, "home":str(homes[0])}), flush=True)
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
    assert len(set(report["threads"])) == 2
    assert not Path(report["home"]).exists()
