"""Probe audit-token SIGKILL after a known child execs the system /bin/sleep."""

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
SYSTEM_IMAGE = b"/bin/sleep"
CS_OPS_STATUS = 0
CS_PLATFORM_BINARY = 0x04000000
CS_SIGNED = 0x20000000


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


class Darwin:
    def __init__(self):
        proc = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.system = c.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.pidinfo = proc.proc_pidinfo
        self.pidinfo.argtypes = [c.c_int, c.c_int, U64, c.c_void_p, c.c_int]
        self.pidpath = proc.proc_pidpath
        self.pidpath.argtypes = [c.c_int, c.c_void_p, U32]
        self.csops = self.system.csops
        self.csops.argtypes = [c.c_int, U32, c.POINTER(U32), c.c_size_t]
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

    def image(self, pid):
        path = c.create_string_buffer(4096)
        c.set_errno(0)
        count = self.pidpath(pid, path, c.sizeof(path))
        check(count > 0, f"proc_pidpath({pid}) returned {count}, errno={c.get_errno()}")
        return path.value

    def signature_flags(self, pid):
        flags = U32()
        c.set_errno(0)
        rc = self.csops(pid, CS_OPS_STATUS, c.byref(flags), c.sizeof(flags))
        check(rc == 0, f"csops(CS_OPS_STATUS) returned {rc}, errno={c.get_errno()}")
        return flags.value

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


def smoke(stack, directory, domain):
    api = Darwin()
    label = f"org.openai.sdk.coalition-system-smoke.{uuid.uuid4().hex}"
    listener = stack.enter_context(socket.socket(socket.AF_UNIX))
    listener.bind(str(directory / "sock"))
    listener.listen(2)
    listener.settimeout(4)
    target_job = load_job(stack, directory, domain, label + ".target", "h")
    helper, helper_token = accept_peer(stack, listener, b"h", target_job)
    control_job = load_job(stack, directory, domain, label + ".control", "c")
    control, control_token = accept_peer(stack, listener, b"c", control_job)
    helper_pid, control_pid = helper_token[5], control_token[5]
    helper_id = api.identity(helper_pid)
    cid = api.member(helper_pid, helper_token[7])
    control_cid = api.member(control_pid, control_token[7])
    check(cid not in {control_cid, api.member(os.getpid())},
          "helper shares its resource coalition with the caller or control")

    helper.sendall(b"f")
    pid = struct.unpack("=I", read_exact(helper, 4))[0]
    check(pid > 1 and pid not in {helper_pid, control_pid, os.getpid()},
          "forked child PID overlaps a known process")
    deadline = time.monotonic() + 2
    while api.image(pid) != SYSTEM_IMAGE:
        check(time.monotonic() < deadline, "forked child did not exec /bin/sleep")
        time.sleep(0.01)

    identity = api.identity(pid)
    check(identity[1] == helper_id[0] and identity[3] == helper_id[2],
          "post-exec process does not match the known fork")
    check(os.getsid(pid) == os.getpgid(pid) == pid, "child did not call setsid")
    child_cid = api.member(pid, identity[2])
    check(child_cid == cid, "system image left the helper's resource coalition")
    flags = api.signature_flags(pid)
    check(flags & CS_PLATFORM_BINARY and flags & CS_SIGNED,
          f"/bin/sleep is missing signed platform flags ({flags:#x})")
    print(f"verified /bin/sleep, pid={pid}, pidversion={identity[2]}, "
          f"signing flags={flags:#x}, resource coalition={cid}, control={control_cid}")

    # No child socket or self-reported token. Ask the kernel for a read-only name port.
    token = api.task_name_token(pid)
    check_token(token, pid, identity[2])
    check(api.image(pid) == SYSTEM_IMAGE and api.identity(pid) == identity,
          "post-exec image or identity changed during token lookup")
    print("read-only task-name audit token matches the post-exec process")

    rc = api.send_signal(c.byref(TOKEN(*token)), signal.SIGKILL)
    check(rc == 0, f"proc_signal_with_audittoken(SIGKILL) returned {rc} ({os.strerror(rc)})")
    check(read_exact(helper, 1) == b"k", "waitpid did not report child SIGKILL")
    check(api.identity(pid, missing_ok=True) is None, "system child still exists")
    ping(helper)
    ping(control)
    check(api.member(control_pid, control_token[7]) == control_cid,
          "control's resource coalition changed")
    print("helper reaped /bin/sleep after SIGKILL; unrelated control still answers")


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
