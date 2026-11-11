"""Measure launchd registration and execution through the installed local executor."""

from __future__ import annotations

import asyncio
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

import pytest
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)

from agents import Agent, Environment, NativeProvider

_WINDOW_SECONDS = 5
_ABSENT = ("absent", "domain_absent")


@dataclass(frozen=True)
class Job:
    label: str
    plist: Path
    marker: Path


class State(TypedDict):
    runs: dict[str, int | str]
    marker: bool


class Observation(TypedDict):
    elapsed_seconds: float
    samples: int
    states: list[State]


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True, timeout=5, check=False
    )


def _job(workspace: Path, domain: str, *, legacy_submit: bool) -> Job:
    label = f"org.openai.agents.sdk-launchd-causal.{uuid4().hex}"
    job = Job(label, workspace / f"{label}.plist", workspace.parent / f"{label}.marker")
    if not legacy_submit:
        properties = {
            "Label": label,
            "ProgramArguments": ["/usr/bin/touch", str(job.marker)],
            "RunAtLoad": False,
            "KeepAlive": False,
            "ExitTimeOut": 2,
            "ThrottleInterval": 1,
        }
        if domain.startswith("user/"):
            properties["LimitLoadToSessionType"] = "Background"
        job.plist.write_bytes(plistlib.dumps(properties))
    return job


def _snapshot(job: Job, domains: dict[str, bool]) -> State:
    runs: dict[str, int | str] = {}
    for domain, domain_available in domains.items():
        try:
            status = _launchctl("print", f"{domain}/{job.label}")
        except (OSError, subprocess.TimeoutExpired) as error:
            runs[domain] = type(error).__name__
            continue
        if status.returncode == 0:
            count = re.search(r"^[ \t]*runs = (\d+)[ \t]*$", status.stdout, re.MULTILINE)
            runs[domain] = int(count[1]) if count else "registered_without_run_count"
        elif not domain_available and "Could not find domain" in status.stderr:
            runs[domain] = "domain_absent"
        elif "Could not find service" in status.stderr and f'"{job.label}"' in status.stderr:
            runs[domain] = "absent"
        else:
            runs[domain] = f"unreadable_exit_{status.returncode}"
    return {"runs": runs, "marker": job.marker.exists()}


def _absent(state: State) -> bool:
    return all(count in _ABSENT for count in state["runs"].values())


def _idle(state: State, domain: str) -> bool:
    return (
        not state["marker"]
        and state["runs"][domain] == 0
        and all(count == 0 or count in _ABSENT for count in state["runs"].values())
    )


def _ran(state: State) -> bool:
    return state["marker"] and any(
        isinstance(count, int) and count > 0 for count in state["runs"].values()
    )


async def _observe(
    job: Job, domains: dict[str, bool], *, until: Callable[[State], bool] | None = None
) -> Observation:
    start = time.monotonic()
    states: list[State] = []
    samples = 0
    while True:
        state = _snapshot(job, domains)
        samples += 1
        if not states or state != states[-1]:
            states.append(state)
        elapsed = time.monotonic() - start
        if (until is not None and until(state)) or elapsed >= _WINDOW_SECONDS:
            return {"elapsed_seconds": round(elapsed, 3), "samples": samples, "states": states}
        await asyncio.sleep(0.1)


def _command_args(operation: str, job: Job, domain: str) -> tuple[str, ...]:
    if operation == "bootstrap":
        return ("bootstrap", domain, str(job.plist))
    if operation == "kickstart":
        return ("kickstart", f"{domain}/{job.label}")
    return ("submit", "-l", job.label, "--", "/usr/bin/touch", str(job.marker))


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt and launchd")
@pytest.mark.requires_native_macos_sandbox
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,write_policy",
    [
        (operation, policy)
        for operation in ("bootstrap", "kickstart", "submit")
        for policy in ("strict", "trusted_workspace_write")
    ],
)
async def test_macos_launchd_effect(
    responses: ResponsesFixture,
    tmp_path: Path,
    operation: Literal["bootstrap", "kickstart", "submit"],
    write_policy: Literal["strict", "trusted_workspace_write"],
) -> None:
    workspace = (await asyncio.to_thread(tmp_path.resolve)) / "workspace"
    workspace.mkdir()
    uid = os.geteuid()
    gui_domain, user_domain = f"gui/{uid}", f"user/{uid}"
    gui_available = _launchctl("print", gui_domain).returncode == 0
    domain = gui_domain if gui_available else user_domain
    domains = {gui_domain: gui_available, user_domain: True}
    if uid == 0:
        domains["system"] = True

    legacy_submit = operation == "submit"
    host_job = _job(workspace, domain, legacy_submit=legacy_submit)
    sandbox_job = _job(workspace, domain, legacy_submit=legacy_submit)
    host: dict[str, object] = {"label": host_job.label}
    sandbox: dict[str, object] = {"label": sandbox_job.label}
    cleanup: dict[str, Observation] = {}
    try:
        if legacy_submit:
            host["submit_exit"] = _launchctl(*_command_args("submit", host_job, domain)).returncode
        else:
            host["bootstrap_exit"] = _launchctl(
                *_command_args("bootstrap", host_job, domain)
            ).returncode
            host_idle = await _observe(host_job, domains)
            host["after_bootstrap"] = host_idle
            host["kickstart_exit"] = _launchctl(
                *_command_args("kickstart", host_job, domain)
            ).returncode
        host_run = await _observe(host_job, domains, until=_ran)
        host["after_submit" if legacy_submit else "after_kickstart"] = host_run

        if operation == "kickstart":
            sandbox["setup_bootstrap_exit"] = _launchctl(
                *_command_args("bootstrap", sandbox_job, domain)
            ).returncode
            sandbox_idle = await _observe(sandbox_job, domains)
            sandbox["after_setup_bootstrap"] = sandbox_idle
            baseline = sandbox_idle["states"][-1]
        else:
            baseline = _snapshot(sandbox_job, domains)
        sandbox["before"] = baseline
        command = (
            "rc=0; "
            + shlex.join(["/bin/launchctl", *_command_args(operation, sandbox_job, domain)])
            + f" || rc=$?; printf '{operation} exit %s\\n' \"$rc\""
        )
        responses.respond(
            function_call(
                "exec_command", {"cmd": command, "yield_time_ms": 10000, "login": False}, operation
            )
        )
        responses.respond(assistant_output("checked"))
        provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
        agent = Agent(instructions="Use the selected tools.", provider=provider)
        environment = Environment.local(
            cwd=workspace, tools=["exec_command"], write_policy=write_policy
        )
        async with timeout(45), provider, agent.session(environment=environment) as session:
            try:
                turn = await session.submit("Run the launchd fixture command.")
                result = await turn.get_result()
                sandbox["turn_completed"] = result.final_output == "checked"
                sandbox["model_requests"] = len(responses.requests)
                output = tool_output(responses.requests[1], operation)
                exit_line = re.search(rf"^{operation} exit (\d+)$", output, re.MULTILINE)
                shell_exit = re.search(r"^Process exited with code (\d+)$", output, re.MULTILINE)
                status = int(exit_line[1]) if exit_line else None
                sandbox["launchctl_exit"] = status
                sandbox["shell_exit"] = int(shell_exit[1]) if shell_exit else None
            finally:
                after = await _observe(sandbox_job, domains)
                sandbox["after"] = after
    finally:
        try:
            for job in (host_job, sandbox_job):
                if legacy_submit:
                    with suppress(OSError, subprocess.TimeoutExpired):
                        _launchctl("remove", job.label)
                for service_domain in domains:
                    with suppress(OSError, subprocess.TimeoutExpired):
                        _launchctl("bootout", f"{service_domain}/{job.label}")
            cleanup["host"] = await _observe(host_job, domains, until=_absent)
            cleanup["sandbox"] = await _observe(sandbox_job, domains, until=_absent)
        finally:
            cleanup_confirmed = len(cleanup) == 2 and all(
                _absent(phase["states"][-1]) for phase in cleanup.values()
            )
            print(
                "LAUNCHD_CAUSAL "
                + json.dumps(
                    {
                        "operation": operation,
                        "write_policy": write_policy,
                        "selected_domain": domain,
                        "host": host,
                        "sandbox": sandbox,
                        "cleanup": cleanup,
                        "cleanup_confirmed_absent": cleanup_confirmed,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    assert cleanup_confirmed, cleanup
    host_final = host_run["states"][-1]
    assert _ran(host_final), f"host control did not run: {host}"
    assert all(isinstance(count, int) or count in _ABSENT for count in host_final["runs"].values())
    if legacy_submit:
        assert host["submit_exit"] == 0, host
    else:
        assert host["bootstrap_exit"] == host["kickstart_exit"] == 0, host
        assert all(_idle(state, domain) for state in host_idle["states"]), host
        assert host_final["runs"][domain] == 1, host
    if operation == "kickstart":
        assert sandbox["setup_bootstrap_exit"] == 0, sandbox
        assert all(_idle(state, domain) for state in sandbox_idle["states"]), sandbox
    else:
        assert _absent(baseline) and not baseline["marker"], sandbox
    assert sandbox["turn_completed"] and sandbox["model_requests"] == 2, sandbox
    assert sandbox["shell_exit"] == 0, sandbox
    assert status is not None and status > 0 and after["states"] == [baseline], sandbox
