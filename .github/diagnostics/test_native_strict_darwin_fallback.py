"""Opt-in hosted check of strict local Environment values and macOS system trust."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agents import Agent, Environment, NativeProvider, NetworkPolicy
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)

_HOST = "example.com"
_URL = f"https://{_HOST}/"
_CURL = ("/usr/bin/curl", "--disable")
_CURL_BACKEND = "secure-transport"
_CURL_ENV = {"CURL_SSL_BACKEND": _CURL_BACKEND}
_HTTPS_ARGS = (
    "--proto",
    "=https",
    "--silent",
    "--show-error",
    "--fail",
    "--connect-timeout",
    "8",
    "--max-time",
    "15",
    "--output",
    "/dev/null",
    "--write-out",
    "STOCK_TRUST http=%{http_code} verify=%{ssl_verify_result}\n",
)
_SUCCESS = re.compile(r"^STOCK_TRUST http=2[0-9]{2} verify=0$", re.MULTILINE)


@pytest.mark.requires_native_macos_sandbox
@pytest.mark.skipif(sys.platform != "darwin", reason="requires a stock macOS runner")
@pytest.mark.skipif(
    os.environ.get("OPENAI_AGENTS_DARWIN_FALLBACK_PROBE") != "1",
    reason="requires explicit opt-in to the credential-free HTTPS control",
)
async def test_strict_darwin_local_environment_temp_and_system_trust(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = await asyncio.to_thread(tmp_path.resolve)
    workspace, scratch = (tmp_path / name for name in ("workspace", "selected-scratch"))
    workspace.mkdir()
    scratch.mkdir()
    outside = tmp_path / "outside-marker"
    outside.write_text("unchanged")
    monkeypatch.setenv("SDK_DARWIN_FALLBACK_AMBIENT", "parent-only")

    version = await asyncio.to_thread(
        subprocess.run,
        [*_CURL, "--version"],
        env=_CURL_ENV,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    host_version = version.stdout.splitlines()[0]
    # MultiSSL surrounds inactive backends with parentheses in the version line.
    assert "SecureTransport" in host_version.split(), host_version
    host = await asyncio.to_thread(
        subprocess.run,
        [*_CURL, *_HTTPS_ARGS, "--proxy", "", _URL],
        env=_CURL_ENV,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    print(f"host curl: {host_version}\nhost trust: {host.stdout.strip()}")
    assert host.returncode == 0 and _SUCCESS.search(host.stdout), (host.stdout, host.stderr)

    clean_curl = shlex.join(("/usr/bin/env", "-i", f"CURL_SSL_BACKEND={_CURL_BACKEND}", *_CURL))
    command = "\n".join(
        (
            "set -eu",
            'test "${SDK_DARWIN_FALLBACK:-}" = explicit-value || exit 11',
            'test -z "${SDK_DARWIN_FALLBACK_AMBIENT+x}" || exit 12',
            f'test "${{TMPDIR:-}}" = {shlex.quote(str(scratch))} || exit 13',
            'temp=$(/usr/bin/mktemp -d "$TMPDIR/sdk-fallback.XXXXXXXX")',
            'printf scratch-ok > "$temp/marker"',
            f"if (printf unexpected > {shlex.quote(str(outside))}) 2>/dev/null; then exit 14; fi",
            "printf 'SDK_ENV_TEMP_OK\\n'",
            f"{clean_curl} --version",
            'test -n "${HTTPS_PROXY:-}" || exit 15',
            f'{clean_curl} {shlex.join(_HTTPS_ARGS)} --proxy "$HTTPS_PROXY" '
            f"--noproxy '' {shlex.quote(_URL)}",
        )
    )
    responses.respond(
        function_call(
            "exec_command", {"cmd": command, "login": False, "yield_time_ms": 20000}, "fallback"
        )
    )
    responses.respond(assistant_output("done"))
    environment = Environment.local(
        cwd=workspace,
        writable_roots=[scratch],
        env={"SDK_DARWIN_FALLBACK": "explicit-value", "TMPDIR": str(scratch)},
        tools=["exec_command"],
        write_policy="strict",
        network=NetworkPolicy(allowed_domains=[_HOST]),
    )
    agent = Agent(
        instructions="Run the selected command.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
    )
    async with timeout(45), agent.session(environment=environment) as session:
        turn = await session.submit("Run the strict local fallback check.")
        assert (await turn.get_result()).final_output == "done"

    output = tool_output(responses.requests[-1], "fallback")
    assert outside.read_text() == "unchanged"
    assert "SDK_ENV_TEMP_OK" in output.splitlines(), output
    [marker] = scratch.glob("sdk-fallback.*/marker")
    assert marker.read_text() == "scratch-ok"
    assert host_version in output.splitlines(), output
    assert "Process exited with code 0" in output and _SUCCESS.search(output), output
    print(output)
