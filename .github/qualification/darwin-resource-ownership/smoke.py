"""Run on a disposable Apple Silicon macOS runner as an ordinary user."""

import argparse
import contextlib
import ctypes as c
import errno
import os
from pathlib import Path
import platform
import plistlib
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid


U32 = c.c_uint32
U64 = c.c_uint64
TOKEN = U32 * 8
PROC_PIDUNIQIDENTIFIERINFO = 17
PROC_PIDCOALITIONINFO = 20
TASK_AUDIT_TOKEN = 15
SOL_LOCAL = 0
LOCAL_PEERTOKEN = 6


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


class Darwin:
    def __init__(self):
        proc = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.system = c.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.pidinfo = proc.proc_pidinfo
        self.pidinfo.argtypes = [c.c_int, c.c_int, U64, c.c_void_p, c.c_int]
        self.usage = self.system.coalition_info_resource_usage
        self.usage.argtypes = [U64, c.c_void_p, c.c_size_t]
        self.send_signal = proc.proc_signal_with_audittoken
        self.send_signal.argtypes = [c.POINTER(TOKEN), c.c_int]

    def read_pid(self, pid, flavor, size, missing_ok=False):
        data = c.create_string_buffer(size)
        c.set_errno(0)
        count = self.pidinfo(pid, flavor, 0, data, size)
        error = c.get_errno()
        if count == 0 and error == errno.ESRCH and missing_ok:
            return None
        check(count == size, f"proc_pidinfo({pid}, {flavor}) = {count}, errno={error}")
        return data.raw

    def identity(self, pid, missing_ok=False):
        data = self.read_pid(pid, PROC_PIDUNIQIDENTIFIERINFO, 56, missing_ok)
        # Unique ID, parent's unique ID, PID version, original parent's PID version.
        return struct.unpack("=16xQQII16x", data) if data is not None else None

    def member(self, pid, version=None):
        before = self.identity(pid)
        if version is not None:
            check(before[2] == version, f"PID version mismatch for {pid}")
        data = self.read_pid(pid, PROC_PIDCOALITIONINFO, 40)
        after = self.identity(pid)
        check(before == after, f"identity changed during coalition lookup for {pid}")
        cid = struct.unpack_from("=Q", data)[0]
        check(cid != 0, f"no resource coalition for {pid}")
        return cid

    def counters(self, cid, expected=None):
        # XNU copies at most the requested size. These are the first two fields.
        counts = (U64 * 2)()
        deadline = time.monotonic() + 2
        while True:
            c.set_errno(0)
            rc = self.usage(cid, counts, c.sizeof(counts))
            check(rc == 0, f"coalition_info_resource_usage({cid}) = {rc}, errno={c.get_errno()}")
            # Task teardown may finish just after the BSD process disappears.
            if (expected is None or counts[0] != expected[0] or counts[1] >= expected[1]
                    or time.monotonic() >= deadline):
                return tuple(counts)
            time.sleep(0.025)

    def task_name_token(self, pid):
        name_for_pid = self.system.task_name_for_pid
        name_for_pid.argtypes = [U32, c.c_int, c.POINTER(U32)]
        task_info = self.system.task_info
        task_info.argtypes = [U32, c.c_int, c.POINTER(TOKEN), c.POINTER(U32)]
        deallocate = self.system.mach_port_deallocate
        deallocate.argtypes = [U32, U32]
        own_port = U32.in_dll(self.system, "mach_task_self_").value
        port = U32()
        rc = name_for_pid(own_port, pid, c.byref(port))
        if rc != 0 or port.value == 0:
            raise OSError(f"task_name_for_pid({pid}) returned {rc}, port={port.value}")
        token = TOKEN()
        count = U32(len(token))
        try:
            rc = task_info(port, TASK_AUDIT_TOKEN, c.byref(token), c.byref(count))
            if rc != 0 or count.value != len(token):
                raise OSError(f"task_info(TASK_AUDIT_TOKEN) returned {rc}, count={count.value}")
            return tuple(token)
        finally:
            deallocate(own_port, port)


def launchctl(*args):
    return subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True, timeout=6
    )


def load_job(stack, directory, domain, label, role):
    service = f"{domain}/{label}"
    plist = directory / f"{role}.plist"
    spec = {
        "Label": label,
        "ProgramArguments": [str(directory / "helper"), str(directory / "sock"), role],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ExitTimeOut": 2,
        "StandardErrorPath": str(directory / f"{role}.stderr"),
    }
    if domain.startswith("user/"):
        spec["LimitLoadToSessionType"] = "Background"
    plist.write_bytes(plistlib.dumps(spec))
    loaded = False

    def bootout():
        result = launchctl("bootout", service)
        if result.returncode != 0:
            message = f"bootout {service}: {result.stderr.strip()}"
            check(not loaded, message)
            print(message, file=sys.stderr)

    # Register cleanup before bootstrap, which can fail after launchd receives it.
    stack.callback(bootout)
    print(f"bootstrap {service}", flush=True)
    result = launchctl("bootstrap", domain, str(plist))
    loaded = result.returncode == 0
    check(loaded, f"bootstrap {service}: {result.stderr.strip()}")
    return service


def read_exact(peer, size):
    data = b""
    while len(data) < size:
        chunk = peer.recv(size - len(data))
        check(chunk, "helper closed its socket early")
        data += chunk
    return data


def peer_token(peer):
    return struct.unpack("=8I", peer.getsockopt(SOL_LOCAL, LOCAL_PEERTOKEN, 32))


def check_token(token, pid, version=None):
    # The audit token binds the real and effective UID, PID, and PID version.
    check(token[1] == token[3] == os.getuid(), "peer UID does not match caller")
    check(pid > 1 and token[5] == pid, "audit token PID mismatch")
    check(version is None or token[7] == version, "audit token PID version mismatch")


def accept_peer(stack, listener, role, service=None):
    peer, _ = listener.accept()
    stack.enter_context(peer)
    peer.settimeout(3)
    check(read_exact(peer, 1) == role, f"unexpected helper role (expected {role!r})")
    token = peer_token(peer)
    check_token(token, token[5])
    if service is not None:
        result = launchctl("print", service)
        pids = re.findall(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        check(result.returncode == 0 and pids == [str(token[5])],
              f"peer is not the running process for {service}")
    return peer, token


def ping(peer):
    peer.sendall(b"p")
    check(read_exact(peer, 1) == b"p", "helper did not echo the ping")


def inspect_token(api, pid, version, role, peer=None):
    try:
        token = api.task_name_token(pid)
    except (OSError, AttributeError, ValueError) as exc:
        print(f"{role} read-only task name token unavailable: {exc}", flush=True)
        return None
    check_token(token, pid, version)
    check(peer is None or token == peer, f"{role} task name and peer tokens differ")
    print(f"{role} read-only task name token matched PID version {version}", flush=True)
    return token


def smoke(stack, directory, domain):
    api = Darwin()
    label = f"org.openai.sdk.coalition-smoke.{uuid.uuid4().hex}"
    listener = stack.enter_context(socket.socket(socket.AF_UNIX))
    listener.bind(str(directory / "sock"))
    listener.listen(3)
    listener.settimeout(4)
    job = load_job(stack, directory, domain, label + ".target", "h")
    helper, helper_token = accept_peer(stack, listener, b"h", job)
    job = load_job(stack, directory, domain, label + ".control", "c")
    control, control_token = accept_peer(stack, listener, b"c", job)
    helper_pid, control_pid = helper_token[5], control_token[5]
    check(helper_pid != control_pid, "helper and control must be separate processes")

    cid = api.member(helper_pid, helper_token[7])
    control_cid = api.member(control_pid, control_token[7])
    caller_cid = api.member(os.getpid())
    print(f"resource coalitions: caller={caller_cid} helper={cid} control={control_cid}")
    baseline = api.counters(cid)

    helper.sendall(b"f")
    parent_pid = struct.unpack("=I", read_exact(helper, 4))[0]
    child, token = accept_peer(stack, listener, b"d")
    child_pid = token[5]
    check(len({helper_pid, control_pid, parent_pid, child_pid, os.getpid()}) == 5,
          "overlapping process roles")
    parent_id = api.identity(parent_pid)
    child_id = api.identity(child_pid)
    check(parent_id[1] == api.identity(helper_pid)[0]
          and child_id[1] == parent_id[0] and child_id[3] == parent_id[2],
          "kernel ancestry does not match the known forks")
    check(os.getsid(child_pid) == os.getpgid(child_pid) == child_pid,
          "child did not create a separate session and process group")
    parent_cid = api.member(parent_pid, parent_id[2])
    child_cid = api.member(child_pid, token[7])
    print(f"resource coalitions: tracked parent={parent_cid} detached child={child_cid}")
    running = api.counters(cid)
    parent_token = inspect_token(api, parent_pid, parent_id[2], "noncooperating parent")

    helper.sendall(b"e")
    check(read_exact(helper, 1) == b"r", "helper did not reap the tracked parent")
    check(api.identity(parent_pid, missing_ok=True) is None, "tracked parent still exists")
    ping(child)
    orphan_cid = api.member(child_pid, token[7])
    orphan = api.counters(cid, expected=(3, 1))
    print(f"resource coalition after parent exit: detached child={orphan_cid}")
    child_name_token = inspect_token(api, child_pid, token[7], "orphan", token)

    # Keep the verified socket open. Pass the kernel's token, never a numeric PID.
    rc = api.send_signal(c.byref(TOKEN(*(child_name_token or token))), signal.SIGKILL)
    check(rc == 0, f"proc_signal_with_audittoken(SIGKILL) returned {rc} ({os.strerror(rc)})")
    check(child.recv(1) == b"", "detached child socket did not close after SIGKILL")
    deadline = time.monotonic() + 3
    while api.identity(child_pid, missing_ok=True) is not None:
        check(time.monotonic() < deadline, "detached child PID is still present")
        time.sleep(0.025)
    ping(control)
    ping(helper)
    check(api.member(control_pid, control_token[7]) == control_cid,
          "unrelated control membership changed")
    after = api.counters(cid, expected=(3, 2))
    print("audit-token SIGKILL passed, detached child gone and control survived")

    # Exact counts detect other tasks even if neither comparison PID shares the coalition.
    snapshots = [baseline, running, orphan, after]
    print(f"resource counters (tasks_started, tasks_exited): {snapshots}")
    dedicated = (cid == parent_cid == child_cid == orphan_cid
                 and cid not in {caller_cid, control_cid}
                 and snapshots == [(1, 0), (3, 0), (3, 1), (3, 2)])
    print(f"dedicated resource coalition observed: {dedicated}")
    check(dedicated, "dedicated resource coalition criteria failed")
    check(parent_token is not None and child_name_token is not None,
          "read-only identity route was unavailable")


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain", choices=("gui", "user"))
    args = parser.parse_args()
    check(platform.system() == "Darwin" and platform.machine() == "arm64",
          "run on native Apple Silicon macOS")
    check(os.getuid() == os.geteuid() != 0, "run as an ordinary user")
    print(f"macOS {platform.mac_ver()[0]}, Darwin {platform.release()}, {platform.machine()}")
    signal.signal(signal.SIGTERM, interrupt)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="sdk-coalition-", dir="/tmp") as tmp:
        directory = Path(tmp)
        source = Path(__file__).resolve().with_name("helper.c")
        subprocess.run(
            ["/usr/bin/clang", "-std=c11", "-D_DARWIN_C_SOURCE",
             "-Wall", "-Wextra", "-Werror", str(source), "-o", str(directory / "helper")],
            check=True, timeout=30,
        )
        try:
            with contextlib.ExitStack() as stack:
                smoke(stack, directory, f"{args.domain}/{os.getuid()}")
        except Exception:
            for path in sorted(directory.glob("*.stderr")):
                if path.stat().st_size:
                    print(f"{path.name}: {path.read_text(errors='replace')}", file=sys.stderr)
            raise
    print("PASS")


if __name__ == "__main__":
    try:
        main()
    except (AttributeError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
