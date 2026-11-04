"""Verify the managed Darwin command transport and launchd port isolation."""

import contextlib
from http.server import BaseHTTPRequestHandler
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
from socketserver import ThreadingTCPServer
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

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


def wait_marker(path, timeout):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    return path.exists()


def observe(name, output, bootstrap_service, bootstrap_marker, idle_service, idle_marker):
    print(f"{name} payload:\n{output}", flush=True)
    time.sleep(0.75)
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


def run_wrapper(binary, workspace, profile, command, *, tty=False, inline=True):
    if inline:
        sandbox_args = ["-p", profile]
    else:
        # Keep the request small. The inline transport is measured separately.
        policy_file = workspace.parent / "control" / "seatbelt.sb"
        policy_file.write_text(profile)
        sandbox_args = ["-f", policy_file]
    return subprocess.run(
        [
            binary, "tty" if tty else "pipe", workspace, "/usr/bin/sandbox-exec",
            "-D", f"WORKSPACE={workspace}", *sandbox_args, "/bin/sh", "-c", command,
        ],
        capture_output=True, text=True, timeout=20,
    )


def command_status(argv, name):
    return (
        f"rc=0; {shlex.join(list(map(str, argv)))} || rc=$?; "
        + f'printf "{name}=%s\\n" "$rc"; '
    )


def ordinary(probe, workspace, git, *, tty=False):
    args = [probe, "inspect", *(["tty"] if tty else [])]
    return (
        "set -eu; " + shlex.join(list(map(str, args))) + "; "
        + 'test "$(/bin/cat tracked.txt)" = "fixture text"; printf "shell-ok\\n"; '
        + command_status([git, "-C", workspace, "status", "--short"], "git_exit")
        + 'PATH=/usr/bin:/bin; export PATH; printf "path_git=%s\\n" "$(command -v git)"; '
        + command_status(["git", "-C", workspace, "status", "--short"], "shim_git_exit")
        + ("test -t 0 && test -t 1 && /bin/stty -a; " if tty else "")
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
    assert "shell-ok" in output.splitlines(), output


def check_git(output):
    assert "A  tracked.txt" in output.splitlines(), output
    assert "git_exit=0" in output.splitlines(), output


def compare_transport(original, stock, fixed, workspace, profile):
    # The managed SDK passes -p in argv. A matched short/large pair measures
    # the runtime difference independently of whether the policy is permissive.
    short = "(version 1) (allow default)"
    command = "printf 'transport-ok\\n'"
    for label, binary, shape, text, inline in (
        ("original", original, "short inline", short, True),
        ("original", original, "large padded inline", short + "\n;" + "x" * len(profile), True),
        ("original", original, "actual profile inline", profile, True),
        ("original", original, "actual profile from file", profile, False),
        ("blocking", stock, "actual profile inline", profile, True),
        ("cleared", fixed, "actual profile inline", profile, True),
    ):
        result = run_wrapper(binary, workspace, text, command, inline=inline)
        print(f"R15 transport {label} {shape} ({len(text)} bytes): rc={result.returncode}, "
              f"stdout={result.stdout.strip()!r}, stderr={result.stderr.strip()!r}", flush=True)
        if label != "original":
            assert result.returncode == 0 and "transport-ok" in result.stdout, result


class LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(f"received-target={self.path}\n".encode())

    def log_message(self, *_args):
        pass


def compare_network(variants, workspace):
    curl = [
        "/usr/bin/curl", "--fail", "--silent", "--show-error",
        "--connect-timeout", "3", "--max-time", "6",
    ]

    def run_cases(name, network, argv, expected):
        results = {}
        for label, binary in variants:
            profile = policy(network=network)
            result = run_wrapper(binary, workspace, profile, shlex.join(argv))
            print(f"{name} {label}: rc={result.returncode} "
                  f"stdout={result.stdout.strip()!r} stderr={result.stderr.strip()!r}", flush=True)
            results[label] = result.returncode == 0 and expected in result.stdout
        return results

    with ThreadingTCPServer(("127.0.0.1", 0), LoopbackHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}"
            restriction = f'(allow network-outbound (remote ip "localhost:{server.server_address[1]}"))'
            loopback = run_cases("loopback", restriction,
                                 curl + ["--noproxy", "*", url + "/"], "received-target=/")
            target = "http://sdk-proxy-qualification.invalid/probe"
            proxy = run_cases(
                "managed HTTP proxy", restriction,
                ["/usr/bin/env", f"http_proxy={url}", *curl, "--noproxy", "", target],
                f"received-target={target}",
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)

    host = "github.com"
    head = curl + [
        "--noproxy", "*", "--head", "--output", "/dev/null",
        "--write-out", "https-status=%{http_code}\n",
    ]
    network = "(allow network-outbound)\n(allow network-inbound)"
    direct = run_cases("direct DNS/TLS HEAD", network, [*head, f"https://{host}/"], "https-status=200")
    return loopback, proxy, direct


def test_sdk_seatbelt_bootstrap_port(request):
    assert platform.system() == "Darwin" and platform.machine() == "arm64"
    assert os.getuid() == os.geteuid() != 0
    kind = os.environ.get("SDK_SEATBELT_DOMAIN", "gui")
    assert kind in {"gui", "user"}
    domain = f"{kind}/{os.geteuid()}"
    stock = os.environ["SDK_WRAPPER_STOCK"]
    fixed = os.environ["SDK_WRAPPER_CLEARED"]
    original = str(Path(stock).with_name("bootstrap-original"))
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
    git = subprocess.check_output(["/usr/bin/xcrun", "--find", "git"], text=True, timeout=5).strip()
    assert Path(git).is_file()
    subprocess.run(["/usr/bin/git", "init", "-q", workspace], check=True, timeout=5)
    (workspace / "tracked.txt").write_text("fixture text\n")
    subprocess.run(["/usr/bin/git", "-C", workspace, "add", "tracked.txt"], check=True, timeout=5)

    with contextlib.ExitStack() as cleanup:
        bootstrap_marker = control_dir / "bootstrapped"
        idle_marker = control_dir / "kickstarted"
        direct_marker = control_dir / "direct-write"
        cleared_marker = control_dir / "cleared-kickstarted"
        trusted_marker = control_dir / "trusted-kickstarted"
        plist, bootstrap_service = make_job(workspace, domain, bootstrap_marker, start=True)
        idle_plist, idle_service = make_job(workspace, domain, idle_marker, start=False)
        cleared_plist, cleared_service = make_job(workspace, domain, cleared_marker, start=False)
        trusted_plist, trusted_service = make_job(workspace, domain, trusted_marker, start=False)
        cleanup.callback(remove_job, trusted_service)
        cleanup.callback(remove_job, cleared_service)
        cleanup.callback(remove_job, idle_service)
        cleanup.callback(remove_job, bootstrap_service)
        for idle, service in (
            (idle_plist, idle_service),
            (cleared_plist, cleared_service),
            (trusted_plist, trusted_service),
        ):
            result = launchctl("bootstrap", domain, idle)
            assert result.returncode == 0, result.stderr
            assert job_runs(service) == 0
        initial = job_runs(idle_service)
        assert not any(path.exists() for path in (bootstrap_marker, idle_marker, direct_marker))
        print(f"synthetic kickstart target {idle_service}; out-of-root marker {idle_marker}", flush=True)

        command = (
            command_status(["/usr/bin/touch", direct_marker], "direct_write_exit")
            + command_status([probe, "run-launchctl", "bootstrap", domain, plist], "bootstrap_exit")
            + command_status([probe, "run-launchctl", "kickstart", idle_service], "kickstart_exit")
            + ordinary(probe, workspace, git)
        )
        prepared = policy()
        baseline = run_wrapper(stock, workspace, prepared, command)
        observed_stock = observe(
            "R15 blocking worker", baseline.stdout + baseline.stderr,
            bootstrap_service, bootstrap_marker, idle_service, idle_marker,
        )
        reset_attempt(bootstrap_service, bootstrap_marker, idle_marker, observed_stock)

        candidate = run_wrapper(
            fixed, workspace, prepared, command.replace(idle_service, cleared_service)
        )
        observed_fixed = observe(
            "R15 cleared", candidate.stdout + candidate.stderr,
            bootstrap_service, bootstrap_marker, cleared_service, cleared_marker,
        )
        reset_attempt(bootstrap_service, bootstrap_marker, cleared_marker, observed_fixed)

        tty_command = ordinary(probe, workspace, git, tty=True)
        tty = run_wrapper(fixed, workspace, prepared, tty_command, tty=True)
        print(f"R15 cleared PTY rc={tty.returncode}:\n{tty.stdout}{tty.stderr}", flush=True)

        # Use a separate trusted job so launchd throttling cannot mask a result.
        control_bootstrap = launchctl("bootstrap", domain, plist)
        control_kickstart = launchctl("kickstart", trusted_service)
        trusted_bootstrap = wait_marker(bootstrap_marker, 3)
        trusted_kickstart = wait_marker(trusted_marker, 3)
        final_runs = job_runs(trusted_service)
        print(f"trusted controls: bootstrap_rc={control_bootstrap.returncode}, "
              f"bootstrap_marker={trusted_bootstrap}, kickstart_rc={control_kickstart.returncode}, "
              f"kickstart_marker={trusted_kickstart}, runs={final_runs}", flush=True)
        compare_transport(original, stock, fixed, workspace, prepared)
        loopback, proxy, direct = compare_network(
            [("stock", stock), ("cleared", fixed)],
            workspace,
        )
        print(f"network comparison: {loopback=} {proxy=} {direct=}", flush=True)

        assert control_bootstrap.returncode == 0 and trusted_bootstrap, control_bootstrap.stderr
        assert control_kickstart.returncode == 0 and trusted_kickstart, control_kickstart.stderr
        assert final_runs == 1
        assert baseline.returncode == candidate.returncode == tty.returncode == 0
        assert rights(baseline.stdout)[0] == "present"
        assert not direct_marker.exists(), "a payload wrote outside its allowed workspace"
        assert observed_stock["runs"] == initial + 1 and observed_stock["kickstart_marker"]
        assert not any(observed_fixed.values()), observed_fixed
        assert "kickstart_exit=0" in baseline.stdout.splitlines(), baseline.stdout
        for command in ("bootstrap", "kickstart"):
            assert re.search(rf"^{command}_exit=[1-9]\d*$", candidate.stdout, re.MULTILINE)
        for output in (candidate.stdout, tty.stdout):
            check_cleared(output)
        assert "controlling-tty-ok" in tty.stdout.splitlines()
        for output in (baseline.stdout, candidate.stdout, tty.stdout):
            check_git(output)
            assert "path_git=/usr/bin/git" in output.splitlines(), output
            assert "shim_git_exit=0" in output.splitlines(), output
        for output in (baseline.stdout, candidate.stdout):
            assert re.search(r"^direct_write_exit=[1-9]\d*$", output, re.MULTILINE), output
        assert all(loopback.values()) and all(proxy.values()) and all(direct.values())
        print("PASS: the cleared payload preserved ordinary commands and could not delegate")
