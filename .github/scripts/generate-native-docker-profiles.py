#!/usr/bin/env python3
"""Generate a diagnostic-only confined Docker profile; never load host policy."""

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

UPSTREAM = "https://raw.githubusercontent.com/moby/moby/v28.0.4/profiles/"
EXPECTED = {
    "seccomp/default.json": "9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76",
    "apparmor/template.go": "42130ca5908f45263facef820961d9e1a988e82f8a2937f4658e7bcccc96cc07",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise SystemExit(message)


p = argparse.ArgumentParser()
p.add_argument("--output-dir", required=True)
p.add_argument("--run-id", required=True)
p.add_argument("--run-attempt", required=True)
p.add_argument("--source-commit", required=True)
p.add_argument("--image-id", required=True)
p.add_argument("--recipe-sha256", required=True)
a = p.parse_args()
require(re.fullmatch(r"[0-9]+", a.run_id), "Invalid run ID")
require(re.fullmatch(r"[0-9]+", a.run_attempt), "Invalid run attempt")
require(re.fullmatch(r"[0-9a-f]{40}", a.source_commit), "Invalid source commit")
require(re.fullmatch(r"sha256:[0-9a-f]{64}", a.image_id), "Invalid immutable image ID")
require(re.fullmatch(r"[0-9a-f]{64}", a.recipe_sha256), "Invalid recipe digest")
out = Path(a.output_dir)
out.mkdir(mode=0o700, parents=True, exist_ok=False)
source = {}
for filename, expected in EXPECTED.items():
    with urllib.request.urlopen(UPSTREAM + filename, timeout=30) as response:
        data = response.read(100_000)
    require(digest(data) == expected, f"Upstream bytes changed: {filename}")
    source[filename] = data
    (out / ("upstream-" + filename.replace("/", "-"))).write_bytes(data)

policy = json.loads(source["seccomp/default.json"])
require(
    policy["defaultAction"] == "SCMP_ACT_ERRNO" and policy["defaultErrnoRet"] == 1,
    "Unexpected default seccomp action",
)
original = list(policy["syscalls"])


def allow(name, args):
    policy["syscalls"].append({"names": [name], "action": "SCMP_ACT_ALLOW", "args": args})


def eq(index, value):
    return {"index": index, "value": value, "op": "SCMP_CMP_EQ"}


# Native requires USER, mount and IPC; trusted PID/network modes may vary.
# No UTS, cgroup, thread-sharing, VM-sharing, or namespace joining allowances.
for flags in (0x18020011, 0x38020011, 0x58020011, 0x78020011):
    allow("clone", [eq(0, flags)])
# Bubblewrap drops setup privileges in a second fresh USER namespace.
allow("unshare", [eq(0, 0x10000000)])
# Actual bubblewrap 0.12.0 setup classes: propagation, virtual filesystems, binds.
for flags in (0x8C000, 6, 14, 10, 0xC0EDD000, 0xD000, 0x9000, 0x4C000):
    allow("mount", [eq(3, flags)])
# Bind-remount requires BIND|REMOUNT|SILENT; only existing filesystem safety/
# timestamp flags can vary. This does not allow arbitrary mount flag classes.
optional = 1 | 2 | 4 | 8 | 1024 | 2048 | 0x200000 | 0x1000000
allow(
    "mount",
    [
        {
            "index": 3,
            "value": 0xFFFFFFFFFFFFFFFF ^ optional,
            "valueTwo": 0x9020,
            "op": "SCMP_CMP_MASKED_EQ",
        }
    ],
)
allow("pivot_root", [])
allow("umount2", [eq(1, 2)])
require(policy["syscalls"][: len(original)] == original, "Original seccomp rules changed")
seccomp = (json.dumps(policy, indent=2) + "\n").encode()
(out / "seccomp.json").write_bytes(seccomp)

# Preserve the exact Docker 28.0.4 default body except its blanket mount deny.
# These source-derived path/flag rules remain diagnostic until hosted audit proof.
name = f"agents-native-ci-{a.run_id}-{a.run_attempt}"
template = (
    source["apparmor/template.go"].decode().split("const baseTemplate = `", 1)[1].rsplit("`", 1)[0]
)
template = template.replace(
    "{{range $value := .Imports}}\n{{$value}}\n{{end}}", "#include <tunables/global>"
)
template = template.replace("{{range $value := .InnerImports}}\n  {{$value}}\n{{end}}", "")
template = template.replace("{{.Name}}", name).replace("{{.DaemonProfile}}", "unconfined")
require("{{" not in template, "Unexpanded AppArmor template token")
require(template.count("  deny mount,") == 1, "Unexpected AppArmor mount policy")
rules = (
    """  # Diagnostic: fresh child userns, then only bwrap setup mount classes.
  userns,
  mount options=(rw,rslave,silent) -> /,
  mount fstype=tmpfs options=(rw,nosuid,nodev) -> /tmp/,
  mount options=(rw,rbind,silent) /tmp/newroot/ -> /tmp/newroot/,
  pivot_root oldroot=/tmp/oldroot/ /tmp/,
  mount options=(rw,rbind,silent) /oldroot/proc/ -> /proc/,
  mount fstype=tmpfs options=(rw,nosuid,nodev) -> /newroot/**,
  mount fstype=proc options=(rw,nosuid,nodev,noexec) -> /newroot/proc/,
  mount fstype=devpts options=(rw,nosuid,noexec) -> /newroot/dev/pts/,
  mount options=(rw,bind,silent) /oldroot/ -> /newroot/,
  mount options=(rw,rbind,silent) /oldroot/ -> /newroot/,
  # Pinned image usrmerge aliases used by the native minimal proc preflight.
  mount options=(rw,rbind,silent) /oldroot/usr/bin/ -> /newroot/bin/,
  mount options=(rw,rbind,silent) /oldroot/usr/lib/ -> /newroot/lib/,
  mount options=(rw,rbind,silent) /oldroot/usr/lib64/ -> /newroot/lib64/,
  mount options=(rw,rbind,silent) /oldroot/usr/sbin/ -> /newroot/sbin/,
  # Temporary files created by SETUP_MAKE_RO_BIND_FILE carry supplied bind data.
  mount options=(rw,bind,silent) /bindfile?????? -> /newroot/**,
"""
    "  remount options in (ro,rw,remount,bind,silent,nosuid,nodev,noexec,"
    "noatime,nodiratime,relatime,strictatime) /newroot/{,**},\n"
    """  mount options=(rw,rprivate,silent) -> /oldroot/,
  pivot_root oldroot=/newroot/ /newroot/,
"""
)
# Fixed fixture/runtime pairs block generic cross-root aliases; suffix aliases remain.
# Every pair keeps its top-level identity; proc/sys receive no generic pair.
for root in (
    "workspace",
    "data-write",
    "mnt/native-readonly",
    "mnt/native-writable",
    "bin",
    "sbin",
    "usr",
    "etc",
    "lib",
    "lib64",
):
    for option in ("bind", "rbind"):
        rules += (
            f"  mount options=(rw,{option},silent) /oldroot/{root}{{,/**}}"
            f" -> /newroot/{root}{{,/**}},\n"
        )
for option in ("bind", "rbind"):
    rules += f"  mount options=(rw,{option},silent) /oldroot/tools/ -> /newroot/tools/,\n"
for device in ("null", "zero", "full", "random", "urandom", "tty"):
    rules += f"  mount options=(rw,rbind,silent) /oldroot/dev/{device} -> /newroot/dev/{device},\n"
apparmor = template.replace("  deny mount,", rules.rstrip()).encode()
(out / "apparmor.profile").write_bytes(apparmor)
manifest = {
    "format": "native-docker-runner-profile-v1",
    "runId": a.run_id,
    "runAttempt": a.run_attempt,
    "sourceCommit": a.source_commit,
    "imageId": a.image_id,
    "recipeSha256": a.recipe_sha256,
    "upstream": {
        "version": "v28.0.4",
        "seccompSha256": EXPECTED["seccomp/default.json"],
        "apparmorTemplateSha256": EXPECTED["apparmor/template.go"],
    },
    "seccomp": {"path": "/etc/openai-agents-native-docker/seccomp.json", "sha256": digest(seccomp)},
    "apparmor": {
        "name": name,
        "path": "/etc/openai-agents-native-docker/apparmor.profile",
        "sha256": digest(apparmor),
    },
    "diagnosticOnly": True,
}
(out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(out / "manifest.json")
