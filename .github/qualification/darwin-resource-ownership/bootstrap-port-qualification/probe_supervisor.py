"""Verify the production Seatbelt rule with a disposable supervisor tree."""

import errno
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ORIGINAL = b"trusted plist fixture\n"
REPLACEMENT = b"replacement fixture\n"


def operations(case):
    parent = case / "parent"
    supervisor = parent / "supervisor"
    job = supervisor / "disposable-job"
    return {
        "read": lambda: (job / "read.plist").read_bytes(),
        "list": lambda: list(supervisor.iterdir()),
        "overwrite": lambda: (job / "overwrite.plist").write_bytes(REPLACEMENT),
        "replace": lambda: os.replace(case / "replacement", job / "replace.plist"),
        "unlink": lambda: (job / "unlink.plist").unlink(),
        "move-job": lambda: job.rename(supervisor / "moved-job"),
        "symlink": lambda: (job / "new.plist").symlink_to(case / "symlink-target"),
        "move-supervisor": lambda: supervisor.rename(parent / "moved-supervisor"),
        "move-ancestor": lambda: parent.rename(case / "moved-parent"),
        "sibling": lambda: (case / "sibling").write_bytes(REPLACEMENT),
    }


def payload(case, operation):
    try:
        operations(case)[operation]()
    except OSError as exc:
        print(json.dumps({"errno": exc.errno}))
    else:
        print(json.dumps({"errno": 0}))


def make_case(root, operation):
    case = root / operation
    job = case / "parent" / "supervisor" / "disposable-job"
    job.mkdir(parents=True, mode=0o700)
    for kind in ("read", "overwrite", "replace", "unlink"):
        (job / f"{kind}.plist").write_bytes(ORIGINAL)
    for kind in ("replacement", "symlink-target"):
        (case / kind).write_bytes(REPLACEMENT)
    return case, job


def run_case(root, operation, helper, mode):
    case, job = make_case(root, operation)
    argv = [sys.executable, "-I", "-B", __file__, "--payload", str(case), operation]
    if mode != "trusted":
        profile = '\n'.join((
            "(version 1)",
            "(deny default)",
            "(allow process-exec process-fork sysctl-read file-read* file-map-executable)",
            '(allow file-write* (literal "/private/tmp") (subpath "/private/tmp"))',
            '(allow file-write-data (literal "/dev/null"))',
        ))
        if mode == "protected":
            rules = subprocess.check_output(
                [helper, "supervisor-policy", job.parent], text=True, timeout=10
            )
            profile += "\n" + rules
        argv = ["/usr/bin/sandbox-exec", "-p", profile, *argv]
    result = subprocess.run(argv, text=True, capture_output=True, timeout=10, check=True)
    observed = json.loads(result.stdout)["errno"]
    print(f"{mode} {operation}: errno={observed}", flush=True)
    if mode == "protected" and operation != "sibling":
        assert observed in (errno.EACCES, errno.EPERM), (operation, result.stdout, result.stderr)
        assert job.is_dir()
        assert all((job / f"{kind}.plist").read_bytes() == ORIGINAL
                   for kind in ("read", "overwrite", "replace", "unlink"))
        assert not (job / "new.plist").is_symlink()
        assert (case / "replacement").read_bytes() == REPLACEMENT
    else:
        assert observed == 0, (mode, operation, result.stdout, result.stderr)
    if operation == "sibling":
        assert (case / "sibling").read_bytes() == REPLACEMENT


def main(helper):
    assert sys.platform == "darwin" and platform.machine() == "arm64"
    assert os.getuid() == os.geteuid() != 0
    with tempfile.TemporaryDirectory(prefix="sdk-supervisor-fixture-", dir="/private/tmp") as temp:
        for mode in ("trusted", "grant-only", "protected"):
            root = Path(temp) / mode
            for operation in operations(root):
                run_case(root, operation, helper, mode)
    print("PASS: the production rule blocked supervisor reads and mutations with /tmp writable")


if __name__ == "__main__":
    if sys.argv[1] == "--payload":
        payload(Path(sys.argv[2]), sys.argv[3])
    else:
        main(sys.argv[1])
