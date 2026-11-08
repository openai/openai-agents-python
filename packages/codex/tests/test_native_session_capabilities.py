"""Configuration reports must agree with the real admitted roster and never grant tools."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import aclosing
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Any, cast, get_args

import pytest

from agents import (
    Agent,
    ApprovalRequest,
    Capabilities,
    CapabilityError,
    Checklist,
    ExecutorContext,
    FeatureName,
    FeatureSupport,
    FunctionTool,
    HttpMCP,
    LocalStorage,
    NativeAgents,
    NativeProvider,
    PromptedOutput,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_environment_leases_checklist import remote, trust
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_stdio import records, stdio
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


def check_all(capabilities: Capabilities) -> dict[FeatureName, FeatureSupport]:
    values: dict[FeatureName, FeatureSupport] = {}
    for name in get_args(FeatureName):
        value = capabilities.get(name)
        assert value.reason and value.scope
        if value.status == "supported":
            capabilities.require(name)
        else:
            with pytest.raises(CapabilityError):
                capabilities.require(name)
        if value.status == "unsupported":
            with pytest.raises(CapabilityError):
                capabilities.require(name, allow_limited=True)
        else:
            capabilities.require(name, allow_limited=True)
        values[name] = value
    assert len(values) == 13
    return values


@(
    pytest.mark.requires_native_linux_sandbox
    if sys.platform == "linux"
    else pytest.mark.requires_native_macos_sandbox
)
async def test_environment_code_result_report_matches_the_nested_return(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    received: list[tuple[ExecutorContext[None], Any]] = []

    async def after_code(ctx: ExecutorContext[None], value: Any) -> Any:
        received.append((ctx, value))
        return {"output": "policy-value"}

    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        environment = declaration(peer, transform_result=after_code)
        patch = "*** Begin Patch\n*** Add File: effect\n+native-write\n*** End Patch"
        responses.respond(
            code_call(
                "const r=await tools.apply_patch(" + json.dumps(patch) + ");"
                'text("cell-only:" + r.output);',
                "boundary-code",
            )
        )
        responses.respond(assistant_output("done"))
        borrowed = provider(responses)
        try:
            async with Agent(
                instructions="Check the tool result.",
                tools=[],
                provider=borrowed,
                programmatic_tool_calling=True,
            ).session(environment=environment) as session:
                feature = session.capabilities.get("environment_code_result_policy")
                assert feature.status == "supported"
                session.capabilities.require("environment_code_result_policy")
                session.capabilities.require("environment_code_result_policy", allow_limited=True)
                assert not responses.requests and not received
                turn = await session.submit("Apply one patch to the remote workspace.")
                native_approvals = []

                async def answer_native_approval() -> None:
                    async with aclosing(session.interactions()) as questions:
                        async for question in questions:
                            assert isinstance(question, ApprovalRequest)
                            assert question.subagent_id is None
                            assert question.submission_id == turn.submission_id
                            assert question.turn_id == turn.turn_id
                            assert not native_approvals
                            assert not (peer.root / "effect").exists()
                            native_approvals.append(question)
                            await question.respond("allow_once")

                # A permitted restricted write completes without a native question.
                reader = asyncio.create_task(answer_native_approval())
                try:
                    assert (await turn.get_result()).final_output == "done"
                finally:
                    reader.cancel()
                    outcome = (await asyncio.gather(reader, return_exceptions=True))[0]
                    if isinstance(outcome, BaseException) and not isinstance(
                        outcome, asyncio.CancelledError
                    ):
                        raise outcome
                assert len(received) == 1
                ctx, value = received[0]
                assert ctx.submission is turn
                assert ctx.tool_name == "apply_patch"
                assert ctx.outer_call_id == "boundary-code"
                assert value == {}  # The native patch return, before replacement and before JS.
                outer = code_output(responses.requests[1], "boundary-code")
                assert "cell-only:policy-value" in outer
                assert (peer.root / "effect").read_text() == "native-write\n"
                writes = [
                    request
                    for request in peer.requests
                    if request.get("method") == "fs/writeFile"
                    and (request.get("params") or {}).get("path") == (peer.root / "effect").as_uri()
                ]
                assert len(writes) == 1 + len(native_approvals), peer.requests
                assert writes[0]["params"]["sandbox"]["permissions"]["file_system"]["type"] == (
                    "restricted"
                )
                if native_approvals:
                    assert writes[1]["params"]["sandbox"] is None
                assert "native-write" not in outer
                # Strict filesystem writes launch the remote host's native sandbox
                # helper even though this route sends no process/start RPC.
                assert not any(
                    request.get("method") == "process/start" for request in peer.requests
                )
                assert feature == FeatureSupport(
                    "supported",
                    "Controls selected executor results before native consumers",
                    "Direct and code calls; raw output events withheld; effects remain",
                )
        finally:
            await borrowed.aclose()


@pytest.mark.parametrize("websockets", [False, True])
async def test_current_outputs_and_transport_do_not_start_model_work(
    responses: ResponsesFixture, websockets: bool
) -> None:
    borrowed = replace(provider(responses), supports_websockets=websockets)
    assert isinstance(borrowed, NativeProvider)
    async with timeout(20), Agent(instructions="Inspect.", provider=borrowed).session() as session:
        capabilities = session.capabilities
        assert capabilities.websockets_allowed is websockets
        values = check_all(capabilities)
        assert values["native_output"].status == values["prompted_output"].status == "supported"
        assert values["native_output"].scope == (
            "Settled manual restore requires a matching output declaration; "
            "native child Turns remain plain text"
        )
        assert values["native_output_manual_restore"].status == "supported"
        assert values["checklist"].status == values["native_agents"].status == "unsupported"
        assert values["function_policy"].status == values["manual_suspend"].status == "unsupported"
        with pytest.raises(ValueError, match="Unknown feature"):
            capabilities.get(cast(FeatureName, "private.invalid.secret"))
        with pytest.raises(ValueError, match="Unknown feature"):
            capabilities.require(cast(FeatureName, "native.outputs"))
        with pytest.raises(FrozenInstanceError):
            values["native_output"].reason = "changed"  # type: ignore[misc]
        assert not responses.requests
    with pytest.raises(RuntimeError, match="closed"):
        check_all(capabilities)
    await borrowed.aclose()


@pytest.mark.parametrize("websockets", [False, True])
async def test_current_stdio_selection_on_either_transport_is_passive(
    responses: ResponsesFixture, tmp_path: Path, websockets: bool
) -> None:
    log = tmp_path / "stdio"
    local = stdio(log)
    borrowed = replace(provider(responses), supports_websockets=websockets)
    async with (
        timeout(30),
        aclosing(borrowed),
        Agent(instructions="Inspect.", provider=borrowed, tools=[local]).session() as session,
    ):
        report = session.capabilities
        before = records(log)
        assert not before
        for _ in range(3):
            assert report.websockets_allowed is websockets
            values = check_all(report)
            assert values["mcp_before_policy"].status == "unsupported"
            assert values["mcp_result_policy"].status == "unsupported"
        assert records(log) == before and not responses.requests

        responses.respond(assistant_output("selected"))
        turn = await session.submit("Select opening defaults")
        assert (await turn.get_result()).final_output == "selected"
        before = records(log)
        request_count = len(responses.requests)
        assert before
        for _ in range(3):
            assert report.websockets_allowed is websockets
            values = check_all(report)
            assert values["mcp_before_policy"].status == "supported"
            assert values["mcp_result_policy"].status == "supported"
        assert records(log) == before and len(responses.requests) == request_count


async def test_live_view_tracks_only_published_tools_not_dormant_mcp_or_invalid_replacement(
    responses: ResponsesFixture,
) -> None:
    peer = Peer()
    effects: list[str] = []

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "original"

    async with timeout(30), peer.serve() as url:
        mcp = HttpMCP(name="docs", url=url, tools=["search"], bearer_token="synthetic-private")
        checklist = Checklist()
        roster = [lookup, mcp, checklist]
        borrowed = provider(responses)
        async with Agent(instructions="Inspect.", provider=borrowed, tools=roster).session() as s:
            view = s.capabilities
            for _ in range(3):
                initial = check_all(view)
                assert initial["checklist"].status == "supported"
                assert initial["function_policy"].status == "supported"
                assert initial["mcp_before_policy"].status == "unsupported"
                assert "synthetic-private" not in repr(initial)
                assert not peer.requests and not responses.requests and not effects
            assert view.get("function_policy").status == "supported"
            assert not peer.requests
            # This would activate MCP before the duplicated function is rejected if
            # admission and reporting used the candidate as their authority.
            with pytest.raises(ValueError, match="distinct"):
                await s.submit("invalid", tools=[lookup, lookup, mcp, checklist])
            assert view.get("mcp_before_policy").status == "unsupported"
            assert not peer.requests
            responses.respond(function_call("lookup", {}, "actual-function"))
            responses.respond(assistant_output("callback"))
            assert (
                await (await s.submit("Function", tools=[lookup, checklist])).get_result()
            ).final_output == "callback"
            assert effects == ["lookup"]
            responses.respond(mcp_call("search", "mcp__docs", "actual-mcp"))
            responses.respond(assistant_output("remote"))
            assert (
                await (await s.submit("MCP", tools=[mcp.tool("search"), checklist])).get_result()
            ).final_output == "remote"
            before = list(peer.requests)
            current = check_all(view)
            assert current["mcp_before_policy"].status == "supported"
            assert current["mcp_result_policy"].status == "supported"
            # Bound Python policies remain a supported ability; this operation
            # omitted their slots from its actual requests.
            assert current["function_policy"].status == "supported"
            assert peer.requests == before
            assert len(peer.calls) == 1
            responses.respond(assistant_output("fixed"))
            await (await s.submit("Only fixed tools", tools=[checklist])).get_result()
            assert view.get("mcp_result_policy").status == "unsupported"
            assert view.get("checklist").status == "supported"
        await borrowed.aclose()


@pytest.mark.parametrize("output", ["plain", "prompted"])
async def test_static_suspend_support_is_separate_from_real_boundary_and_destination_authority(
    responses: ResponsesFixture, output: str
) -> None:
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    borrowed = provider(responses)
    agent = Agent(
        instructions="Inspect.",
        tools=[manual, Checklist()],
        provider=borrowed,
        output_type=PromptedOutput(LookupArgs) if output == "prompted" else str,
    )
    async with timeout(30), agent.session() as s:
        view = s.capabilities
        assert view.get("manual_suspend").status == "limited"
        assert view.get("manual_suspend").scope == (
            "Settled native values are retained; active code and root before_step "
            "callbacks cannot suspend"
        )
        with pytest.raises(CapabilityError):
            view.require("manual_suspend")
        view.require("manual_suspend", allow_limited=True)
        assert (await s.checkpoint_status(kind="manual_wait")).status != "ready"
        responses.respond(function_call("lookup", {"key": "A"}, "original"))
        turn = await s.submit("read")
        request = await next_request(s.interactions(), manual)
        assert view.get("manual_suspend").status == "limited"
        saved = await s.suspend()
        with pytest.raises(RuntimeError, match="sealed"):
            view.get("manual_suspend")
        with pytest.raises(RuntimeError):
            await turn.get_result()
        with pytest.raises((ValueError, RuntimeError)):
            await request.respond("old")
    # Restore a real saved call with the current manual binding but no Checklist.
    async with agent.session(state=saved, tools=[manual]) as restored:
        current = check_all(restored.capabilities)
        assert current["checklist"].status == "unsupported"
        assert current["manual_suspend"].status == "limited"
        restored_turn = restored.restored_turn
        assert restored_turn is not None
        actual = await next_request(restored.interactions(), manual)
        responses.respond(assistant_output('{"key":"valid"}' if output == "prompted" else "valid"))
        await actual.respond("accepted")
        assert (await restored_turn.get_result()).final_output in ("valid", LookupArgs(key="valid"))
        assert ("functions", "update_plan") not in declarations(responses.requests[-1])
    await borrowed.aclose()


async def test_storage_cannot_export_even_when_manual_turn_would_otherwise_be_supported(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    borrowed = provider(responses)
    async with (
        timeout(25),
        Agent(instructions="Inspect.", tools=[manual], provider=borrowed).session(
            storage=LocalStorage(tmp_path / "profile")
        ) as session,
    ):
        report = check_all(session.capabilities)
        assert report["manual_suspend"].status == "unsupported"
        assert report["native_output_manual_restore"].status == "unsupported"
        with pytest.raises(CapabilityError, match="Persistent Sessions cannot export SessionState"):
            session.capabilities.require("native_output_manual_restore")
        with pytest.raises(ValueError, match="Persistent storage"):
            await session.suspend()
    await borrowed.aclose()


@pytest.mark.parametrize(
    "roots, status", [(None, "unsupported"), ((), "unsupported"), ("selected", "supported")]
)
async def test_destination_environment_roots_and_native_children_are_current_opening_grants(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    roots: tuple[()] | str | None,
    status: str,
) -> None:
    async with timeout(50), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        borrowed = provider(responses)
        source = remote(peer, peer.root)
        agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=borrowed)
        async with agent.session(environment=source) as session:
            assert session.capabilities.get("remote_skills").status == "supported"
            responses.respond(assistant_output("save"))
            await (await session.submit("source")).get_result()
            saved = await session.save_state()
        destination = replace(
            source, capability_directories=(str(peer.root),) if roots == "selected" else roots
        )
        async with agent.session(state=saved, environment=destination) as restored:
            report = check_all(restored.capabilities)
            assert report["remote_skills"].status == status
            assert report["native_agents"].status == "unsupported"
            assert report["environment_before_policy"].status == "unsupported"
        execution = replace(destination, tools=("exec_command", "write_stdin", "apply_patch"))
        with_children = replace(
            agent,
            tools=[*[t for t in agent.tools if not isinstance(t, NativeAgents)], NativeAgents()],
        )
        async with with_children.session(environment=execution) as tree:
            initial = (len(peer.transports), len(responses.requests))
            for _ in range(3):
                report = check_all(tree.capabilities)
                assert report["native_agents"].status == "supported"
                assert report["remote_skills"].status == status
                assert report["tool_selection"].status == "limited"
                assert report["tool_selection"].reason == (
                    "Root and native children select tools before each model step"
                )
                assert report["native_agents"].scope == (
                    "Model and instructions stay fixed; owned prompts require interactions=True"
                )
                assert report["environment_before_policy"].status == "supported"
                assert report["environment_code_result_policy"].status == "supported"
                assert (len(peer.transports), len(responses.requests)) == initial
        await borrowed.aclose()
