"""Compare the same public DER inputs using default macOS trust on host and child."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)

from agents import Agent, Environment, NativeProvider, NetworkPolicy

_HOSTS = ("api.github.com", "www.apple.com")
_PEM = re.compile(rb"-----BEGIN CERTIFICATE-----\s+.*?-----END CERTIFICATE-----", re.DOTALL)
_VERDICT = re.compile(r"TRUST verdict=(0|1) domain=([A-Za-z0-9_.-]{1,127}) code=(-?[0-9]+)")


def _capture_public_chain(hostname: str) -> list[bytes]:
    # This handshake only collects public peer certificate bytes. The offline
    # host SecTrust result below determines whether the fixture is trusted.
    result = subprocess.run(
        [
            "/usr/bin/openssl",
            "s_client",
            "-connect",
            f"{hostname}:443",
            "-servername",
            hostname,
            "-showcerts",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
        timeout=8,
    )
    return [ssl.PEM_cert_to_DER_cert(pem.decode("ascii")) for pem in _PEM.findall(result.stdout)]


def _read_verdict(output: str, expected_inputs: list[str]) -> tuple[bool, str, int]:
    lines = output.splitlines()
    assert [line for line in lines if line.startswith(("DER ", "INPUT "))] == expected_inputs
    [verdict] = [line for line in lines if line.startswith("TRUST ")]
    match = _VERDICT.fullmatch(verdict)
    assert match is not None, verdict
    return match[1] == "1", match[2], int(match[3])


@pytest.mark.requires_native_macos_sandbox
async def test_trusted_darwin_offline_sectrust(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sys.platform == "darwin"
    workspace = (await asyncio.to_thread(tmp_path.resolve)) / "workspace"
    workspace.mkdir()
    outside = workspace.parent / "outside-marker"
    outside.write_text("unchanged")
    helper = await asyncio.to_thread(Path(os.environ["SDK_OFFLINE_SECTRUST_HELPER"]).resolve)
    codex_home = workspace.parent / "isolated-codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    verify_time = str(int(time.time()))
    for hostname in _HOSTS:
        try:
            chain = await asyncio.to_thread(_capture_public_chain, hostname)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"FIXTURE hostname={hostname} capture={type(error).__name__}", flush=True)
            continue
        print(f"FIXTURE hostname={hostname} certificates={len(chain)}", flush=True)
        if not 2 <= len(chain) <= 8:
            continue
        expected_inputs = []
        files = []
        for index, certificate in enumerate(chain):
            der_file = workspace / f"cert-{index}.der"
            der_file.write_bytes(certificate)
            expected_inputs.append(f"DER {index} {hashlib.sha256(certificate).hexdigest()}")
            files.append(str(der_file))
        expected_inputs.append(f"INPUT hostname={hostname} unix_time={verify_time} fetch=0")
        argv = [str(helper), hostname, verify_time, *files]
        host = await asyncio.to_thread(
            subprocess.run, argv, env={}, capture_output=True, text=True, timeout=12, check=True
        )
        print(f"HOST SecTrust\n{host.stdout}", flush=True)
        host_verdict = _read_verdict(host.stdout, expected_inputs)
        if host_verdict == (True, "none", 0):
            break
    else:
        pytest.fail("Neither public-chain fixture passed stock offline host SecTrust")

    command = "\n".join(
        (
            "set -eu",
            f"if (printf unexpected > {shlex.quote(str(outside))}) 2>/dev/null; then exit 14; fi",
            f"exec /usr/bin/env -i {shlex.join(argv)}",
        )
    )
    responses.respond(
        function_call(
            "exec_command",
            {"cmd": command, "login": False, "yield_time_ms": 20000},
            "offline-sectrust",
        )
    )
    responses.respond(assistant_output("done"))
    environment = Environment.local(
        cwd=workspace,
        tools=["exec_command"],
        write_policy="trusted_workspace_write",
        network=NetworkPolicy(allowed_domains=[hostname]),
    )
    agent = Agent(
        instructions="Run the selected command.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    async with timeout(45), agent.session(environment=environment) as session:
        turn = await session.submit("Run the offline macOS trust check.")
        assert (await turn.get_result()).final_output == "done"

    output = tool_output(responses.requests[-1], "offline-sectrust")
    print(f"MANAGED TRUSTED CHILD SecTrust\n{output}", flush=True)
    assert "Process exited with code 0" in output, output
    assert outside.read_text() == "unchanged"
    child_verdict = _read_verdict(output, expected_inputs)
    print(f"OFFLINE_SECTRUST host={host_verdict} child={child_verdict}", flush=True)
    assert host_verdict == child_verdict == (True, "none", 0)
