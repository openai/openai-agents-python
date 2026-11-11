"""Collect fixture-owned launchd behavior; no SDK, model, or network activity."""

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMMAND_TIMEOUT = 5
OBSERVATION_SECONDS = 5


def command(argv, *, keep_stdout=True, full_stdout=False):
    child = subprocess.Popen(
        [str(a) for a in argv], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = child.communicate(timeout=COMMAND_TIMEOUT)
    except BaseException as error:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = child.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = child.communicate(timeout=2)
        if not isinstance(error, subprocess.TimeoutExpired):
            raise
        timed_out = True
    return {
        "argv": [str(a) for a in argv], "returncode": child.returncode,
        "timed_out": timed_out, "stdout": (stdout if full_stdout else stdout[-4096:]) if keep_stdout else None,
        "stderr": stderr[-4096:], "child_joined": child.poll() is not None,
    }


def launchctl(*args, **kwargs):
    return command(["/bin/launchctl", *args], **kwargs)


def state(service):
    value = launchctl("print", service, full_stdout=True)
    match = re.search(r"^\s*runs = (\d+)\s*$", value["stdout"], re.MULTILINE)
    value["runs"] = int(match[1]) if match else None
    # Only the run count has been leaving the local print buffer; launchd may print environment values.
    value["stdout"] = None
    return value


def absent(value):
    return not value["timed_out"] and value["returncode"] != 0 and bool(
        re.search(r"could not find.*service|service.*not found|no such process",
                  value["stderr"], re.IGNORECASE)
    )


def interrupted(number, frame):
    raise KeyboardInterrupt(signal.Signals(number).name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shim", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin" or args.output.exists():
        raise RuntimeError("A fresh output on macOS has been required")
    shim = args.shim.resolve(strict=True)
    root = Path(tempfile.mkdtemp(prefix="agents-launchd-diagnostic-")).resolve()
    result = {"diagnostic_only": True, "complete": False, "uid": os.getuid(),
              "system": list(os.uname()), "fixture_root": str(root), "cells": [],
              "cleanup": [], "security_conclusion": None}
    owned = []
    previous = {n: signal.signal(n, interrupted) for n in (signal.SIGTERM, signal.SIGINT)}
    failure = None
    try:
        domain = None
        for candidate in (f"gui/{os.getuid()}", f"user/{os.getuid()}"):
            probe = launchctl("print", candidate, keep_stdout=False)
            result.setdefault("domain_controls", []).append(probe)
            if probe["returncode"] == 0 and not probe["timed_out"]:
                domain = candidate
                break
        if domain is None:
            raise RuntimeError("No fixture launchd domain has been available")
        result["domain"] = domain
        base = (HERE / "core-base.sbpl").read_text()
        profiles = {
            "host": None,
            "allow-default": "(version 1)\n(allow default)\n",
            "deny-job": "(version 1)\n(allow default)\n(deny job-creation)\n",
            "deny-job-mach": "(version 1)\n(allow default)\n(deny job-creation)\n(deny mach-lookup)\n",
            "core-base-diagnostic": base + "\n(allow file-read*)\n"
                + "(allow file-write* (subpath " + json.dumps(str(root)) + "))\n"
                + '(deny mach-lookup (xpc-service-name-prefix ""))\n',
        }
        result["core_base_sha256"] = hashlib.sha256(base.encode()).hexdigest()
        result["core_profile_limit"] = (
            "Exact copied Core base plus diagnostic read/fixture-write permissions and "
            "the generator's XPC-service deny; not the complete generated Core profile."
        )
        for profile_name, policy in profiles.items():
            profile = root / (profile_name + ".sbpl")
            if policy is not None:
                profile.write_text(policy)
            for mode in ("inherited", "registered", "null-bootstrap"):
                label = "org.openai.agents.launchd-diag." + uuid.uuid4().hex
                service = domain + "/" + label
                marker = root / (label + ".marker")
                plist = root / (label + ".plist")
                job = {"Label": label, "ProgramArguments": ["/usr/bin/touch", str(marker)],
                       "RunAtLoad": False, "KeepAlive": False, "ExitTimeOut": 2,
                       "ThrottleInterval": 1}
                if domain.startswith("user/"):
                    job["LimitLoadToSessionType"] = "Background"
                plist.write_bytes(plistlib.dumps(job))
                cell = {"profile": profile_name, "mode": mode, "service": service,
                        "marker": str(marker), "job": job,
                        "profile_sha256": hashlib.sha256(policy.encode()).hexdigest() if policy else None}
                result["cells"].append(cell)
                cell["before_bootstrap"] = state(service)
                if not absent(cell["before_bootstrap"]):
                    raise RuntimeError("Unique fixture label has not been proving absent")
                owned.append((service, marker))
                cell["bootstrap"] = launchctl("bootstrap", domain, str(plist))
                cell["before"] = state(service)
                if cell["bootstrap"]["returncode"] or cell["bootstrap"]["timed_out"]:
                    raise RuntimeError("Host bootstrap has not been succeeding")
                if cell["before"]["returncode"] or cell["before"]["runs"] != 0 or marker.exists():
                    raise RuntimeError("New fixture job has not been remaining idle")
                prefix = [str(shim), mode]
                if policy is not None:
                    prefix += ["/usr/bin/sandbox-exec", "-f", str(profile)]
                cell["payload_control"] = command(prefix + ["/usr/bin/true"])
                cell["identity"] = [command(prefix + argv) for argv in
                                    (["/usr/bin/id", "-un"], ["/usr/bin/whoami"])]
                cell["kickstart"] = command(prefix + ["/bin/launchctl", "kickstart", service])
                deadline = time.monotonic() + OBSERVATION_SECONDS
                while True:
                    after = state(service)
                    if after["returncode"] or after["timed_out"] or after["runs"] is None:
                        raise RuntimeError("Host post-kickstart observation has been failing")
                    if marker.exists() or time.monotonic() >= deadline:
                        break
                    time.sleep(0.1)
                cell["after"] = after
                cell["marker_after"] = marker.exists()
                cell["job_started"] = after["runs"] > 0
                cell["execution_observed"] = cell["marker_after"] or cell["job_started"]
                cell["observation_seconds_max"] = OBSERVATION_SECONDS
                cell["payload_control_ok"] = cell["payload_control"]["returncode"] == 0 and not cell["payload_control"]["timed_out"]
                if profile_name == "host" and mode == "inherited":
                    if cell["kickstart"]["returncode"] or not cell["marker_after"] or after["runs"] != 1:
                        raise RuntimeError("Unconfined positive control has not been proving actual execution")
        result["complete"] = True
    except BaseException as error:
        failure = error
        result["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        deferred = []
        for number in previous:
            signal.signal(number, lambda number, frame: deferred.append(number))
        try:
            for service, marker in reversed(owned):
                row = {"service": service, "absent": False}
                result["cleanup"].append(row)
                try:
                    row["bootout"] = launchctl("bootout", service)
                    deadline = time.monotonic() + COMMAND_TIMEOUT
                    while True:
                        final = state(service)
                        if absent(final) or time.monotonic() >= deadline:
                            break
                        time.sleep(0.1)
                    row.update(final=final, marker_at_cleanup=marker.exists(), absent=absent(final))
                except BaseException as error:
                    row["error"] = {"type": type(error).__name__, "message": str(error)}
            result["all_owned_labels_absent"] = all(row["absent"] for row in result["cleanup"])
            result["complete"] = result["complete"] and result["all_owned_labels_absent"] and not deferred
            result["deferred_cleanup_signals"] = deferred
            if result["all_owned_labels_absent"]:
                shutil.rmtree(root)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2))
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
    if failure is not None:
        raise failure
    if not result["complete"]:
        raise RuntimeError("Fixture cleanup has not been completing")


if __name__ == "__main__":
    main()
