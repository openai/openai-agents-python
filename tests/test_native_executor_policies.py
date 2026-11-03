"""Selected remote tools pass through policy before generated code consumers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ChildInvocation,
    Environment,
    ExecutorContext,
    HttpMCP,
    MCPResultContext,
    NativeAgents,
    NativeError,
    NativeRole,
    ReplaceArguments,
    function_tool,
)
from agents.observation import ExecutionItemCompleted
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import captured_turn, child_peer, collaboration, handles, trust
from tests.test_native_child_tool_integration import PendingChildCallsPeer
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize("kind", ["exec_command", "apply_patch"])
@pytest.mark.parametrize("result_kind", ["raw", "tagged"])
@pytest.mark.parametrize(
    "permit", [False, pytest.param(True, marks=pytest.mark.requires_native_linux_sandbox)]
)
async def test_direct_executor_arguments_are_rewritten_before_one_effect(
    responses: ResponsesFixture, tmp_path, monkeypatch, kind, result_kind, permit
) -> None:
    contexts: list[ExecutorContext[None]] = []
    original: Any = (
        {"cmd": "printf original >> effect"}
        if kind == "exec_command"
        else "*** Begin Patch\n*** Add File: effect\n+original\n*** End Patch"
    )
    accepted: Any = (
        {"cmd": "printf accepted >> effect; printf accepted-output"}
        if kind == "exec_command"
        else "*** Begin Patch\n*** Add File: effect\n+accepted\n*** End Patch"
    )

    approved: list[ExecutorContext[None]] = []

    def approval(ctx: ExecutorContext[None], value: Any) -> bool:
        approved.append(ctx)
        assert value == original
        return permit

    async def arguments(ctx: ExecutorContext[None], value: Any) -> Any:
        contexts.append(ctx)
        assert value == original
        assert ctx.call_id == "direct"
        assert ctx.outer_call_id is ctx.cell_id is ctx.runtime_tool_call_id is None
        return ReplaceArguments(accepted) if result_kind == "tagged" else accepted

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        selected = declaration(
            peer,
            approval=approval,
            transform_arguments=arguments,
        )
        event = (
            function_call(kind, original, "direct")
            if kind == "exec_command"
            else {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "name": kind,
                    "call_id": "direct",
                    "input": original,
                },
            }
        )
        responses.respond(event)
        responses.respond(assistant_output("done"))
        async with Agent(instructions="Use tools.", tools=[], provider=provider(responses)).session(
            environment=selected
        ) as session:
            handle = await session.submit("Run the selected tool.")
            if not permit:
                with pytest.raises(
                    NativeError, match="Native tool interception failed or was denied"
                ):
                    await handle.get_result()
                assert len(approved) == 1 and approved[0].submission is handle
                assert not contexts
                assert not (peer.root / "effect").exists()
                assert not any(
                    req.get("method") in {"process/start", "fs/writeFile"} and "effect" in str(req)
                    for req in peer.requests
                )
                assert len(responses.requests) == 1
                fresh = await session.submit("Continue after the denied call.")
                assert (await fresh.get_result()).final_output == "done"
                return
            assert (await handle.get_result()).final_output == "done"
            assert len(approved) == 1 and approved[0].submission is handle
            assert len(contexts) == 1
            assert contexts[0].submission is handle
            assert contexts[0].thread_id == session.id
            assert contexts[0].turn_id == handle.turn_id
            assert contexts[0].agent_role is None
            assert contexts[0].model == responses.requests[0]["model"]
            assert (peer.root / "effect").read_text().strip() == "accepted"
            if kind == "exec_command":
                assert "accepted-output" in tool_output(responses.requests[1], "direct")


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("apply_patch", marks=pytest.mark.requires_native_linux_sandbox),
        "write_stdin",
    ],
)
@pytest.mark.parametrize("transform_expires", [False, True])
async def test_environment_approval_wait_keeps_transform_deadline_independent(
    responses: ResponsesFixture, tmp_path, monkeypatch, transform_expires, operation
) -> None:
    stages: list[str] = []
    contexts: list[ExecutorContext[None]] = []
    original: Any = (
        "*** Begin Patch\n*** Add File: effect\n+original\n*** End Patch"
        if operation == "apply_patch"
        else {"session_id": 731031}
    )
    accepted: Any = (
        "*** Begin Patch\n*** Add File: effect\n+approved-transform\n*** End Patch"
        if operation == "apply_patch"
        else {"session_id": 731032}
    )

    async def approval(ctx: ExecutorContext[None], value: Any) -> bool:
        assert ctx.tool_name == operation
        assert value == original
        stages.append("approval")
        contexts.append(ctx)
        # Waiting for the application takes longer than the transform budget.
        await asyncio.sleep(0.5)
        return True

    async def transform(ctx: ExecutorContext[None], value: Any) -> Any:
        assert value == original
        stages.append("transform")
        contexts.append(ctx)
        if transform_expires:
            await asyncio.Future()
        # The transform has its own deadline after approval.
        return ReplaceArguments(accepted)

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        selected = declaration(
            peer,
            approval=approval,
            transform_arguments=transform,
            policy_timeout=0.25,
        )
        event = (
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "name": operation,
                    "call_id": "approval-transform",
                    "input": original,
                },
            }
            if operation == "apply_patch"
            else function_call(operation, original, "approval-transform")
        )
        responses.respond(event)
        responses.respond(assistant_output("done"))
        async with Agent(instructions="Use tools.", tools=[], provider=provider(responses)).session(
            environment=selected
        ) as session:
            handle = await session.submit("Run the selected tool.")
            if transform_expires:
                with pytest.raises(NativeError, match="Executor policy failed"):
                    await handle.get_result()
                assert not (peer.root / "effect").exists()
                assert len(responses.requests) == 1
            else:
                assert (await handle.get_result()).final_output == "done"
                if operation == "apply_patch":
                    assert (peer.root / "effect").read_text().strip() == "approved-transform"
                else:
                    # Core's real handler consumes the approved replacement.
                    # No process exists, so this portable case launches no shell.
                    output = tool_output(responses.requests[1], "approval-transform")
                    assert "Unknown process id 731032" in output
                    assert "731031" not in output
            assert stages == ["approval", "transform"]
            assert len(contexts) == 2 and contexts[0] is contexts[1]
            assert all(context.submission is handle for context in contexts)
            assert all(context.session is session for context in contexts)
            assert all(context.thread_id == session.id for context in contexts)
            assert all(context.turn_id == handle.turn_id for context in contexts)
            if operation == "write_stdin":
                assert not any(
                    request.get("method") == "process/start" for request in peer.requests
                )


@pytest.mark.requires_native_linux_sandbox
async def test_native_command_argument_rewrite_runs_once_and_code_uses_accepted_result(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    stages = []

    async def arguments(ctx: ExecutorContext[None], value: Any) -> Any:
        assert ctx.tool_name == "exec_command"
        assert value["cmd"] == "printf model-request"
        stages.append("arguments")
        return ReplaceArguments({"cmd": "cat private.txt; printf effect >> single-effect"})

    async def result(ctx: ExecutorContext[None], value: Any) -> Any:
        assert ctx.tool_name == "exec_command"
        assert "remote-private-8115" in json.dumps(value)
        stages.append("result")
        return {"output": "accepted-command-value"}

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        (peer.root / "private.txt").write_text("remote-private-8115")
        selected = declaration(
            peer,
            transform_arguments=arguments,
            transform_result=result,
        )
        responses.respond(
            code_call(
                'const result=await tools.exec_command({cmd:"printf model-request"});'
                'text("value:" + result.output);',
                "command-code",
            )
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Run one command via code.",
            tools=[],
            provider=provider(responses),
            programmatic_tool_calling=True,
        ).session(environment=selected) as session:
            captured = OutputEvents(session._native)
            session._native = captured
            handle = await session.submit("Run the command.")
            assert (await (handle).get_result()).final_output == "done"
            history = json.loads(await captured.read_thread_history(None))
            assert "remote-private-8115" not in json.dumps([history, captured.events])
            output = code_output(responses.requests[1], "command-code")
            assert "accepted-command-value" in output
            assert "remote-private-8115" not in output
        assert stages == ["arguments", "result"]
        assert (peer.root / "single-effect").read_text() == "effect"


@pytest.mark.requires_native_linux_sandbox
async def test_real_patch_arguments_and_result_are_replaced_before_code_receives_them(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    identities: list[ExecutorContext[None]] = []
    original = "*** Begin Patch\n*** Add File: artifact.xml\n+unaccepted-original\n*** End Patch"
    accepted = "*** Begin Patch\n*** Add File: artifact.xml\n+<presentation />\n*** End Patch"

    async def arguments(ctx: ExecutorContext[None], value: Any) -> Any:
        identities.append(ctx)
        assert ctx.tool_name == "apply_patch"
        assert isinstance(value, str)
        assert value == original
        return ReplaceArguments(accepted)

    async def result(ctx: ExecutorContext[None], value: Any) -> Any:
        identities.append(ctx)
        assert ctx.tool_name == "apply_patch"
        assert value is not None
        return {"authorized": "patched-through-policy"}

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        selected = declaration(peer, transform_arguments=arguments, transform_result=result)
        responses.respond(
            code_call(
                "const r=await tools.apply_patch(" + json.dumps(original) + ");"
                'text("PATCH-RESULT:" + JSON.stringify(r));',
                "patch-code",
            )
        )
        responses.respond(assistant_output("patched"))
        async with Agent(
            instructions="Patch using code mode.",
            tools=[],
            provider=provider(responses),
            programmatic_tool_calling=True,
        ).session(environment=selected) as session:
            handle = await session.submit("Produce artifact.")
            assert (await (handle).get_result()).final_output == "patched"
            assert (peer.root / "artifact.xml").read_text() == "<presentation />\n"
            output = code_output(responses.requests[1], "patch-code")
            assert "patched-through-policy" in output
            assert "unaccepted-original" not in output
            assert len(identities) == 2
            before, after = identities
            assert before.submission is after.submission is handle
            assert before.thread_id == after.thread_id == session.id
            assert before.turn_id == after.turn_id == handle.turn_id
            assert before.outer_call_id == after.outer_call_id == "patch-code"
            assert before.cell_id == after.cell_id
            assert before.runtime_tool_call_id == after.runtime_tool_call_id
            assert before.cell_id and before.runtime_tool_call_id
        assert peer.token not in repr(selected)


@pytest.mark.requires_native_linux_sandbox
async def test_invalid_remote_xml_requests_real_repair_without_replaying_patch(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    checked: list[str] = []
    malformed = (
        "*** Begin Patch\n*** Add File: presentation.xml\n"
        "+<presentation><slide></presentation>\n*** End Patch"
    )
    repair = (
        "*** Begin Patch\n*** Update File: presentation.xml\n"
        "@@\n-<presentation><slide></presentation>\n"
        "+<presentation><slide>Validated.</slide></presentation>\n*** End Patch"
    )
    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))

        async def validate(ctx: ExecutorContext[None], value: Any) -> Any:
            assert ctx.tool_name == "apply_patch"
            # This application owns the ExecutorPeer fixture and its remote
            # filesystem. The SDK does not read an arbitrary local workspace.
            path = peer.root / "presentation.xml"
            checked.append(path.read_text())
            try:
                root = ElementTree.parse(path).getroot()
            except ElementTree.ParseError as error:
                return {"error": f"Repair invalid presentation XML at {error.position}."}
            if root.tag != "presentation" or root.find("slide") is None:
                return {"error": "Repair: presentation requires a slide."}
            return {"validated": True}

        selected = declaration(peer, transform_result=validate)
        responses.respond(
            code_call(
                "text(JSON.stringify(await tools.apply_patch(" + json.dumps(malformed) + ")));",
                "invalid-xml",
            )
        )
        responses.respond(
            code_call(
                "text(JSON.stringify(await tools.apply_patch(" + json.dumps(repair) + ")));",
                "repair-xml",
            )
        )
        responses.respond(assistant_output("Validated presentation ready."))
        saw_rejection = False

        async def before(event):
            nonlocal saw_rejection
            if event["type"] == "response.created" and len(responses.requests) == 2:
                assert "Repair invalid presentation XML" in code_output(
                    responses.requests[-1], "invalid-xml"
                )
                assert checked == ["<presentation><slide></presentation>\n"]
                saw_rejection = True

        responses.before_event = before
        async with Agent(
            instructions="Repair invalid artifacts.",
            tools=[],
            provider=provider(responses),
            programmatic_tool_calling=True,
        ).session(environment=selected) as session:
            turn = await session.submit("Publish valid presentation XML.")
            assert (await (turn).get_result()).final_output == "Validated presentation ready."
        assert saw_rejection
        assert checked == [
            "<presentation><slide></presentation>\n",
            "<presentation><slide>Validated.</slide></presentation>\n",
        ]
        assert "validated" in code_output(responses.requests[-1], "repair-xml")
        assert ElementTree.parse(peer.root / "presentation.xml").getroot().find("slide").text == (
            "Validated."
        )


class OutputEvents:
    """Tee real native events without replacing dispatch or transport."""

    def __init__(self, native):
        self.native = native
        self.events = []

    def __getattr__(self, name):
        return getattr(self.native, name)

    async def _next_session_event(self):
        event = await self.native._next_session_event()
        self.events.append(json.loads(event))
        return event


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("namespace", [None, "", "functions"])
@pytest.mark.parametrize("kind", ["exec_command", "apply_patch"])
async def test_direct_executor_result_is_accepted_before_native_consumers(
    responses: ResponsesFixture, tmp_path, monkeypatch, namespace, kind
) -> None:
    contexts = []
    raw = "remote-private-8115"

    async def policy(ctx: ExecutorContext[None], value: Any) -> Any:
        contexts.append(ctx)
        assert ctx.tool_name == kind
        if kind == "exec_command":
            assert raw in json.dumps(value)
        return {"accepted": "public-result"}

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        (peer.root / "private.txt").write_text(raw)
        selected = declaration(peer, transform_result=policy)
        filename = "direct-effect.txt"
        if kind == "exec_command":
            event = function_call(
                kind, {"cmd": "cat private.txt; printf effect >> " + filename}, "direct"
            )
        else:
            event = {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "name": kind,
                    "call_id": "direct",
                    "input": f"*** Begin Patch\n*** Add File: {filename}\n+effect\n*** End Patch",
                },
            }
        if namespace is not None:
            event["item"]["namespace"] = namespace
        responses.respond(event)
        responses.respond(assistant_output("done"))
        # Direct result control also works when code mode was never enabled.
        async with Agent(instructions="Use tools.", tools=[], provider=provider(responses)).session(
            environment=selected
        ) as session:
            captured = OutputEvents(session._native)
            session._native = captured
            turn = await session.submit("Call the selected tool.")
            assert (await turn.get_result()).final_output == "done"
            history = json.loads(await captured.read_thread_history(None))
            output_kind = (
                "custom_tool_call_output" if kind == "apply_patch" else "function_call_output"
            )
            assert "public-result" in tool_output(responses.requests[1], "direct", output_kind)
            assert raw not in json.dumps([captured.events, history, responses.requests])
            assert session.capabilities.get("environment_code_result_policy").status == "supported"
        assert len(contexts) == 1
        assert contexts[0].submission is turn
        assert contexts[0].call_id == "direct"
        assert (peer.root / filename).read_text().strip() == "effect"


@pytest.mark.parametrize(
    "replacement",
    [{"cmd": None}, "invalid-arguments", {"cmd": "touch rejected-effect", "action": "deny"}],
)
@pytest.mark.parametrize("result_kind", ["raw", "tagged"])
async def test_invalid_direct_executor_policy_replacement_never_executes(
    responses: ResponsesFixture, tmp_path, monkeypatch, replacement, result_kind
) -> None:
    calls = 0

    async def arguments(ctx: ExecutorContext[None], value: Any) -> Any:
        nonlocal calls
        calls += 1
        assert ctx.call_id == "invalid-direct"
        assert value == {"cmd": "printf forbidden > rejected-effect"}
        return ReplaceArguments(replacement) if result_kind == "tagged" else replacement

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        selected = declaration(peer, transform_arguments=arguments)
        responses.respond(
            function_call(
                "exec_command", {"cmd": "printf forbidden > rejected-effect"}, "invalid-direct"
            )
        )
        async with Agent(instructions="Use tools.", tools=[], provider=provider(responses)).session(
            environment=selected
        ) as session:
            handle = await session.submit("Run a command.")
            with pytest.raises(NativeError):
                await (handle).get_result()
        assert calls == 1
        assert not (peer.root / "rejected-effect").exists()
        assert not any("rejected-effect" in json.dumps(request) for request in peer.requests)
        assert len(responses.requests) == 1


async def test_unrelated_python_and_same_leaf_mcp_calls_do_not_enter_executor_policy(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    policies: list[str] = []
    python_effects: list[str] = []
    mcp = Peer(names=("exec_command",), label="ordinary-native-mcp")

    async def policy(ctx: ExecutorContext[None], value: Any) -> Any:
        policies.append(ctx.tool_name)
        return value

    @function_tool
    def lookup(key: str) -> str:
        python_effects.append(key)
        return "ordinary-python"

    async with (
        timeout(60),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        mcp.serve() as mcp_url,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        selected = declaration(peer, transform_result=policy)
        responses.respond(function_call("lookup", {"key": "direct-python"}, "local"))
        responses.respond(mcp_call("exec_command", "mcp__remote", "ordinary-mcp"))
        responses.respond(assistant_output("Unrelated tools worked."))
        async with Agent(
            instructions="Use declared tools.",
            tools=[lookup, HttpMCP(name="remote", url=mcp_url, tools=["exec_command"])],
            provider=provider(responses),
            programmatic_tool_calling=True,
        ).session(environment=selected) as session:
            turn = await session.submit("Use Python and MCP.")
            assert (await (turn).get_result()).final_output == "Unrelated tools worked."
        assert python_effects == ["direct-python"]
        assert len(mcp.calls) == 1
        assert policies == []


@pytest.mark.parametrize(
    ("phase", "programmatic_tool_calling"),
    [
        ("before", False),
        ("before", True),
        pytest.param("after", True, marks=pytest.mark.requires_native_linux_sandbox),
        pytest.param("after", False, marks=pytest.mark.requires_native_linux_sandbox),
    ],
)
async def test_cancel_waits_for_entered_executor_policy_and_discards_late_result(
    responses: ResponsesFixture, tmp_path, monkeypatch, phase, programmatic_tool_calling
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def late(ctx: ExecutorContext[None], value: Any) -> Any:
        nonlocal calls
        calls += 1
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return {"unaccepted": "late-executor-9410"}

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        selected = declaration(
            peer,
            **{"transform_arguments" if phase == "before" else "transform_result": late},
        )
        patch = "*** Begin Patch\n*** Add File: done-once\n+effect\n*** End Patch"
        responses.respond(
            code_call("text(await tools.apply_patch(" + json.dumps(patch) + "));", "late")
            if programmatic_tool_calling
            else {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": "late",
                    "input": patch,
                },
            }
        )
        async with Agent(
            instructions="Use the executor.",
            tools=[],
            provider=provider(responses),
            programmatic_tool_calling=programmatic_tool_calling,
        ).session(environment=selected) as session:
            first = await session.submit("Patch.")
            stopping = None
            try:
                await entered.wait()
                if phase == "after":
                    assert (peer.root / "done-once").read_text() == "effect\n"
                else:
                    assert not (peer.root / "done-once").exists()
                stopping = asyncio.create_task(first.cancel())
                await cancelled.wait()
                assert not stopping.done()
            finally:
                release.set()
                if stopping is not None:
                    await stopping
            assert len(responses.requests) == 1
            responses.respond(assistant_output("continued"))
            second = await session.submit("Continue without tools.")
            assert (await (second).get_result()).final_output == "continued"
            items = [item async for item in session.items()]
        assert calls == 1
        assert "late-executor-9410" not in json.dumps(responses.requests)
        assert "late-executor-9410" not in json.dumps(items)


class ChildExecutorPolicyPeer(PendingChildCallsPeer):
    def response(self) -> list[dict[str, Any]]:
        result = super().response()
        if result[0].get("item", {}).get("call_id") == "concurrent-child-calls":
            return [
                code_call(
                    "const read = tools.mcp__vault__read({key:'record'});"
                    "const command = tools.exec_command("
                    "{cmd:'printf forbidden > stopped-child-policy-effect-51c3'});"
                    "text(await Promise.all([read, command]));",
                    "concurrent-child-calls",
                )
            ]
        return result


async def test_stopped_child_executor_policy_retires_its_decision_before_deadline(
    tmp_path, monkeypatch
) -> None:
    # The native before-effect interceptor holds this command. Both supported
    # parallel tools must enter before the failure; this never needs a sandboxed
    # shell or grants permission to launch the unaccepted command.
    peer = ChildExecutorPolicyPeer()
    vault = Peer(names=("read",))
    effect = "stopped-child-policy-effect-51c3"
    mcp_entered, allow_failure = asyncio.Event(), asyncio.Event()
    exec_entered, exec_cancelled, exec_release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    exec_joined = asyncio.Event()
    executor_contexts: list[ExecutorContext[None]] = []

    async def arguments(ctx: ExecutorContext[None], value: Any) -> Any:
        assert isinstance(ctx.submission, ChildInvocation)
        assert ctx.tool_name == "exec_command" and effect in value["cmd"]
        executor_contexts.append(ctx)
        exec_entered.set()
        try:
            try:
                await exec_release.wait()
            except asyncio.CancelledError:
                # Real async cleanup can finish after stop. The original,
                # otherwise valid command must never become an approval.
                exec_cancelled.set()
                await exec_release.wait()
            return ReplaceArguments(value)
        finally:
            exec_joined.set()

    async def fail_read(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        assert isinstance(ctx.submission, ChildInvocation)
        mcp_entered.set()
        await allow_failure.wait()
        raise RuntimeError("private-executor-sibling-mcp-error-98df")

    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as vault_url,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use native children for the requested work.",
            model="gpt-6.1-sol",
            programmatic_tool_calling=True,
            provider=peer.provider(websocket=False),
            tools=[
                HttpMCP(
                    name="vault",
                    url=vault_url,
                    tools=["read"],
                    transform_result=fail_read,
                    transform_result_timeout=25,
                ),
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research", description="Read records", instructions="Read once."
                        )
                    ],
                    max_children=2,
                ),
            ],
        )
        try:
            async with agent.session(
                environment=declaration(
                    executor,
                    transform_arguments=arguments,
                    policy_timeout=25,
                )
            ) as session:
                root = await session.submit("Start alpha and beta")
                assert (await (root).get_result()).final_output == "root complete"
                async with timeout(8):
                    await asyncio.gather(
                        mcp_entered.wait(), exec_entered.wait(), peer.entered["beta"].wait()
                    )
                assert len(executor_contexts) == 1
                by_id = {turn.child_id: turn for turn in await handles(session, 2)}
                alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
                beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
                ctx = executor_contexts[0]
                assert (ctx.thread_id, ctx.turn_id) == (alpha.child_id, alpha.turn_id)
                assert effect not in json.dumps(executor.requests)
                assert not (executor.root / effect).exists()

                peer.action = collaboration(
                    "send_message",
                    {"target": "/root/alpha", "message": "QUEUED_EXEC_51C3: retain this input."},
                    "queued-message",
                )
                sent = await session.submit("Queue further input to alpha")
                assert (await (sent).get_result()).final_output == "root complete"
                waiting = asyncio.create_task((alpha).get_result())
                allow_failure.set()
                peer.release["beta"].set()
                assert (await (beta).get_result()).final_output == "beta complete"
                async with timeout(8):
                    await exec_cancelled.wait()
                assert not waiting.done(), "Wait must join the entered executor callback"
                assert not exec_joined.is_set()
                exec_release.set()
                # Eight seconds is shorter than either native 25-second policy
                # deadline. Stop must answer the decision it is discarding.
                async with timeout(8):
                    with pytest.raises(NativeError, match="^Native MCP result policy failed$"):
                        await waiting
                assert exec_joined.is_set()
                assert effect not in json.dumps(executor.requests)
                assert not (executor.root / effect).exists()

                peer.action = collaboration(
                    "followup_task",
                    {"target": "/root/alpha", "message": "Continue with the queued input."},
                    "wake-queued-message",
                )
                followed = await session.submit("Continue alpha")
                assert (await (followed).get_result()).final_output == "root complete"
                async with timeout(8):
                    current = await captured_turn(session, alpha.child_id)
                    while current is None or current.turn_id == alpha.turn_id:
                        await asyncio.sleep(0)
                        current = await captured_turn(session, alpha.child_id)
                    assert (await (current).get_result()).final_output == "alpha complete"
                assert current.instance_id == alpha.instance_id
                wire = json.dumps(peer.child_requests["alpha"])
                assert "QUEUED_EXEC_51C3" in wire
                assert "private-executor-sibling-mcp-error-98df" not in wire
                assert len(executor_contexts) == 1
        finally:
            allow_failure.set()
            exec_release.set()
            peer.release["beta"].set()


@dataclass
class Account:
    name: str


@pytest.mark.parametrize("policy_name", ["transform_arguments", "transform_result"])
def test_executor_accepts_approval_combined_with_transform(policy_name: str) -> None:
    async def policy(ctx: ExecutorContext[None], value: Any) -> Any:
        return value

    selected = Environment(
        url="wss://127.0.0.1:1/executor",
        cwd="/remote",
        tools=["exec_command"],
        approval=lambda context, arguments: False,
        **{policy_name: policy},
    )
    assert getattr(selected, policy_name) is policy


async def test_executor_result_policy_checks_context_before_connect(
    responses: ResponsesFixture,
) -> None:
    async def needs_context(ctx: ExecutorContext[Account], value: Any) -> Any:
        return value

    descriptor = Environment(
        url="wss://127.0.0.1:1/executor",
        cwd="/remote",
        tools=["exec_command"],
        transform_result=needs_context,
    )
    with pytest.raises(TypeError, match="context"):
        async with Agent(instructions="Use tools.", tools=[], provider=provider(responses)).session(
            environment=descriptor
        ):
            pytest.fail("Connected executor with missing application context")
    assert responses.requests == []


@pytest.mark.parametrize("duration", [0, -1, True, float("nan"), float("inf"), 3601])
def test_executor_policy_timeout_must_fit_native_window(duration):
    with pytest.raises(ValueError, match="timeout"):
        Environment(
            url="wss://127.0.0.1:1/executor",
            cwd="/remote",
            tools=["apply_patch"],
            policy_timeout=duration,
        )


@pytest.mark.requires_native_linux_sandbox
async def test_result_policy_withholds_background_transcripts_and_stdin_output(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    private = "synthetic-output-private-50e2"
    calls = []

    async def replace(ctx: ExecutorContext[None], value: Any) -> Any:
        calls.append(ctx.tool_name)
        assert isinstance(value, dict)
        # Keep the native session id for the next real poll; replace output only.
        return {**value, "output": "accepted-poll-output"}

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        (peer.root / "private.txt").write_text(private)
        responses.respond(
            function_call(
                "exec_command",
                {
                    "cmd": "cat private.txt; read -r line; cat private.txt",
                    "tty": True,
                    "yield_time_ms": 1000,
                },
                "start",
            )
        )
        responses.respond(function_call("write_stdin", {}, "poll"))
        responses.respond(assistant_output("done"))

        async def supply_terminal(event):
            if event.get("item", {}).get("call_id") == "poll":
                accepted = json.loads(tool_output(responses.requests[-1], "start"))
                event["item"]["arguments"] = json.dumps(
                    {"session_id": accepted["session_id"], "chars": "\n", "yield_time_ms": 1000}
                )

        responses.before_event = supply_terminal
        async with Agent(
            instructions="Poll the process.", tools=[], provider=provider(responses)
        ).session(environment=declaration(peer, transform_result=replace)) as session:
            captured = OutputEvents(session._native)
            session._native = captured
            turn = await session.submit("Run and poll once.")
            assert (await turn.get_result()).final_output == "done"
            assert "accepted-poll-output" in tool_output(responses.requests[2], "poll")
            history = json.loads(await captured.read_thread_history(None))
        assert calls == ["exec_command", "write_stdin"]
        assert private not in json.dumps([captured.events, history, responses.requests])
        assert any(request.get("method") == "process/write" for request in peer.requests)


@pytest.mark.requires_native_linux_sandbox
async def test_failed_direct_result_policy_does_not_publish_original_output(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    private = "synthetic-private-failed-result-ae21"
    entered = []

    async def fail(ctx: ExecutorContext[None], value: Any) -> Any:
        entered.append(ctx.call_id)
        assert private in json.dumps(value)
        raise ValueError(private)

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        (peer.root / "private.txt").write_text(private)
        responses.respond(
            function_call(
                "exec_command", {"cmd": "cat private.txt; printf once >> effect"}, "direct-fail"
            )
        )
        async with Agent(instructions="Read once.", tools=[], provider=provider(responses)).session(
            environment=declaration(peer, transform_result=fail)
        ) as session:
            captured = OutputEvents(session._native)
            session._native = captured
            turn = await session.submit("Read the private file.")
            with pytest.raises(NativeError, match="Executor policy failed") as error:
                await turn.get_result()
            assert error.value.__cause__ is None
            assert error.value.__context__ is None
            history = json.loads(await captured.read_thread_history(None))
            [completion] = [
                event.item
                async for event in turn.events()
                if isinstance(event, ExecutionItemCompleted)
            ]
            assert completion["type"] == "command_execution"
            assert completion["id"] == "direct-fail"
            assert completion["output"] == "Native tool output withheld by result policy"
            assert [
                item async for item in session.items() if item["type"] == "command_execution"
            ] == [completion]
        assert entered == ["direct-fail"]
        assert (peer.root / "effect").read_text() == "once"
        assert len(responses.requests) == 1
        assert private not in json.dumps([captured.events, history, responses.requests])


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("websocket", [False, True])
async def test_native_child_direct_result_policy_keeps_the_child_owner(
    tmp_path, monkeypatch, websocket
) -> None:
    private = "synthetic-child-output-private-e105"
    contexts = []

    async def replace(ctx: ExecutorContext[None], value: Any) -> Any:
        assert isinstance(ctx.submission, ChildInvocation)
        assert private in json.dumps(value)
        contexts.append(ctx)
        return {"output": "accepted-child-output"}

    async with (
        timeout(60),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        (executor.root / "private.txt").write_text(private)
        peer.command = "cat " + str(executor.root / "private.txt")
        peer.release["alpha"].set()
        agent = Agent(
            instructions="Use a native child.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=websocket),
            tools=[NativeAgents(max_children=1)],
        )
        async with agent.session(
            environment=declaration(executor, transform_result=replace)
        ) as session:
            captured = OutputEvents(session._native)
            session._native = captured
            root = await session.submit("Start alpha")
            assert (await root.get_result()).final_output == "root complete"
            (child,) = await handles(session, 1)
            assert (await child.get_result()).final_output == "alpha complete"
            assert len(contexts) == 1
            context = contexts[0]
            assert (
                context.submission.session_id,
                context.submission.child_id,
                context.submission.turn_id,
            ) == (session.id, child.child_id, child.turn_id)
            assert (context.thread_id, context.turn_id) == (child.child_id, child.turn_id)
            assert "accepted-child-output" in json.dumps(peer.child_requests["alpha"])
            history = json.loads(await captured.read_thread_history(None))
        assert private not in json.dumps([history, captured.events, peer.child_requests])


@pytest.mark.requires_native_linux_sandbox
async def test_yielded_native_result_retains_original_call_after_native_compaction(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    private = "synthetic-yielded-private-7c14"
    contexts: list[ExecutorContext[None]] = []
    accepted = asyncio.Event()

    async def replace(ctx: ExecutorContext[None], value: Any) -> Any:
        assert private in json.dumps(value)
        contexts.append(ctx)
        accepted.set()
        return {"output": "accepted-after-compaction"}

    async with timeout(60), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        (peer.root / "private.txt").write_text(private)
        command = (
            "printf started > command-started; "
            "while [ ! -e release-command ]; do sleep 0.02; done; "
            "printf once >> completed-effect; cat private.txt"
        )
        start = code_call(
            "yield_control(); const result = await tools.exec_command("
            + json.dumps({"cmd": command, "yield_time_ms": 30000})
            + "); text(result.output);",
            "yielded-native-result",
        )
        start["item"]["id"] = "outer-native-result-item"
        responses.respond(start)
        responses.respond(assistant_output("source turn complete"))
        responses.respond(assistant_output("Native compacted summary without tool calls."))
        async with Agent(
            instructions="Run the native command through code.",
            model="gpt-5.5",
            tools=[],
            provider=provider(responses),
            programmatic_tool_calling=True,
        ).session(environment=declaration(peer, transform_result=replace)) as session:
            captured = OutputEvents(session._native)
            session._native = captured
            try:
                first = await session.submit("start the command")
                assert (await first.get_result()).final_output == "source turn complete"
                receipt = code_output(responses.requests[1], "yielded-native-result")
                assert receipt.startswith("Script running with cell ID "), receipt
                cell = receipt.removeprefix("Script running with cell ID ").split()[0].rstrip(".")
                while not (peer.root / "command-started").exists():
                    await asyncio.sleep(0.01)
                assert not accepted.is_set()
                await session.compact()
                assert not accepted.is_set()
                await session.update(model="gpt-5.2")
                (peer.root / "release-command").touch()
                responses.respond(
                    function_call("wait", {"cell_id": cell, "yield_time_ms": 10000}, "wait-origin")
                )
                responses.respond(assistant_output("settled after compaction"))
                later = await session.submit("collect the original cell")
                assert (await later.get_result()).final_output == "settled after compaction"
                assert "accepted-after-compaction" in tool_output(
                    responses.requests[4], "wait-origin"
                )
                current = responses.requests[3]["input"]
                assert "Native compacted summary without tool calls." in json.dumps(current)
                assert not any(item.get("call_id") == "yielded-native-result" for item in current)
                assert accepted.is_set() and len(contexts) == 1
                context = contexts[0]
                assert context.outer_call_id == "yielded-native-result"
                assert context.cell_id == cell and context.runtime_tool_call_id
                assert context.submission is first and context.session is session
                assert (context.thread_id, context.turn_id) == (session.id, first.turn_id)
                assert context.model == responses.requests[0]["model"] == "gpt-5.5"
                assert responses.requests[3]["model"] == "gpt-5.2"
                assert (peer.root / "completed-effect").read_text() == "once"
                assert (
                    sum(request.get("method") == "process/start" for request in peer.requests) == 1
                )
                history = json.loads(await captured.read_thread_history(None))
                assert private not in json.dumps([history, captured.events, responses.requests])
                assert len(responses.requests) == 5
                assert (await first.get_result()).final_output == "source turn complete"
            finally:
                (peer.root / "release-command").touch()
