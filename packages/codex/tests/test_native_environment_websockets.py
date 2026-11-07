"""Effects and native continuation of admitted Environment policies on a real model WS."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
from base64 import b64decode
from contextlib import aclosing
from pathlib import Path

import pytest

from agents import (
    Agent,
    Allow,
    ApprovalRequest,
    InteractionRequired,
    NativeError,
    ReplaceArguments,
    StopTurn,
    TurnStoppedError,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_child_agents import trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_provider_websockets import peer as websocket_peer


def _expected_executor_script(argv, command):
    # Pinned Core shell-command::powershell prefixes scripts only for PowerShell.
    if Path(argv[0]).name.lower() in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
        return "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}\n" + command
    return command


@pytest.mark.parametrize(
    ("tool", "decision"),
    [
        pytest.param(
            tool,
            decision,
            # Admitted processes and strict filesystem writes launch native
            # sandbox helpers; denied and stopped calls need no such helper.
            marks=(
                pytest.mark.requires_native_linux_sandbox
                if sys.platform == "linux"
                else pytest.mark.requires_native_macos_sandbox
            )
            if decision in {"allow", "replace"}
            else (),
        )
        for tool in ("exec_command", "apply_patch")
        for decision in ("allow", "deny", "stop-before", "stop-approval", "replace")
    ],
)
@pytest.mark.parametrize("code", [False, True], ids=["direct", "code"])
@pytest.mark.parametrize("provider_id", [False, True], ids=["missing-id", "real-id"])
async def test_environment_ws_policy_effect_and_history(
    tmp_path, monkeypatch, tool, code, decision, provider_id
):
    calls, approved, native_approvals = [], [], []
    original = (
        {"cmd": "printf original > effect; printf original-receipt"}
        if tool == "exec_command"
        else "*** Begin Patch\n*** Add File: effect\n+original\n*** End Patch"
    )
    effective = (
        {"cmd": "printf accepted > effect; printf accepted-receipt"}
        if tool == "exec_command"
        else "*** Begin Patch\n*** Add File: effect\n+accepted\n*** End Patch"
    )
    if tool == "exec_command" and sys.platform == "win32":
        # The remote Windows shell may start in its installation directory.
        # Use built-in cmdlets supported by constrained language mode and the remote workspace.
        effect_path = str(tmp_path / "remote" / "effect").replace("'", "''")
        original = {
            "cmd": f"Set-Content -LiteralPath '{effect_path}' -Value 'original' "
            "-NoNewline -Encoding utf8 -ErrorAction Stop; "
            "Write-Output 'original-receipt'"
        }
        effective = {
            "cmd": f"Set-Content -LiteralPath '{effect_path}' -Value 'accepted' "
            "-NoNewline -Encoding utf8 -ErrorAction Stop; "
            "Write-Output 'accepted-receipt'"
        }
    text = f"text(await tools.{tool}({json.dumps(original)}));"
    event = (
        code_call(text, "owned")
        if code
        else function_call(tool, original, "owned")
        if tool == "exec_command"
        else {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": tool,
                "call_id": "owned",
                "input": original,
            },
        }
    )
    if provider_id:
        event["item"]["id"] = (
            "fc_123providerOwned" if tool == "exec_command" and not code else "ctc_123providerOwned"
        )

    def before(ctx, value):
        assert ctx.tool_name == tool and value == original
        calls.append(ctx)
        if decision == "stop-before":
            return StopTurn("private-ws-reason")
        if decision == "replace":
            return ReplaceArguments(effective)
        return Allow()

    def approval(ctx, value):
        approved.append((ctx, value))
        if decision == "stop-approval":
            return StopTurn("private-ws-reason")
        return decision != "deny"

    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        if sys.platform == "win32" and tool == "exec_command":
            # Make only this synthetic workspace writable by the real test user.
            # Do not inherit this ACE into fixture credentials or touch other paths.
            system32 = Path(os.environ["SystemRoot"]) / "System32"
            identity = await asyncio.create_subprocess_exec(
                str(system32 / "whoami.exe"),
                "/user",
                "/fo",
                "csv",
                "/nh",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await identity.communicate()
            assert identity.returncode == 0, stderr.decode(errors="replace")
            account = next(csv.reader(stdout.decode(errors="replace").splitlines()))
            sid = account[1]
            assert sid.startswith("S-1-5-"), "expected the test process user SID"
            access = await asyncio.create_subprocess_exec(
                str(system32 / "icacls.exe"),
                str(executor.root),
                "/grant",
                f"*{sid}:(M)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await access.communicate()
            assert access.returncode == 0, stderr.decode(errors="replace")
        trust(executor, tmp_path, monkeypatch)
        model.respond(assistant_output("warm"))
        model.respond(event)
        model.respond(assistant_output("finished"))
        agent = Agent(
            instructions="Use selected Environment tools.",
            provider=model.provider(),
            tools=[],
            programmatic_tool_calling=code,
        )
        async with agent.session(
            environment=declaration(executor, transform_arguments=before, approval=approval)
        ) as session:
            assert (await (await session.submit("warm up")).get_result()).final_output == "warm"
            turn = await session.submit("run one selected operation")
            if decision.startswith("stop") or (code and decision == "deny"):
                with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$") as stopped:
                    await turn.get_result()
                assert not isinstance(stopped.value, InteractionRequired)
                assert not [
                    r
                    for r in executor.requests
                    if (
                        r.get("method") == "process/start"
                        and (
                            "original-receipt" in str(r.get("params"))
                            or "accepted-receipt" in str(r.get("params"))
                            or (r.get("params") or {}).get("metadata", {}).get("toolCallId")
                            == "owned"
                        )
                    )
                    or (r.get("method") == "fs/writeFile" and "effect" in str(r.get("params")))
                ], executor.requests
                assert not (executor.root / "effect").exists()
                assert len(model.requests) == 2
                assert (
                    await (await session.submit("successor")).get_result()
                ).final_output == "finished"
            else:

                async def answer_native_approval():
                    async with aclosing(session.interactions()) as questions:
                        async for question in questions:
                            assert isinstance(question, ApprovalRequest)
                            assert tool == "apply_patch" and decision in {"allow", "replace"}
                            assert question.submission_id == turn.submission_id
                            assert question.turn_id == turn.turn_id
                            assert not native_approvals
                            attempts = [
                                r
                                for r in executor.requests
                                if r.get("method") == "fs/writeFile"
                                and (r.get("params") or {}).get("path")
                                == (executor.root / "effect").as_uri()
                            ]
                            assert len(attempts) == 1, executor.requests
                            assert (
                                attempts[0]["params"]["sandbox"]["permissions"]["file_system"][
                                    "type"
                                ]
                                == "restricted"
                            )
                            # Remote native permissions rejected this attempt.
                            # The policy itself granted no workspace access.
                            assert not (executor.root / "effect").exists()
                            native_approvals.append(question)
                            await question.respond("allow_once")

                reader = asyncio.create_task(answer_native_approval())
                try:
                    result = await turn.get_result()
                except NativeError as denied:
                    assert decision == "deny" and not code
                    assert not isinstance(denied, InteractionRequired)
                    assert str(denied) == "Native tool interception failed or was denied"
                    assert not [
                        r
                        for r in executor.requests
                        if (
                            r.get("method") == "process/start"
                            and (
                                "original-receipt" in str(r.get("params"))
                                or (r.get("params") or {}).get("metadata", {}).get("toolCallId")
                                == "owned"
                            )
                        )
                        or (r.get("method") == "fs/writeFile" and "effect" in str(r.get("params")))
                    ], executor.requests
                    assert not (executor.root / "effect").exists()
                    assert len(model.requests) == 2
                    assert (
                        await (await session.submit("successor")).get_result()
                    ).final_output == "finished"
                else:
                    assert result.final_output == "finished"
                    if decision == "deny":
                        denied_output = (
                            code_output(model.requests[-1], "owned")
                            if code
                            else tool_output(model.requests[-1], "owned")
                        )
                        assert "Native tool interception failed or was denied" in denied_output
                        assert not [
                            r
                            for r in executor.requests
                            if (
                                r.get("method") == "process/start"
                                and (
                                    "original-receipt" in str(r.get("params"))
                                    or (r.get("params") or {}).get("metadata", {}).get("toolCallId")
                                    == "owned"
                                )
                            )
                            or (
                                r.get("method") == "fs/writeFile"
                                and "effect" in str(r.get("params"))
                            )
                        ], executor.requests
                        assert not (executor.root / "effect").exists()
                    else:
                        if tool == "exec_command":
                            starts = [
                                r
                                for r in executor.requests
                                if r.get("method") == "process/start"
                                and (r.get("params") or {}).get("metadata", {}).get("toolCallId")
                                == "owned"
                            ]
                            if code:
                                starts = [
                                    r
                                    for r in executor.requests
                                    if r.get("method") == "process/start"
                                    and (
                                        "accepted-receipt" in str(r) or "original-receipt" in str(r)
                                    )
                                ]
                            assert len(starts) == 1, executor.requests
                            command = (effective if decision == "replace" else original)["cmd"]
                            argv = starts[0]["params"]["argv"]
                            assert argv[-1] == _expected_executor_script(argv, command)
                            assert (
                                starts[0]["params"]["sandbox"]["permissions"]["file_system"]["type"]
                                == "restricted"
                            )
                        effect = executor.root / "effect"
                        if effect.exists():
                            assert effect.read_text().strip() == (
                                "accepted" if decision == "replace" else "original"
                            )
                        else:
                            out = (
                                code_output(model.requests[-1], "owned")
                                if code
                                else next(
                                    i["output"]
                                    for i in model.requests[-1]["input"]
                                    if i.get("call_id") == "owned"
                                    and i.get("type") == "function_call_output"
                                )
                            )
                            assert (
                                tool == "exec_command"
                                and "app-server socket directory must be a user-owned directory"
                                in out
                            ), out
                    history = model.requests[-1]["input"]
                    item = next(
                        i
                        for i in history
                        if i.get("call_id") == "owned"
                        and i.get("type") in ("function_call", "custom_tool_call")
                    )
                    if code:
                        assert item["input"] == text
                        out = code_output(model.requests[-1], "owned")
                    else:
                        value = (
                            json.loads(item["arguments"])
                            if tool == "exec_command"
                            else item["input"]
                        )
                        assert value == (effective if decision == "replace" else original), (
                            item,
                            model.frames,
                        )
                finally:
                    reader.cancel()
                    outcome = (await asyncio.gather(reader, return_exceptions=True))[0]
                    if isinstance(outcome, BaseException) and not isinstance(
                        outcome, asyncio.CancelledError
                    ):
                        raise outcome
            assert len(approved) == 1 and approved[0][0].submission is turn
            assert approved[0][1] == original
            if decision in {"deny", "stop-approval"}:
                assert not calls
            else:
                assert len(calls) == 1 and calls[0].submission is turn
                assert calls[0].thread_id == session.id and calls[0].turn_id == turn.turn_id
                if code:
                    assert calls[0].outer_call_id == "owned" and calls[0].cell_id
                else:
                    assert calls[0].outer_call_id is calls[0].cell_id is None
            if tool == "apply_patch" and decision in {"allow", "replace"}:
                writes = [
                    r
                    for r in executor.requests
                    if r.get("method") == "fs/writeFile"
                    and (r.get("params") or {}).get("path") == (executor.root / "effect").as_uri()
                ]
                # A permitted restricted write needs no native escalation.
                assert len(writes) == 1 + len(native_approvals), executor.requests
                assert (
                    writes[0]["params"]["sandbox"]["permissions"]["file_system"]["type"]
                    == "restricted"
                )
                for attempt in writes:
                    assert b64decode(attempt["params"]["dataBase64"]) == (
                        b"accepted\n" if decision == "replace" else b"original\n"
                    )
                # Only the actual approval allowed native to retry this patch.
                if native_approvals:
                    assert writes[1]["params"]["sandbox"] is None
            assert model.connections == 1 and not model.http_requests and not model.errors
            assert "private-ws-reason" not in json.dumps(model.requests)


@pytest.mark.parametrize("case", ["joined", "delayed-receipt", "after-terminal"])
async def test_environment_ws_native_child_stop_is_owned_and_joined(tmp_path, monkeypatch, case):
    from tests import test_native_child_environment_stop as control
    from tests.test_native_child_agents import ChildPeer

    real_provider = ChildPeer.provider
    active = []
    executors = []

    class RecordedExecutor(ExecutorPeer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            executors.append(self)

    monkeypatch.setattr(control, "ExecutorPeer", RecordedExecutor)

    def on_websocket(self, **kwargs):
        # Reuse the three actual native ownership schedules with a WS-configured
        # provider. Verify the native transport below; no mocked native responses.
        active.append(self)
        return real_provider(self, websocket=True)

    monkeypatch.setattr(ChildPeer, "provider", on_websocket)
    if case == "joined":
        await control.test_environment_stop_joins_all_owned_yielded_cells_not_sibling_cells(
            tmp_path, monkeypatch
        )
    elif case == "delayed-receipt":
        await control.test_child_stop_waits_for_delayed_native_receipt_before_joining_cells(
            tmp_path, monkeypatch
        )
    else:
        await (
            control.test_environment_child_stop_after_real_native_terminal_prevents_unhooked_effect(
                tmp_path, monkeypatch
            )
        )
    assert active and len(executors) == 1
    forbidden = "must-not-exist" if case == "after-terminal" else "forbidden"
    assert not [
        request
        for request in executors[0].requests
        if request.get("method") == "process/start"
        and forbidden in str(request.get("params", {}).get("argv"))
    ], executors[0].requests
    for peer in active:
        assert peer.frames and not peer.http_requests
        assert len(peer.child_requests["alpha"]) > 1 and len(peer.child_requests["beta"]) > 1


@pytest.mark.parametrize("provider_id", [False, True], ids=["missing-id", "real-id"])
@pytest.mark.parametrize("tool", ["exec_command", "apply_patch"])
@pytest.mark.parametrize("websocket", [False, True], ids=["http", "ws"])
async def test_ws_environment_two_edits_unchanged_reuse_and_sibling(
    tmp_path, monkeypatch, provider_id, tool, websocket
):
    """Observe effective inputs at the provider, plus call and local history identity."""
    seen, approved = [], []

    def before(ctx, value):
        seen.append((ctx, value))
        if tool == "exec_command":
            content = value["cmd"]
        else:
            content = value
        if "original-" in content:
            replacement = content.replace("original-", "accepted-", 1)
            return ReplaceArguments({"cmd": replacement} if tool == "exec_command" else replacement)
        if "no-op" in content:
            return ReplaceArguments(value)
        return Allow()

    def approval(ctx, value):
        approved.append((ctx, value))
        return True

    async with (
        timeout(46),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        trust(executor, tmp_path, monkeypatch)
        model.respond(assistant_output("first warm"))
        model.respond(assistant_output("sibling warm"))
        shared = model.provider(websocket=websocket)
        agent = Agent(instructions="Use workspace commands", tools=[], provider=shared)
        descriptor = declaration(executor, transform_arguments=before, approval=approval)
        async with (
            agent.session(environment=descriptor) as session,
            agent.session(environment=descriptor) as sibling,
        ):
            assert (await (await session.submit("warm")).get_result()).final_output == "first warm"
            assert (
                await (await sibling.submit("warm")).get_result()
            ).final_output == "sibling warm"
            turns = []
            requests = []
            identities = []
            for current, label, original, effective in (
                (session, "edit-one", "original-one", "accepted-one"),
                (session, "edit-two", "original-two", "accepted-two"),
                (session, "no-edit", "unchanged", "unchanged"),
                (session, "no-op", "no-op", "no-op"),
                (sibling, "sibling", "live-sibling", "live-sibling"),
            ):
                name = "call-" + label
                if tool == "exec_command":
                    event = function_call(tool, {"cmd": "printf " + original}, name)
                    effective_input = {"cmd": "printf " + effective}
                    # Preserve provider whitespace for an equivalent replacement.
                    event["item"]["arguments"] = json.dumps({"cmd": "printf " + original}, indent=2)
                else:
                    # A valid update to a nonexistent file reaches Core's patch
                    # verifier and returns its real error without an OS grant.
                    # This exercises continuation even on a sandboxed host.
                    original_patch = (
                        f"*** Begin Patch\n*** Update File: {label}\n"
                        f"@@\n-old\n+{original}\n*** End Patch"
                    )
                    event = {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "custom_tool_call",
                            "name": tool,
                            "call_id": name,
                            "input": original_patch,
                        },
                    }
                    effective_input = original_patch.replace(original, effective)
                if provider_id:
                    event["item"]["id"] = ("fc_" if tool == "exec_command" else "ctc_") + label
                added = {"type": "response.output_item.added", "item": {**event["item"]}}
                added["item"]["arguments" if tool == "exec_command" else "input"] = ""
                model.respond(added, event)
                model.respond(assistant_output(label + " done"))
                turn = await current.submit("do " + label)
                turns.append(turn)
                assert (await turn.get_result()).final_output == label + " done"
                assert approved[-1][0].submission is turn
                assert approved[-1][1] == (
                    {"cmd": "printf " + original} if tool == "exec_command" else original_patch
                )
                assert (approved[-1][0].thread_id, approved[-1][0].turn_id) == (
                    current.id,
                    turn.turn_id,
                )
                history = model.requests[-1]["input"]
                item = next(
                    i
                    for i in history
                    if i.get("call_id") == name
                    and i.get("type") in ("function_call", "custom_tool_call")
                )
                received = (
                    json.loads(item["arguments"]) if tool == "exec_command" else item["input"]
                )
                # Collect all edits, so a failed comparison cannot hide the next response.
                requests.append((label, received, effective_input, item))
                assert any(
                    i.get("call_id") == name
                    and i.get("type") in ("function_call_output", "custom_tool_call_output")
                    for i in history
                )
                if tool == "exec_command":
                    starts = [
                        r
                        for r in executor.requests
                        if r.get("method") == "process/start"
                        and r.get("params", {}).get("metadata", {}).get("toolCallId") == name
                    ]
                    assert len(starts) == 1
                    argv = starts[0]["params"]["argv"]
                    assert argv[-1] == _expected_executor_script(argv, effective_input["cmd"])
                else:
                    writes = [
                        r
                        for r in executor.requests
                        if r.get("method") == "fs/writeFile"
                        and (r.get("params") or {}).get("path") == (executor.root / label).as_uri()
                    ]
                    assert not writes
                    assert not (executor.root / label).exists()
                state = await current.save_state()
                saved = next(
                    row["payload"]
                    for row in state.history
                    if row["type"] == "response_item"
                    and row["payload"].get("call_id") == name
                    and row["payload"].get("type") in ("function_call", "custom_tool_call")
                )
                public = [
                    i
                    async for i in current.items()
                    if i.get("call_id") == name
                    and i.get("type") in ("function_call", "custom_tool_call")
                ]
                assert len(public) == 1
                assert public[0]["id"] == saved["id"] == item["id"]
                assert public[0]["call_id"] == saved["call_id"] == item["call_id"]
                assert (
                    json.loads(public[0]["arguments"])
                    if tool == "exec_command"
                    else public[0]["arguments"]
                ) == effective_input
                assert saved["call_id"] == approved[-1][0].call_id == name
                assert saved["id"]
                if provider_id:
                    if original == effective:
                        assert saved["id"] == event["item"]["id"]
                        if tool == "exec_command":
                            assert item["arguments"] == event["item"]["arguments"]
                        if websocket:
                            assert model.requests[-1].get("previous_response_id"), model.frames
                    else:
                        identities.append((label, saved["id"], event["item"]["id"]))
                        assert saved["id"].startswith("fc_" if tool == "exec_command" else "ctc_")
                        if websocket:
                            assert not model.requests[-1].get("previous_response_id"), model.frames
            assert [c.submission for c, _ in seen] == turns
            assert [c.submission for c, _ in approved] == turns
            assert not [
                (label, got, want, item) for label, got, want, item in requests if got != want
            ]
            assert not [
                (label, saved, given) for label, saved, given in identities if saved == given
            ]
            if websocket:
                assert model.connections == 2 and not model.http_requests and not model.errors
            else:
                assert model.connections == 0 and len(model.http_requests) == len(model.requests)


@pytest.mark.parametrize("phase", ["before", "approval"])
async def test_ws_cancel_during_real_id_replacement_joins_before_successor(
    tmp_path, monkeypatch, phase
):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    async def before(ctx, value):
        assert ctx.call_id == "held-call"
        if phase == "before":
            await hold()
        return ReplaceArguments({"cmd": "printf unapproved-value > forbidden"})

    async def approval(ctx, value):
        assert value == {"cmd": "printf original > forbidden"}
        if phase == "approval":
            await hold()
        return True

    async with (
        timeout(35),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        trust(executor, tmp_path, monkeypatch)
        event = function_call("exec_command", {"cmd": "printf original > forbidden"}, "held-call")
        event["item"]["id"] = "fc_heldProviderItem"
        model.respond(
            {
                "type": "response.output_item.added",
                "item": {**event["item"], "arguments": ""},
            },
            event,
        )
        agent = Agent(instructions="Use environment tools.", tools=[], provider=model.provider())
        async with agent.session(
            environment=declaration(executor, transform_arguments=before, approval=approval)
        ) as session:
            turn = await session.submit("Run command")
            stopping = None
            try:
                await entered.wait()
                stopping = asyncio.create_task(turn.cancel())
                await cancelled.wait()
                assert not stopping.done()
            finally:
                release.set()
                if stopping is not None:
                    await stopping
            assert len(model.requests) == 1
            assert not [
                r
                for r in executor.requests
                if r.get("method") == "process/start" and "forbidden" in str(r.get("params"))
            ]
            assert not (executor.root / "forbidden").exists()
            model.respond(assistant_output("successor"))
            successor = await session.submit("Continue")
            assert (await successor.get_result()).final_output == "successor"
            assert len(model.requests) == 2
            assert "unapproved-value" not in json.dumps(model.requests)
            assert not model.http_requests and not model.errors
