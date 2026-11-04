"""Compare inherited Mach authority in the public R11 and focused R15 wrappers."""

import asyncio
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

import pytest

from agents import Agent, Environment, NativeProvider
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.native_fixtures import responses as responses


HERE = Path(__file__).resolve().parent


def launchctl(*args):
    return subprocess.run(
        ["/bin/launchctl", *map(str, args)], capture_output=True, text=True, timeout=5
    )


def remove_job(service):
    result = launchctl("bootout", service)
    if result.returncode:
        assert launchctl("print", service).returncode != 0, result.stderr


def make_job(workspace, domain, marker, *, start):
    label = f"org.openai.sdk.bootstrap-port-test.{uuid4().hex}"
    path = workspace / f"{label}.plist"
    spec = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/touch", str(marker)],
        "RunAtLoad": start,
        "KeepAlive": False,
        "ExitTimeOut": 2,
    }
    if domain.startswith("user/"):
        spec["LimitLoadToSessionType"] = "Background"
    path.write_bytes(plistlib.dumps(spec))
    return path, f"{domain}/{label}"


def job_runs(service):
    result = launchctl("print", service)
    assert result.returncode == 0, result.stderr
    match = re.search(r"^\s*runs = (\d+)", result.stdout, re.MULTILINE)
    assert match, result.stdout
    return int(match[1])


async def wait_marker(path, timeout):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.025)
    return path.exists()


async def observe(name, output, bootstrap_service, bootstrap_marker, idle_service, idle_marker):
    print(f"{name} payload:\n{output}", flush=True)
    await asyncio.sleep(0.75)
    result = {
        "bootstrap_registered": launchctl("print", bootstrap_service).returncode == 0,
        "bootstrap_marker": bootstrap_marker.exists(),
        "kickstart_marker": idle_marker.exists(),
        "runs": job_runs(idle_service),
    }
    print(f"{name} BEFORE TRUSTED CONTROL: {result}", flush=True)
    return result


def reset_attempt(bootstrap_service, bootstrap_marker, idle_marker, observation):
    if observation["bootstrap_registered"]:
        remove_job(bootstrap_service)
    for marker in (bootstrap_marker, idle_marker):
        marker.unlink(missing_ok=True)


def policy(*, network=""):
    # Use the real base and platform allowlist, plus a synthetic write root
    # and the final xpc-service-name deny from managed SDK policy assembly.
    parts = [
        (HERE / "seatbelt_base_policy.sbpl").read_text(),
        '(allow file-read* file-test-existence file-map-executable)',
        '(allow file-write* (subpath (param "WORKSPACE")))',
    ]
    if network:
        parts.extend([
            network,
            (HERE / "seatbelt_network_policy.sbpl").read_text(),
            '(allow mach-lookup (global-name "com.apple.TrustEvaluationAgent"))',
        ])
    parts.append((HERE / "seatbelt_read_only_platform_defaults.sbpl").read_text())
    parts.append('(deny mach-lookup (xpc-service-name-prefix ""))')
    return "\n".join(parts)


def run_wrapper(binary, workspace, profile, command, *, tty=False):
    return subprocess.run(
        [
            binary, "tty" if tty else "pipe", workspace, "/usr/bin/sandbox-exec",
            "-D", f"WORKSPACE={workspace}", "-p", profile, "/bin/sh", "-c", command,
        ],
        capture_output=True, text=True, timeout=20,
    )


def command_status(argv, name):
    return (
        f"rc=0; {shlex.join(list(map(str, argv)))} || rc=$?; "
        + f'printf "{name}=%s\\n" "$rc"; '
    )


def ordinary(probe, workspace, *, tty=False):
    args = [str(probe), "inspect", *(["tty"] if tty else [])]
    return (
        "set -eu; " + shlex.join(args) + "; "
        + shlex.join(["/usr/bin/git", "-C", str(workspace), "status", "--short"]) + "; "
        + 'test "$(/bin/cat tracked.txt)" = "fixture text"; '
        + ("test -t 0 && test -t 1 && /bin/stty -a; " if tty else "")
        + "printf 'shell-git-ok\\n'; "
    )


def rights(output):
    output = output.replace("\r\n", "\n")
    match = re.search(
        r"^after-exec bootstrap_rc=0 bootstrap=(present|absent) registered_rc=0 "
        r"registered=(\d+) host_launchctl_rc=(-?\d+) host_launchctl=(present|absent) "
        r"parent_rc=(-?\d+) parent=(present|absent)$", output, re.MULTILINE,
    )
    assert match, output
    return match.groups()


def check_cleared(output):
    bootstrap, registered, _, host, _, parent = rights(output)
    assert bootstrap == host == parent == "absent", output
    assert int(registered) == 0, output
    assert "A  tracked.txt" in output.splitlines(), output
    assert "shell-git-ok" in output.splitlines(), output


class LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"loopback-ok\n")

    def log_message(self, *_args):
        pass


def network_comparison(stock, fixed, workspace):
    with ThreadingHTTPServer(("127.0.0.1", 0), LoopbackHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            profile = policy(network=f'(allow network-outbound (remote ip "localhost:{port}"))')
            command = shlex.join([
                "/usr/bin/curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
                "--connect-timeout", "2", "--max-time", "4", f"http://127.0.0.1:{port}/",
            ])
            loopback = [run_wrapper(binary, workspace, profile, command) for binary in (stock, fixed)]
        finally:
            server.shutdown()
            thread.join(timeout=2)

    # No proxy or credentials. This separate profile matches the system DNS
    # and trust allowances and tests a public HTTPS endpoint with a HEAD.
    command = shlex.join([
        "/usr/bin/curl", "--noproxy", "*", "--head", "--fail", "--silent", "--show-error",
        "--connect-timeout", "3", "--max-time", "6", "--output", "/dev/null",
        "--write-out", "https-status=%{http_code}\\n", "https://github.com/",
    ])
    profile = policy(network="(allow network-outbound)\n(allow network-inbound)")
    https = [run_wrapper(binary, workspace, profile, command) for binary in (stock, fixed)]
    for name, pair in (("loopback", loopback), ("direct DNS/TLS HEAD", https)):
        for label, result in zip(("stock", "cleared"), pair, strict=True):
            print(f"{name} {label}: rc={result.returncode} "
                  f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}", flush=True)
        if pair[0].returncode:
            print(f"INCONCLUSIVE: stock {name} failed under the selected profile", flush=True)
        else:
            assert pair[1].returncode == 0, f"{name} regressed after clearing: {pair[1].stderr}"
    if loopback[0].returncode == 0:
        assert all("loopback-ok" in result.stdout for result in loopback)


@pytest.mark.asyncio
async def test_sdk_seatbelt_bootstrap_port(responses, request):
    assert platform.system() == "Darwin" and platform.machine() == "arm64"
    assert os.getuid() == os.geteuid() != 0
    kind = os.environ.get("SDK_SEATBELT_DOMAIN", "gui")
    assert kind in {"gui", "user"}
    domain = f"{kind}/{os.geteuid()}"
    stock = os.environ["SDK_WRAPPER_STOCK"]
    fixed = os.environ["SDK_WRAPPER_CLEARED"]
    # The SDK also allows temporary paths. Keep both synthetic siblings in
    # the checkout outside those paths, then grant only the workspace.
    scratch = tempfile.TemporaryDirectory(prefix=".sdk-bootstrap-port-", dir=Path.cwd())
    request.addfinalizer(scratch.cleanup)
    private = Path(scratch.name).resolve()
    workspace = private / "workspace"
    workspace.mkdir(mode=0o700)
    control_dir = private / "control"
    control_dir.mkdir(mode=0o700)
    probe = workspace / "bootstrap-probe"
    subprocess.run(
        ["/usr/bin/clang", "-std=c11", "-D_DARWIN_C_SOURCE", "-Wall", "-Wextra", "-Werror",
         HERE / "probe.c", "-o", probe],
        check=True, timeout=20,
    )
    subprocess.run(["/usr/bin/git", "init", "-q", workspace], check=True, timeout=5)
    (workspace / "tracked.txt").write_text("fixture text\n")
    subprocess.run(["/usr/bin/git", "-C", workspace, "add", "tracked.txt"], check=True, timeout=5)

    with contextlib.ExitStack() as cleanup:
        bootstrap_marker = control_dir / "bootstrapped"
        idle_marker = control_dir / "kickstarted"
        direct_marker = control_dir / "direct-write"
        plist, bootstrap_service = make_job(workspace, domain, bootstrap_marker, start=True)
        idle_plist, idle_service = make_job(workspace, domain, idle_marker, start=False)
        cleanup.callback(remove_job, idle_service)
        cleanup.callback(remove_job, bootstrap_service)
        result = launchctl("bootstrap", domain, idle_plist)
        assert result.returncode == 0, result.stderr
        initial = job_runs(idle_service)
        assert not any(path.exists() for path in (bootstrap_marker, idle_marker, direct_marker))
        print(f"synthetic kickstart target {idle_service}; out-of-root marker {idle_marker}", flush=True)

        command = (
            ordinary(probe, workspace)
            + command_status(["/usr/bin/touch", direct_marker], "direct_write_exit")
            + command_status([probe, "run-launchctl", "bootstrap", domain, plist], "bootstrap_exit")
            + command_status([probe, "run-launchctl", "kickstart", idle_service], "kickstart_exit")
        )
        prepared = policy()
        baseline = run_wrapper(stock, workspace, prepared, command)
        observed_stock = await observe(
            "R15 stock", baseline.stdout + baseline.stderr,
            bootstrap_service, bootstrap_marker, idle_service, idle_marker,
        )
        reset_attempt(bootstrap_service, bootstrap_marker, idle_marker, observed_stock)

        candidate = run_wrapper(fixed, workspace, prepared, command)
        observed_fixed = await observe(
            "R15 cleared", candidate.stdout + candidate.stderr,
            bootstrap_service, bootstrap_marker, idle_service, idle_marker,
        )
        reset_attempt(bootstrap_service, bootstrap_marker, idle_marker, observed_fixed)
        tty = run_wrapper(fixed, workspace, prepared, ordinary(probe, workspace, tty=True), tty=True)
        print(f"R15 cleared PTY rc={tty.returncode}:\n{tty.stdout}{tty.stderr}", flush=True)

        cleared = shlex.join(["exec", str(probe), "clear-all", "/bin/sh", "-c", command])
        terminal = shlex.join([
            "exec", str(probe), "clear-all", "/bin/sh", "-c", ordinary(probe, workspace, tty=True),
        ])
        responses.respond(function_call("exec_command", {"cmd": cleared, "login": False}, "clear"))
        responses.respond(function_call(
            "exec_command", {"cmd": terminal, "login": False, "tty": True}, "clear-tty"
        ))
        responses.respond(assistant_output("probe complete"))
        environment = Environment.local(cwd=workspace, writable_roots=[], tools=["exec_command"])
        agent = Agent(
            instructions="Use the selected tools.",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-model-key"),
        )
        async with asyncio.timeout(25), agent.session(environment=environment) as session:
            turn = await session.submit("Run the bootstrap-port qualification commands.")
            assert (await turn.get_result()).final_output == "probe complete"
        assert len(responses.requests) == 3
        shell_out = tool_output(responses.requests[1], "clear")
        tty_out = tool_output(responses.requests[2], "clear-tty")
        observed_r11 = await observe(
            "R11 cleared before exec", shell_out,
            bootstrap_service, bootstrap_marker, idle_service, idle_marker,
        )
        print(f"R11 cleared PTY:\n{tty_out}", flush=True)
        reset_attempt(bootstrap_service, bootstrap_marker, idle_marker, observed_r11)

        # Read the observations first, then exercise trusted controls against
        # these same labels and independently verify both protected markers.
        control_bootstrap = launchctl("bootstrap", domain, plist)
        control_kickstart = launchctl("kickstart", idle_service)
        trusted_bootstrap = await wait_marker(bootstrap_marker, 3)
        trusted_kickstart = await wait_marker(idle_marker, 3)
        final_runs = job_runs(idle_service)
        print(f"trusted controls: bootstrap_rc={control_bootstrap.returncode}, "
              f"bootstrap_marker={trusted_bootstrap}, kickstart_rc={control_kickstart.returncode}, "
              f"kickstart_marker={trusted_kickstart}, runs={final_runs}", flush=True)
        network_comparison(stock, fixed, workspace)

        assert control_bootstrap.returncode == 0 and trusted_bootstrap, control_bootstrap.stderr
        assert control_kickstart.returncode == 0 and trusted_kickstart, control_kickstart.stderr
        assert final_runs > observed_r11["runs"]
        assert baseline.returncode == candidate.returncode == tty.returncode == 0
        assert rights(baseline.stdout)[0] == "present"
        assert not direct_marker.exists(), "a payload wrote outside its allowed workspace"
        assert observed_stock["runs"] >= initial
        assert observed_fixed["runs"] == observed_r11["runs"] == observed_stock["runs"]
        for observation in (observed_fixed, observed_r11):
            assert not any(value for key, value in observation.items() if key != "runs"), observation
        for output in (candidate.stdout, tty.stdout):
            check_cleared(output)
        for output in (shell_out, tty_out):
            assert "clear bootstrap_rc=0 registered_rc=0" in output.splitlines(), output
            check_cleared(output)
        assert "controlling-tty-ok" in tty.stdout.splitlines()
        assert "controlling-tty-ok" in tty_out.splitlines()
        print("PASS: cleared payloads did not delegate; the trusted job and command controls passed")
