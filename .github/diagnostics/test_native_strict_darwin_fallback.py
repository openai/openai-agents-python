"""Opt-in hosted check of strict local Environment values and macOS system trust."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from agents import Agent, Environment, NativeProvider, NetworkPolicy
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    tool_output,
)
from tests.native_fixtures import (
    responses as responses,  # noqa: PLC0414
)

_HOST = "example.com"
_URL = f"https://{_HOST}/"
_CURL = ("/usr/bin/curl", "--disable")
_STOCK_PEM = "/etc/ssl/cert.pem"
_HTTPS_ARGS = (
    "--proto",
    "=https",
    "--silent",
    "--show-error",
    "--fail",
    "--connect-timeout",
    "4",
    "--max-time",
    "4",
    "--output",
    "/dev/null",
    "--write-out",
)


def _curl_checks(proxy: str) -> str:
    commands = []
    for mode, backend, ca in (
        ("NATIVE_TRUST", "secure-transport", ()),
        ("STOCK_PEM", "openssl", ("--cacert", _STOCK_PEM)),
    ):
        curl = shlex.join(("/usr/bin/env", "-i", f"CURL_SSL_BACKEND={backend}", *_CURL))
        receipt = f"{mode} connect=%{{http_connect}} http=%{{http_code}} verify=%{{ssl_verify_result}}"
        options = shlex.join((*_HTTPS_ARGS, receipt, *ca, "--noproxy", "", _URL))
        commands.extend(
            (
                f"printf '%s ' {mode}",
                f"{curl} --version | /usr/bin/head -n 1",
                "status=0",
                f"{curl} {options} --proxy {proxy} || status=$?",
                """printf ' exit=%s\n' "$status" """.rstrip(),
            )
        )
    return "\n".join(commands)


def _verified(output: str, mode: str) -> bool:
    lines = output.splitlines()
    # MultiSSL surrounds inactive backends with parentheses in the version line.
    versions = [line.split() for line in lines if line.startswith(f"{mode} curl ")]
    assert len(versions) == 1, output
    tokens = versions[0]
    active = (
        "SecureTransport" in tokens
        if mode == "NATIVE_TRUST"
        else any(token.startswith("LibreSSL/") for token in tokens)
    )
    assert active, output
    return f"{mode} connect=200 http=200 verify=0 exit=0" in lines


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
            'test -n "${HTTPS_PROXY:-}" || exit 15',
            'printf "%s" "$HTTPS_PROXY" > "$temp/proxy-url"',
            _curl_checks('"$HTTPS_PROXY"'),
        )
    )
    responses.respond(
        function_call(
            "exec_command",
            {"cmd": command, "login": False, "yield_time_ms": 20000},
            "fallback",
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
        assert "Process exited with code 0" in output, output
        [proxy_file] = scratch.glob("sdk-fallback.*/proxy-url")
        proxy = urlsplit(proxy_file.read_text())
        assert (
            proxy.scheme == "http"
            and proxy.hostname in {"127.0.0.1", "::1"}
            and proxy.port
            and not (proxy.username or proxy.password or proxy.query or proxy.fragment)
            and proxy.path in {"", "/"}
            and "@" not in proxy.netloc
        ), "expected the SDK's credential-free loopback HTTP proxy"
        host = await asyncio.to_thread(
            subprocess.run,
            ["/bin/sh", "-c", _curl_checks(shlex.quote(proxy.geturl()))],
            env={},
            capture_output=True,
            text=True,
            timeout=12,
            check=True,
        )

    assert outside.read_text() == "unchanged"
    assert "SDK_ENV_TEMP_OK" in output.splitlines(), output
    [marker] = scratch.glob("sdk-fallback.*/marker")
    assert marker.read_text() == "scratch-ok"
    print(f"Host through SDK CONNECT\n{host.stdout}{host.stderr}")
    print(f"Strict child through SDK CONNECT\n{output}")
    results = {
        "host_native": _verified(host.stdout, "NATIVE_TRUST"),
        "child_native": _verified(output, "NATIVE_TRUST"),
        "host_stock_pem": _verified(host.stdout, "STOCK_PEM"),
        "child_stock_pem": _verified(output, "STOCK_PEM"),
    }
    print(f"Verified TLS results: {results}")
    assert all(results.values()), results
