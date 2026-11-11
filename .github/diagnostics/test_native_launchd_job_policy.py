"""Query live Seatbelt policy from the installed local executor's command process."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal
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

_TOKEN_VARIABLE = "NOV9_JOB_POLICY_TOKEN"
_PREFIX = "JOB_POLICY_HELPER "
_CONTEXT_FIELDS = ("pid_match", "cwd_match", "uid", "euid", "token_match")
_QUERY_FIELDS = (
    "sandbox_status",
    "job_creation_none",
    "job_creation_path",
    "inside_write",
    "outside_write",
)
_DENY_PROFILE = """
(version 1)
(allow default)
(deny job-creation)
(deny file-write-data (literal (param "OUTSIDE_CANARY")))
"""


def _outcome(query: str, rc: int) -> str:
    if query == "sandbox_status":
        return {0: "unsandboxed", 1: "sandboxed"}.get(rc, "error")
    if rc < 0:
        return "error"
    return "allow" if rc == 0 else "deny"


def _receipt(output: str) -> dict[str, Any]:
    lines = [line.removeprefix(_PREFIX) for line in output.splitlines() if line.startswith(_PREFIX)]
    if len(lines) != 1:
        return {"helper_receipt_count": len(lines)}
    try:
        raw = json.loads(lines[0])
    except json.JSONDecodeError:
        return {"helper_error": "invalid_json"}
    if not isinstance(raw, dict):
        return {"helper_error": "invalid_receipt"}

    # Report only fixed fields and numeric data, even if tool output is malformed.
    result: dict[str, Any] = {}
    if raw.get("helper_error") in ("invalid_arguments", "sandbox_check_unavailable"):
        result["helper_error"] = raw["helper_error"]
    context = raw.get("context")
    if isinstance(context, dict):
        result["context"] = {
            key: context[key] for key in _CONTEXT_FIELDS if type(context.get(key)) is int
        }
    queries = raw.get("queries")
    if isinstance(queries, dict):
        result["queries"] = {}
        for name in _QUERY_FIELDS:
            query = queries.get(name)
            if (
                isinstance(query, dict)
                and type(query.get("rc")) is int
                and type(query.get("errno")) is int
            ):
                result["queries"][name] = {
                    "rc": query["rc"],
                    "errno": query["errno"],
                    "outcome": _outcome(name, query["rc"]),
                }
    return result


def _control(argv: list[str], workspace: Path, token: str) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            cwd=workspace,
            env={_TOKEN_VARIABLE: token},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"control_error": type(error).__name__}
    return {"shell_exit": proc.returncode, **_receipt(proc.stdout)}


def _outcomes(receipt: dict[str, Any]) -> dict[str, str]:
    return {name: result["outcome"] for name, result in receipt.get("queries", {}).items()}


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS Seatbelt")
@pytest.mark.requires_native_macos_sandbox
@pytest.mark.asyncio
@pytest.mark.parametrize("write_policy", ["strict", "trusted_workspace_write"])
async def test_macos_launchd_job_policy(
    responses: ResponsesFixture,
    tmp_path: Path,
    write_policy: Literal["strict", "trusted_workspace_write"],
) -> None:
    root = await asyncio.to_thread(tmp_path.resolve)
    workspace = root / "workspace"
    workspace.mkdir()
    probe = workspace / "launchd-job-policy"
    shutil.copy2(os.environ["NOV9_JOB_POLICY_PROBE"], probe)
    inside, outside = workspace / "inside-canary", root / "outside-canary"
    inside.write_bytes(b"inside\n")
    outside.write_bytes(b"outside\n")
    token = uuid4().hex
    command = f'exec {shlex.quote(str(probe))} "$$" ' + shlex.join(
        [str(workspace), token, str(inside), str(outside)]
    )
    shell = ["/bin/sh", "-c", command]
    host = await asyncio.to_thread(_control, shell, workspace, token)
    calibration = await asyncio.to_thread(
        _control,
        [
            "/usr/bin/sandbox-exec",
            "-D",
            f"OUTSIDE_CANARY={outside}",
            "-p",
            _DENY_PROFILE,
            *shell,
        ],
        workspace,
        token,
    )

    responses.respond(
        function_call(
            "exec_command", {"cmd": command, "login": False, "yield_time_ms": 10000}, "policy"
        )
    )
    responses.respond(assistant_output("checked"))
    managed: dict[str, Any] = {}
    try:
        provider = NativeProvider(base_url=responses.url, api_key="synthetic-model-key")
        environment = Environment.local(
            cwd=workspace,
            tools=["exec_command"],
            write_policy=write_policy,
            env={_TOKEN_VARIABLE: token},
        )
        async with (
            timeout(35),
            provider,
            Agent(instructions="Use the selected tools.", provider=provider).session(
                environment=environment
            ) as session,
        ):
            turn = await session.submit("Query the live command policy.")
            result = await turn.get_result()
            managed["turn_completed"] = result.final_output == "checked"
            managed["model_requests"] = len(responses.requests)
            output = tool_output(responses.requests[1], "policy")
            managed.update(_receipt(output))
            shell_exit = re.search(r"^Process exited with code (\d+)$", output, re.MULTILINE)
            managed["shell_exit"] = int(shell_exit[1]) if shell_exit else None
    except Exception as error:
        # The receipt carries the failure without printing tool input or exceptions.
        managed["execution_error"] = type(error).__name__

    print(
        "LAUNCHD_JOB_POLICY "
        + json.dumps(
            {
                "write_policy": write_policy,
                "host": host,
                "deny_calibration": calibration,
                "managed": managed,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    expected_context = {
        "pid_match": 1,
        "cwd_match": 1,
        "uid": os.getuid(),
        "euid": os.geteuid(),
        "token_match": 1,
    }
    for receipt in (host, calibration, managed):
        assert receipt.get("shell_exit") == 0, receipt
        assert receipt.get("context") == expected_context, receipt
        assert set(_outcomes(receipt)) == set(_QUERY_FIELDS), receipt
    assert _outcomes(host) == {
        "sandbox_status": "unsandboxed",
        "job_creation_none": "allow",
        "job_creation_path": "allow",
        "inside_write": "allow",
        "outside_write": "allow",
    }, host
    assert _outcomes(calibration) == {
        "sandbox_status": "sandboxed",
        "job_creation_none": "deny",
        "job_creation_path": "deny",
        "inside_write": "allow",
        "outside_write": "deny",
    }, calibration
    assert managed.get("turn_completed") and managed.get("model_requests") == 2, managed
    assert "execution_error" not in managed, managed
    actual = _outcomes(managed)
    assert actual["sandbox_status"] == "sandboxed", managed
    assert actual["inside_write"] == "allow" and actual["outside_write"] == "deny", managed
    assert all(
        actual[key] in ("allow", "deny") for key in ("job_creation_none", "job_creation_path")
    ), managed
    assert inside.read_bytes() == b"inside\n" and outside.read_bytes() == b"outside\n"
