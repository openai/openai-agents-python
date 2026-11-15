"""Synthetic-only enforcement probes inside the confined fixture container."""

import ctypes
import errno
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

if platform.machine() != "x86_64":
    raise RuntimeError("These syscall probes require x86_64")
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
checks = []
if len(sys.argv) != 2 or re.fullmatch(r"agents-native-ci-[0-9]+-[0-9]+", sys.argv[1]) is None:
    raise RuntimeError("The expected unique AppArmor name is required")
current = Path("/proc/self/attr/current").read_text().strip()
if current != sys.argv[1] + " (enforce)":
    raise RuntimeError("The probe's AppArmor profile is not enforcing")
status = Path("/proc/self/status").read_text()
if re.search(r"^Seccomp:\s+2$", status, re.MULTILINE) is None:
    raise RuntimeError("The probe is not running with seccomp filtering")
capabilities = {
    name: re.search(r"^" + name + r":\s+([0-9a-fA-F]+)$", status, re.MULTILINE).group(1)
    for name in ("CapEff", "CapBnd")
}
if any(int(value, 16) & (1 << 21) for value in capabilities.values()):
    raise RuntimeError("The probe must not hold or regain CAP_SYS_ADMIN")
outer = {
    "apparmor": current,
    "seccomp": 2,
    **capabilities,
    "namespaces": {
        name: os.readlink("/proc/self/ns/" + name)
        for name in ("user", "mnt", "pid", "net", "uts", "ipc", "cgroup")
    },
}


def check(name, operation):
    try:
        details = operation()
        checks.append({"name": name, "passed": True, **(details or {})})
    except Exception as error:
        checks.append({"name": name, "passed": False, "error": str(error)})


def denied_syscall(number, *arguments):
    ctypes.set_errno(0)
    result = libc.syscall(number, *arguments)
    if number == 56 and result == 0:
        os._exit(99)
    if number == 56 and result > 0:
        os.waitpid(result, 0)
    if result != -1 or ctypes.get_errno() not in (errno.EPERM, errno.EACCES):
        raise RuntimeError(f"Syscall {number}: result={result}, errno={ctypes.get_errno()}")


# Reject unrelated namespaces, namespace joining, and newer mount interfaces.
check("uts-clone-denied", lambda: denied_syscall(56, 0x1C020000 | signal.SIGCHLD, 0, 0, 0, 0))
check("cgroup-unshare-denied", lambda: denied_syscall(272, 0x02000000))
with open("/proc/self/ns/user", "rb") as namespace:
    check("setns-denied", lambda: denied_syscall(308, namespace.fileno(), 0))
check("fsopen-denied", lambda: denied_syscall(430, ctypes.c_char_p(b"tmpfs"), 0))
check("open-tree-denied", lambda: denied_syscall(428, -100, ctypes.c_char_p(b"/"), 1))
check("fsmount-denied", lambda: denied_syscall(432, -1, 0, 0))
check(
    "move-mount-denied",
    lambda: denied_syscall(429, -1, ctypes.c_char_p(b""), -1, ctypes.c_char_p(b""), 0),
)
check("mount-setattr-denied", lambda: denied_syscall(442, -1, ctypes.c_char_p(b""), 0, 0, 0))
check("bpf-denied", lambda: denied_syscall(321, -1, 0, 0))
check("keyctl-denied", lambda: denied_syscall(250, 0, -1, 0))

for directory in (
    "/workspace",
    "/tools",
    "/oldroot/proc/sys",
    "/oldroot/sys",
    "/newroot/proc/sys",
    "/newroot/sys",
):
    Path(directory).mkdir(parents=True, exist_ok=True)

with (
    tempfile.TemporaryDirectory(prefix="policy-probe-", dir="/workspace") as workspace_name,
    tempfile.TemporaryDirectory(prefix="policy-probe-", dir="/tools") as tools_name,
):
    workspace, tools = Path(workspace_name), Path(tools_name)
    metadata = tools / "metadata"
    metadata.mkdir()
    (metadata / "marker").write_text("protected synthetic metadata")
    (workspace / "metadata-link").symlink_to(metadata, target_is_directory=True)
    base = [
        "/usr/bin/bwrap",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-net",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        str(workspace),
        str(workspace),
        "--ro-bind",
        str(tools),
        str(tools),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--cap-drop",
        "ALL",
    ]
    inner = r"""
import ctypes,errno,json,os,sys
from pathlib import Path
workspace,metadata=map(Path,sys.argv[1:])
def need(condition,message):
 if not condition: raise RuntimeError(message)
need((metadata/'marker').read_text()=='protected synthetic metadata','Synthetic source changed')
(workspace/'allowed').write_text('allowed synthetic write')
for name in (
 metadata/'marker',workspace/'metadata-link'/'marker',
 Path('/proc/sys/kernel/hostname'),Path('/sys/kernel/notes')
):
 try:
  fd=os.open(name,os.O_WRONLY)
  os.close(fd)
 except OSError as error:
  need(error.errno in (errno.EACCES,errno.EPERM,errno.EROFS),str(error))
 else: raise RuntimeError('Protected write succeeded: '+str(name))
try:
 fd=os.open('/proc/kcore',os.O_RDONLY)
except OSError as error:
 need(error.errno in (errno.EACCES,errno.EPERM),str(error))
else:
 os.close(fd)
 raise RuntimeError('Protected proc read opened')
libc=ctypes.CDLL(None,use_errno=True)
for path in (metadata,Path('/proc/sys'),Path('/sys')):
 result=libc.mount(None,os.fsencode(path),None,0x9020,None)
 need(
  result==-1 and ctypes.get_errno() in (errno.EPERM,errno.EACCES,errno.EROFS),
  'Protected remount succeeded'
 )
print('positive read/write and canonical/symlink/remount denials passed')
"""

    def sandbox_controls():
        result = subprocess.run(
            [*base, "--", "/usr/local/bin/python", "-c", inner, str(workspace), str(metadata)],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        if (metadata / "marker").read_text() != "protected synthetic metadata":
            raise RuntimeError("Protected source changed")

    check("bwrap-positive-and-protected-access", sandbox_controls)
    cases = [("metadata-within-root", metadata, tools / "metadata-alias", "marker")]
    for root in (workspace, Path("/oldroot"), Path("/newroot")):
        cases.extend(
            [
                (str(root) + "-proc", Path("/proc"), root / "proc", "sys/kernel/hostname"),
                (str(root) + "-sys", Path("/sys"), root / "sys", "kernel/notes"),
            ]
        )
    for name, source, alias, relative in cases:
        alias.mkdir(exist_ok=True)

        def alias_denied(source=source, alias=alias, relative=relative):
            code = r"""
import ctypes,errno,os,sys
from pathlib import Path
alias,target,source=map(Path,sys.argv[1:])
print('ALIAS-COMMAND-STARTED',flush=True)
def denied_open(path,flags):
 try: fd=os.open(path,flags)
 except OSError as error:
  if error.errno not in (errno.EACCES,errno.EPERM,errno.EROFS): raise
 else:
  os.close(fd)
  raise RuntimeError('Protected alias opened: '+str(path))
denied_open(target,os.O_WRONLY)
if source==Path('/proc'): denied_open(alias/'kcore',os.O_RDONLY)
libc=ctypes.CDLL(None,use_errno=True)
if (
 libc.mount(None,os.fsencode(alias),None,0x9020,None)!=-1 or
 ctypes.get_errno() not in (errno.EPERM,errno.EACCES,errno.EROFS)
):
 raise RuntimeError('Protected alias remount succeeded')
print('ALIAS-DENIALS-PASSED')
"""
            result = subprocess.run(
                [
                    *base,
                    "--bind",
                    str(source),
                    str(alias),
                    "--",
                    "/usr/local/bin/python",
                    "-c",
                    code,
                    str(alias),
                    str(alias / relative),
                    str(source),
                ],
                capture_output=True,
                text=True,
            )
            if "ALIAS-COMMAND-STARTED" in result.stdout:
                if result.returncode or "ALIAS-DENIALS-PASSED" not in result.stdout:
                    raise RuntimeError(result.stdout + result.stderr)
                stage = "inner-access-and-remount-denied"
            else:
                # A namespace/startup failure cannot count as an alias denial.
                error = result.stderr.lower()
                if (
                    result.returncode == 0
                    or "bind mount" not in error
                    or not any(
                        term in error
                        for term in (
                            "permission denied",
                            "operation not permitted",
                            "read-only file system",
                        )
                    )
                    or not any(str(path) in result.stderr for path in (source, alias))
                ):
                    raise RuntimeError(
                        "Unclassified alias startup failure: " + result.stdout + result.stderr
                    )
                stage = "alias-bind-mount-denied"
            if (metadata / "marker").read_text() != "protected synthetic metadata":
                raise RuntimeError("Protected source changed through alias")
            return {"denialStage": stage, "source": str(source), "alias": str(alias)}

        check(name + "-bind-write-denied", alias_denied)

print(
    json.dumps(
        {"outer": outer, "checks": checks, "passed": all(row["passed"] for row in checks)}, indent=2
    )
)
raise SystemExit(0 if all(row["passed"] for row in checks) else 1)
