"""Configured children have been retaining their live parent and guard owners."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import traceback
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ApprovalRequested,
    Environment,
    GuardrailResult,
    InputGuardrail,
    InteractionExpiredError,
    InteractionRequired,
    NativeAgents,
    NativeProvider,
    NativeRole,
    NativeRoleTool,
    OutputGuardrail,
    PendingRequests,
    SignInWithChatGPT,
    ToolContext,
    ToolRef,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call

CATALOG = Path(__file__).parent / "fixtures/request-control-models.json"
ROLE_CATALOG = Path(__file__).parent / "fixtures/configured-agent-role-models.json"
MODEL = next(
    item["slug"]
    for item in json.loads(CATALOG.read_text())["models"]
    if item.get("supported_in_api") and not item.get("use_responses_lite", False)
)


@asynccontextmanager
async def peer():
    fixture = ResponsesFixture()
    app = web.Application()
    app.router.add_post("/v1/responses", fixture.handle)
    async with TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        yield fixture


@asynccontextmanager
async def fixture(*, sibling_approval=False, custom_output_extractor=None):
    async with peer() as root_model, peer() as child_model:
        root_model.respond(
            function_call(
                "nested_child", {"input": "request child approval"}, "nested-parent-call"
            ),
            function_call("accepted_sibling", {}, "held-sibling-call"),
        )
        root_model.respond(assistant_output("parent complete"))
        child_model.respond(function_call("child_effect", {}, "child-approval-call"))
        child_model.respond(assistant_output("child complete"))
        root_provider = NativeProvider(base_url=root_model.url, api_key="synthetic-parent")
        child_provider = NativeProvider(base_url=child_model.url, api_key="synthetic-child")
        effects = []
        guards = []
        sibling = asyncio.Event()
        approval_entered = asyncio.Event()
        application = {"tenant": "local"}

        def input_guard(name):
            def check(_turn, context, agent, _input):
                assert context is application
                guards.append((name, agent))
                return GuardrailResult(False)

            return InputGuardrail(name, check, run_in_parallel=False)

        def output_guard(name):
            def check(_request, agent):
                guards.append((name, agent))
                return GuardrailResult(False)

            return OutputGuardrail(name, check)

        def needs_approval(context):
            assert context.context is application
            assert context.call.tool.name == "child_effect"
            assert context.call.call_id == "child-approval-call"
            approval_entered.set()
            return True

        @function_tool(needs_approval=needs_approval)
        async def child_effect(context: ToolContext[dict[str, str]]) -> str:
            assert context.context is application
            effects.append("child-effect")
            return "child effect output"

        @function_tool(needs_approval=sibling_approval)
        async def accepted_sibling() -> str:
            effects.append("accepted-sibling")
            sibling.set()
            return "held sibling output"

        child = Agent[dict[str, str]](
            name="Nested guarded child",
            instructions="Request the child effect.",
            model=MODEL,
            provider=child_provider,
            tools=[child_effect],
            input_guardrails=[input_guard("child-input-owner")],
            output_guardrails=[output_guard("child-output-owner")],
        )
        try:
            parent = Agent[dict[str, str]](
                name="Guarded parent",
                instructions="Run the child and sibling.",
                model=MODEL,
                provider=root_provider,
                tools=[
                    child.as_tool(
                        tool_name="nested_child",
                        tool_description="Run the configured child.",
                        custom_output_extractor=custom_output_extractor,
                    ),
                    accepted_sibling,
                ],
                input_guardrails=[input_guard("parent-input-owner")],
                output_guardrails=[output_guard("parent-output-owner")],
            )
            yield parent, child, application, root_model, child_model, effects, guards, sibling
        finally:
            await child_provider.aclose()
            await root_provider.aclose()


@pytest.mark.skipif(sys.platform != "linux", reason="requires native Linux turn write policy")
@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("roots", ["opening", "clear", "replace"])
async def test_configured_child_has_been_inheriting_effective_turn_write_roots(tmp_path, roots):
    work, opening, replacement = (tmp_path / name for name in ("work", "opening", "replacement"))
    for directory in (work, opening, replacement):
        directory.mkdir()
    environment = Environment.local(cwd=work, writable_roots=[opening], tools=["exec_command"])
    async with timeout(60), peer() as root_model, peer() as child_model:
        root_provider = NativeProvider(base_url=root_model.url, api_key="synthetic-roots-parent")
        child_provider = NativeProvider(base_url=child_model.url, api_key="synthetic-roots-child")
        child = Agent(instructions="Run the write probe.", model=MODEL, provider=child_provider)
        parent = Agent(
            instructions="Run the configured child.",
            model=MODEL,
            provider=root_provider,
            tools=[child.as_tool("roots_child")],
        )
        try:
            async with parent.session(environment=environment) as session:
                for index in range(2):
                    paths = [
                        directory / f"child-{index}" for directory in (work, opening, replacement)
                    ]
                    command = "; ".join(
                        f"printf native-write > {shlex.quote(str(path))}" for path in paths
                    )
                    root_model.respond(
                        function_call("roots_child", {"input": "Run the probe."}, f"parent-{index}")
                    )
                    root_model.respond(assistant_output("parent complete"))
                    child_model.respond(
                        function_call(
                            "exec_command", {"cmd": command, "login": False}, f"write-{index}"
                        )
                    )
                    child_model.respond(assistant_output("child complete"))
                    overrides = {}
                    if index == 0 and roots != "opening":
                        overrides["writable_roots"] = [] if roots == "clear" else [replacement]
                    turn = await session.submit("Run the child write probe.", **overrides)
                    assert (await turn.get_result()).final_output == "parent complete"
                    assert paths[0].read_text() == "native-write"
                    assert paths[1].exists() is (roots == "opening")
                    assert paths[2].exists() is (roots == "replace")
                assert len(root_model.requests) == len(child_model.requests) == 4
        finally:
            await child_provider.aclose()
            await root_provider.aclose()


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="requires a Linux or macOS strict executor"
)
@pytest.mark.parametrize("roots", ["opening", "clear", "replace"])
async def test_configured_child_has_been_retaining_effective_wss_root_receipts(
    tmp_path, monkeypatch, native_executor_binary, roots
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust

    async with timeout(60), peer() as root_model, peer() as child_model:
        async with ExecutorPeer(
            tmp_path / "remote", synthetic_process_output="native probe"
        ).serve() as executor:
            trust(executor, tmp_path, monkeypatch)
            work, opening, replacement = (
                executor.root / name for name in ("work", "opening", "replacement")
            )
            for directory in (work, opening, replacement):
                directory.mkdir()
            opening_roots = [opening]
            if sys.platform == "darwin" and roots != "opening":
                opening_roots = [] if roots == "clear" else [replacement]
            environment = Environment(
                url=executor.url,
                headers={"Authorization": "Bearer " + executor.token},
                cwd=str(work),
                writable_roots=opening_roots,
                tools=["exec_command"],
            )
            child_receipts = []
            child_openings = []

            class InstrumentedAgent(Agent):
                @asynccontextmanager
                async def session(self, **options):
                    async with super().session(**options) as opened:
                        child_openings.append(
                            json.loads(opened._native_options)["runtime"]["environment"][
                                "writable_roots"
                            ]
                        )
                        yield opened

            async def extract(result):
                recorded = [
                    item
                    async for item in result.turn._session.turns()
                    if item["id"] == result.turn.turn_id
                ]
                child_receipts.append(recorded[0]["receipt"]["executors"][0]["writable_roots"])
                async with child.session(environment=environment):
                    assert set(child_openings[-1]) == {path.as_uri() for path in opening_roots}
                return result.final_output

            root_provider = NativeProvider(
                base_url=root_model.url, api_key="synthetic-roots-parent"
            )
            child_provider = NativeProvider(
                base_url=child_model.url, api_key="synthetic-roots-child"
            )
            child = InstrumentedAgent(
                instructions="Run the probe.", model=MODEL, provider=child_provider
            )
            parent = Agent(
                instructions="Run the child.",
                model=MODEL,
                provider=root_provider,
                tools=[child.as_tool("roots_child", custom_output_extractor=extract)],
            )
            try:
                async with parent.session(environment=environment) as session:
                    for index in range(2):
                        root_model.respond(
                            function_call(
                                "roots_child", {"input": "Run the probe."}, f"parent-{index}"
                            )
                        )
                        root_model.respond(assistant_output("parent complete"))
                        child_model.respond(
                            function_call(
                                "exec_command",
                                {"cmd": "printf native-probe", "login": False},
                                f"write-{index}",
                            )
                        )
                        child_model.respond(assistant_output("child complete"))
                        overrides = {}
                        if index == 0 and roots != "opening" and sys.platform != "darwin":
                            overrides["writable_roots"] = [] if roots == "clear" else [replacement]
                        turn = await session.submit("Run the child probe.", **overrides)
                        assert (await turn.get_result()).final_output == "parent complete"
                        recorded = [
                            item async for item in session.turns() if item["id"] == turn.turn_id
                        ]
                        parent_roots = recorded[0]["receipt"]["executors"][0]["writable_roots"]
                        print(
                            json.dumps(
                                {
                                    "mode": roots,
                                    "turn": index,
                                    "parent_roots": parent_roots,
                                    "child_roots": child_receipts[-1],
                                }
                            )
                        )
                        expected = {work.as_uri()}
                        if roots != "clear":
                            expected.add((opening if roots == "opening" else replacement).as_uri())
                        assert set(parent_roots) == expected
                        assert set(child_receipts[-1]) == expected
                        assert set(child_openings[2 * index]) == expected - {work.as_uri()}
                    assert len(executor.synthetic_process_starts) == 2
                    assert len(child_openings) == 4
                    assert len(root_model.requests) == len(child_model.requests) == 4
            finally:
                await child_provider.aclose()
                await root_provider.aclose()


@pytest.mark.parametrize(
    "selection",
    [
        "operation",
        "before_step",
        "environment",
        "partial",
        "forced",
        "callback_alias",
        "legacy",
        "default",
        "search",
        "search_narrow",
        "search_alias",
    ],
)
async def test_configured_child_has_been_preserving_issuing_executor_tools(
    tmp_path, monkeypatch, native_executor_binary, selection
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust

    def offered(rows):
        names = set()
        for row in rows:
            if row.get("type") == "namespace":
                names.update(offered(row["tools"]))
            elif row.get("name"):
                names.add(row["name"])
        return names

    async with timeout(45), peer() as root_model, peer() as child_model:
        async with ExecutorPeer(
            tmp_path / "remote", synthetic_process_output="executed"
        ).serve() as executor:
            trust(executor, tmp_path, monkeypatch)
            (executor.root / "search-hit.txt").write_text("owned search witness")
            search_case = selection in {"search", "search_narrow", "search_alias"}
            environment = Environment(
                url=executor.url,
                headers={"Authorization": "Bearer " + executor.token},
                cwd=str(executor.root),
                tools=["exec_command", "write_stdin", "apply_patch"]
                + (["project_glob", "project_grep"] if search_case else []),
                write_policy="legacy" if selection == "legacy" else "strict",
            )
            effects = []

            @function_tool
            def declared_child_tool() -> str:
                effects.append("declared-child-tool")
                return "child declaration executed"

            root_provider = NativeProvider(
                base_url=root_model.url, api_key="synthetic-ceiling-parent"
            )
            child_provider = NativeProvider(
                base_url=child_model.url, api_key="synthetic-ceiling-child"
            )
            child = Agent(
                instructions="Run the child declaration and executor probe.",
                model=MODEL,
                provider=child_provider,
                tools=[declared_child_tool],
            )
            child_tool = child.as_tool("ceiling_child")

            @function_tool(name="project_glob" if selection == "search_alias" else "exec_command")
            def callback_alias() -> str:
                effects.append("parent-callback-alias")
                return "application callback"

            parent = Agent(
                instructions="Run the child.",
                model=MODEL,
                provider=root_provider,
                tools=[child_tool, callback_alias]
                if selection in {"callback_alias", "search_alias"}
                else [child_tool],
                model_settings={"tool_choice": ToolRef("ceiling_child")}
                if selection == "forced"
                else None,
            )
            root_model.respond(
                function_call("ceiling_child", {"input": "Run the probes."}, "ceiling-parent")
            )
            root_model.respond(assistant_output("parent complete"))
            child_model.respond(function_call("declared_child_tool", {}, "child-declared"))
            if search_case:
                child_model.respond(
                    function_call("project_glob", {"pattern": "**/*.txt"}, "child-search")
                )
            child_model.respond(
                function_call(
                    "exec_command",
                    {"cmd": "printf executor-probe", "login": False},
                    "child-executor",
                )
            )
            child_model.respond(assistant_output("child complete"))
            try:
                if selection in {"callback_alias", "search_alias"}:
                    rejection = (
                        "reserved name: exec_command"
                        if selection == "callback_alias"
                        else "collides with enabled environment tool: project_glob"
                    )
                    with pytest.raises(ValueError, match=rejection):
                        async with parent.session(environment=environment):
                            pytest.fail("Reserved application callback has been admitted")
                    assert not root_model.requests and not child_model.requests
                    assert not effects and not executor.synthetic_process_starts
                    return
                async with parent.session(environment=environment) as session:
                    options = {"tools": [child_tool]}
                    if selection == "default":
                        options = {}
                    elif selection == "search":
                        options["tools"].append(ToolRef("project_glob", "functions"))
                    elif selection == "environment":
                        options["tools"].append(environment)
                    elif selection == "forced":
                        options["tools"].append(environment)
                    elif selection == "partial":
                        options["tools"].append(ToolRef("exec_command", "functions"))
                    elif selection == "before_step":
                        options = {"before_step": lambda *_args, **_kwargs: [child_tool]}
                    result = await (await session.submit("Run the child.", **options)).get_result()
                    assert result.final_output == "parent complete"
                    names = offered(child_model.requests[0]["tools"])
                    if selection == "forced":
                        assert offered(root_model.requests[0]["tools"]) == {"ceiling_child"}
                    expected = (
                        set(environment.tools)
                        if selection in {"environment", "default"}
                        else {"project_glob"}
                        if selection == "search"
                        else {"exec_command"}
                        if selection == "partial"
                        else set()
                    )
                    print(
                        json.dumps(
                            {
                                "selection": selection,
                                "offered_executor_tools": sorted(names & set(environment.tools)),
                                "executor_starts": len(executor.synthetic_process_starts),
                            }
                        )
                    )
                    assert effects == ["declared-child-tool"]
                    assert "declared_child_tool" in names
                    assert names & set(environment.tools) == expected
                    assert len(executor.synthetic_process_starts) == int("exec_command" in expected)
                    if search_case:
                        assert ("search-hit.txt" in json.dumps(child_model.requests[2])) is (
                            selection == "search"
                        )
            finally:
                await child_provider.aclose()
                await root_provider.aclose()


@pytest.mark.parametrize("approve", [True, False])
async def test_configured_child_approval_and_held_sibling_owners(approve):
    async with timeout(20), fixture() as values:
        parent, child, context, root_model, child_model, effects, guards, sibling = values
        async with (
            parent.session(context=context) as session,
            parent.session(context=context) as foreign,
        ):
            turn = await session.submit("begin joint witness")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            request = next(
                item for item in boundary.requests if isinstance(item, ApprovalRequested)
            )
            await sibling.wait()
            assert request.session_id != session.id
            assert request.call_id == "child-approval-call"
            assert request.name == request.call.tool.name == "child_effect"
            assert request.call.session_id == request.session_id
            assert request.call.call_id == request.call_id
            assert effects == ["accepted-sibling"]
            assert len(root_model.requests) == len(child_model.requests) == 1
            history = [item async for item in session.items()]
            assert not any(item["type"] == "function_call_output" for item in history)
            with pytest.raises(RuntimeError, match="manual"):
                await session.suspend()
            reply = request.reply(approve)
            with pytest.raises(InteractionExpiredError):
                await foreign.respond(reply)
            await session.respond(reply)
            with pytest.raises(InteractionExpiredError):
                await session.respond(reply)
            result = await turn.get_result()
            assert result.final_output == "parent complete"
            assert effects == (
                ["accepted-sibling", "child-effect"] if approve else ["accepted-sibling"]
            )
            assert guards == [
                ("parent-input-owner", parent),
                ("child-input-owner", child),
                ("child-output-owner", child),
                ("parent-output-owner", parent),
            ]
            assert [record.agent for record in result.guardrail_results] == [parent, parent]
            outputs = [
                item
                for item in root_model.requests[1]["input"]
                if item.get("type") == "function_call_output"
            ]
            assert [item["call_id"] for item in outputs] == [
                "nested-parent-call",
                "held-sibling-call",
            ]
            assert len(root_model.requests) == len(child_model.requests) == 2


async def test_configured_child_one_shot_without_handler_has_been_failing_promptly():
    async with timeout(20), fixture() as values:
        parent, _child, context, root_model, child_model, effects, _guards, _sibling = values
        with pytest.raises(InteractionRequired):
            await parent.run("begin joint witness", context=context)
        assert effects == ["accepted-sibling"]
        assert len(root_model.requests) == len(child_model.requests) == 1


async def test_configured_child_one_shot_handler_has_been_answering_its_child():
    async with timeout(20), fixture() as values:
        parent, _child, context, root_model, child_model, effects, _guards, _sibling = values
        seen = []

        async def answer(request):
            seen.append(request)
            return request.reply(True)

        result = await parent.run("begin joint witness", context=context, on_interaction=answer)
        assert result.final_output == "parent complete"
        assert [request.call_id for request in seen] == ["child-approval-call"]
        assert effects == ["accepted-sibling", "child-effect"]
        assert len(root_model.requests) == len(child_model.requests) == 2


@pytest.mark.parametrize("stop", ["close", "cancel"])
async def test_configured_child_parent_stop_has_been_joining_pending_child(stop):
    async with timeout(20), fixture() as values:
        parent, _child, context, root_model, child_model, effects, _guards, sibling = values
        async with parent.session(context=context) as session:
            turn = await session.submit("begin joint witness")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            request = boundary.requests[0]
            await sibling.wait()
            if stop == "close":
                await session.aclose()
            else:
                await turn.cancel()
            with pytest.raises((TurnStoppedError, asyncio.CancelledError)):
                await turn.get_result()
            with pytest.raises(InteractionExpiredError):
                await request.respond(True)
            assert effects == ["accepted-sibling"]
            assert len(root_model.requests) == len(child_model.requests) == 1


async def test_configured_child_extractor_has_been_receiving_the_child_result():
    extracted = []

    def extract(result):
        extracted.append(result)
        return "selected child output"

    async with timeout(20), fixture(custom_output_extractor=extract) as values:
        parent, child, context, root_model, _child_model, _effects, _guards, _sibling = values
        async with parent.session(context=context) as session:
            turn = await session.submit("begin joint witness")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            await session.respond(boundary.requests[0].reply(True))
            assert (await turn.get_result()).final_output == "parent complete"
            [result] = extracted
            assert result.final_output == "child complete"
            assert result.turn.session_id != session.id
            assert [record.agent for record in result.guardrail_results] == [child, child]
            output = next(
                item
                for item in root_model.requests[1]["input"]
                if item.get("call_id") == "nested-parent-call"
                and item.get("type") == "function_call_output"
            )
            assert "selected child output" in str(output["output"])


@pytest.mark.parametrize("graceful", [False, True])
async def test_configured_child_and_parent_have_been_sharing_the_existing_batch(graceful):
    from tests.test_review_ii_sessions import _GracefulReceiptObserver

    async with timeout(20), fixture(sibling_approval=True) as values:
        parent, _child, context, root_model, child_model, effects, _guards, _sibling = values
        async with parent.session(context=context) as session:
            turn = await session.submit("begin joint witness")
            cursor = session.interactions()
            ready = [await anext(cursor), await anext(cursor)]
            await cursor.aclose()
            assert {request.call_id for request in ready} == {
                "child-approval-call",
                "held-sibling-call",
            }
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            assert {request.call_id for request in boundary.requests} == {
                "child-approval-call",
                "held-sibling-call",
            }
            child_request = next(
                request for request in boundary.requests if request.session_id != session.id
            )
            parent_request = next(
                request for request in boundary.requests if request.session_id == session.id
            )
            stopping = None
            if graceful:
                probe = _GracefulReceiptObserver(session._native)
                session._native = probe
                stopping = asyncio.create_task(turn.cancel(mode="graceful"))
                await probe.prepared.wait()
                assert probe.installed
            await session.respond(parent_request.reply(not graceful))
            if not graceful:
                await _sibling.wait()
            await session.respond(child_request.reply(True))
            if stopping is not None:
                await stopping
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
                assert effects == ["child-effect"]
                assert len(root_model.requests) == 1
            else:
                assert (await turn.get_result()).final_output == "parent complete"
                assert effects == ["accepted-sibling", "child-effect"]
                assert len(root_model.requests) == 2
            assert len(child_model.requests) == 2


@pytest.mark.parametrize("stop", ["close", "cancel"])
async def test_configured_child_parent_stop_has_been_joining_credential_opening(stop):
    entered = asyncio.Event()
    retired = asyncio.Event()

    async def credentials():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            retired.set()

    async with timeout(20), peer() as model:
        model.respond(function_call("opening_child", {"input": "begin"}, "opening-call"))
        root_provider = NativeProvider(base_url=model.url, api_key="synthetic-opening-parent")
        child_provider = NativeProvider(auth=SignInWithChatGPT(get_credentials=credentials))
        child = Agent(instructions="Wait for credentials.", model=MODEL, provider=child_provider)
        parent = Agent(
            instructions="Run the declared child.",
            model=MODEL,
            provider=root_provider,
            tools=[child.as_tool(tool_name="opening_child")],
        )
        try:
            async with parent.session() as session:
                turn = await session.submit("begin opening witness")
                await entered.wait()
                if stop == "close":
                    await session.aclose()
                else:
                    await turn.cancel()
                assert retired.is_set()
                with pytest.raises((TurnStoppedError, asyncio.CancelledError)):
                    await turn.get_result()
                assert len(model.requests) == 1
        finally:
            await child_provider.aclose()
            await root_provider.aclose()


@pytest.mark.parametrize("operation", ["poll", "respond"])
async def test_retired_configured_child_has_been_losing_snapshot_authority(monkeypatch, operation):
    async with timeout(20), fixture() as values:
        parent, _child, context, _root_model, _child_model, effects, _guards, sibling = values
        async with parent.session(context=context) as session:
            turn = await session.submit("begin retirement witness")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            request = boundary.requests[0]
            child_session = request._session()
            assert child_session is not None
            await sibling.wait()
            if operation == "poll":
                entered = asyncio.Event()
                release = asyncio.Event()
                original = child_session.pending_interactions

                async def held_snapshot():
                    entered.set()
                    await release.wait()
                    return await original()

                monkeypatch.setattr(child_session, "pending_interactions", held_snapshot)
                polling = asyncio.create_task(session.pending_interactions())
                try:
                    await entered.wait()
                    await child_session.aclose()
                finally:
                    release.set()
                assert await polling == ()
            else:
                await child_session.aclose()
                with pytest.raises(InteractionExpiredError):
                    await session.respond(request.reply(True))
            assert effects == ["accepted-sibling"]
            [result] = await asyncio.gather(turn.get_result(), return_exceptions=True)
            assert isinstance(result, BaseException)


async def test_active_configured_child_has_been_preserving_snapshot_errors(monkeypatch):
    async with timeout(20), fixture() as values:
        parent, _child, context, _root_model, _child_model, effects, _guards, _sibling = values
        async with parent.session(context=context) as session:
            turn = await session.submit("begin active snapshot witness")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            request = boundary.requests[0]
            child_session = request._session()
            assert child_session is not None
            original = child_session.pending_interactions

            async def failed_snapshot():
                raise RuntimeError("active snapshot failure")

            monkeypatch.setattr(child_session, "pending_interactions", failed_snapshot)
            with pytest.raises(RuntimeError, match="active snapshot failure"):
                await session.pending_interactions()
            monkeypatch.setattr(child_session, "pending_interactions", original)
            await session.respond(request.reply(True))
            assert (await turn.get_result()).final_output == "parent complete"
            assert effects == ["accepted-sibling", "child-effect"]


async def test_configured_child_handler_cleanup_has_been_staying_joined_and_visible():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def answer(_request):
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise RuntimeError("configured handler cleanup failure") from None

    async with timeout(20), fixture() as values:
        parent, _child, context, _root_model, _child_model, effects, _guards, sibling = values
        async with parent.session(context=context, on_interaction=answer) as session:
            turn = await session.submit("begin joined cleanup witness")
            await entered.wait()
            await sibling.wait()
            closing = asyncio.create_task(session.aclose())
            try:
                await cancelled.wait()
                assert not closing.done()
            finally:
                release.set()
            results = await asyncio.gather(closing, turn.get_result(), return_exceptions=True)
            details = "\n".join(
                "".join(traceback.format_exception(error))
                for error in results
                if isinstance(error, BaseException)
            )
            assert "configured handler cleanup failure" in details
            assert effects == ["accepted-sibling"]


async def test_noninteractive_configured_child_has_been_completing_without_a_handler():
    async with timeout(20), peer() as root_model, peer() as child_model:
        root_model.respond(function_call("plain_child", {"input": "begin"}, "plain-call"))
        root_model.respond(assistant_output("parent complete"))
        child_model.respond(assistant_output("plain child complete"))
        root_provider = NativeProvider(base_url=root_model.url, api_key="synthetic-plain-parent")
        child_provider = NativeProvider(base_url=child_model.url, api_key="synthetic-plain-child")
        child = Agent(instructions="Answer directly.", model=MODEL, provider=child_provider)
        parent = Agent(
            instructions="Run the child.",
            model=MODEL,
            provider=root_provider,
            tools=[child.as_tool("plain_child")],
        )
        try:
            result = await parent.run("begin noninteractive witness")
            assert result.final_output == "parent complete"
            output = next(
                item
                for item in root_model.requests[1]["input"]
                if item.get("type") == "function_call_output"
            )
            assert "plain child complete" in str(output["output"])
            assert len(root_model.requests) == 2 and len(child_model.requests) == 1
        finally:
            await child_provider.aclose()
            await root_provider.aclose()


async def test_arbitrary_callback_has_not_been_adopting_an_independent_child():
    async with timeout(20), fixture() as values:
        parent, child, context, root_model, child_model, effects, _guards, sibling = values
        entered = asyncio.Event()

        @function_tool
        async def nested_child(input: str) -> str:
            async with child.session(context=context) as independent:
                turn = await independent.submit(input)
                boundary = await turn.wait_for_request_or_result()
                assert isinstance(boundary, PendingRequests)
                entered.set()
                return (await turn.get_result()).final_output

        ordinary_parent = replace(parent, tools=[nested_child, parent.tools[1]])
        async with ordinary_parent.session(context=context) as session:
            await session.submit("begin independent child safety control")
            await entered.wait()
            await sibling.wait()
            assert await session.pending_interactions() == ()
            with pytest.raises(RuntimeError, match="manual"):
                await session.suspend()
            assert effects == ["accepted-sibling"]
            assert len(root_model.requests) == len(child_model.requests) == 1


async def test_configured_child_inside_native_role_has_been_failing_its_one_shot_root(
    tmp_path, monkeypatch, native_executor_binary
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import collaboration, trust
    from tests.test_native_executor_tools import declaration

    role_model = next(
        item["slug"]
        for item in json.loads(ROLE_CATALOG.read_text())["models"]
        if item.get("supported_in_api") and item.get("multi_agent_version") == "v2"
    )
    async with timeout(30), fixture() as values:
        parent, _child, context, root_model, child_model, effects, _guards, _sibling = values
        root_model.replies.clear()
        root_model.respond(
            collaboration("native_delegate", {"input": "Run the child."}, "role-call")
        )
        root_model.respond(
            function_call("nested_child", {"input": "Request approval."}, "role-nested-call")
        )
        root_model.respond(assistant_output("unexpected role continuation"))
        native_parent = Agent[dict[str, str]](
            instructions="Run the declared role.",
            model=role_model,
            provider=parent.provider,
            tools=[
                parent.tools[0],
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="bridge",
                            instructions="Run nested_child.",
                            description="Run the configured child.",
                        )
                    ],
                    managed_tools=[
                        NativeRoleTool(
                            name="native_delegate",
                            role="bridge",
                            description="Run the bridge role.",
                        )
                    ],
                    interactions=True,
                ),
            ],
        )
        async with ExecutorPeer(tmp_path / "remote").serve() as executor:
            trust(executor, tmp_path, monkeypatch)
            with pytest.raises(InteractionRequired):
                await native_parent.run(
                    "begin native role witness", context=context, environment=declaration(executor)
                )
        assert effects == []
        assert len(root_model.requests) == 2
        assert len(child_model.requests) == 1


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"), reason="requires a Linux or macOS strict executor"
)
@pytest.mark.parametrize("same_source", [True, False])
async def test_configured_child_has_been_preserving_its_authenticated_skill_source(
    tmp_path, monkeypatch, native_executor_binary, same_source
):
    from agents import Environment
    from agents.sandbox import Manifest, Skills, sandbox_environment
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_sandbox_skills import Source
    from tests.test_sandbox_environment import Client, Options

    async with timeout(30), peer() as root_model, peer() as child_model:
        async with ExecutorPeer(tmp_path / "remote").serve() as executor:
            trust(executor, tmp_path, monkeypatch)
            resource = Source(effects=[])
            environment = Environment(
                url=executor.url,
                headers={"Authorization": "Bearer " + executor.token},
                cwd=str(executor.root),
                tools=(),
                capability_directories=[str(executor.root / ".agents")],
            )

            @asynccontextmanager
            async def connect(_session):
                yield environment

            first = sandbox_environment(
                Client([]),
                manifest=Manifest(root=str(executor.root)),
                options=Options(),
                connect=connect,
                skills=Skills(lazy_from=resource),
            )
            second = sandbox_environment(
                Client([]),
                manifest=Manifest(root=str(executor.root)),
                options=Options(),
                connect=connect,
            )

            @asynccontextmanager
            async def selected_source():
                if same_source:
                    yield first
                else:
                    async with first:
                        yield second

            root_model.respond(
                function_call("skill_child", {"input": "Load report."}, "skill-parent-call")
            )
            root_model.respond(assistant_output("parent complete"))
            child_model.respond(
                function_call("load_skill", {"skill_name": "report"}, "skill-child-call")
            )
            child_model.respond(assistant_output("child complete"))
            root_provider = NativeProvider(
                base_url=root_model.url, api_key="synthetic-skill-parent"
            )
            child_provider = NativeProvider(
                base_url=child_model.url, api_key="synthetic-skill-child"
            )
            child = Agent(
                instructions="Load report.",
                model=MODEL,
                provider=child_provider,
                tools=first.skill_tools,
            )
            parent = Agent(
                instructions="Run the child.",
                model=MODEL,
                provider=root_provider,
                tools=[child.as_tool("skill_child")],
            )
            try:
                async with (
                    selected_source() as source,
                    parent.session(environment=source) as session,
                ):
                    turn = await session.submit("begin skill source witness")
                    if same_source:
                        assert (await turn.get_result()).final_output == "parent complete"
                        assert resource.effects == ["report"]
                        assert (
                            executor.root / ".agents/report/SKILL.md"
                        ).read_bytes() == b"lazy body"
                        assert len(root_model.requests) == len(child_model.requests) == 2
                    else:
                        with pytest.raises(RuntimeError, match="current sandbox environment"):
                            await turn.get_result()
                        assert resource.effects == []
                        assert not (executor.root / ".agents/report/SKILL.md").exists()
                        assert len(root_model.requests) == len(child_model.requests) == 1
            finally:
                await child_provider.aclose()
                await root_provider.aclose()


async def test_root_has_been_replying_through_its_admitted_configured_child_ancestry():
    async with timeout(25), fixture() as values, peer() as outer_model:
        middle, leaf, context, middle_model, leaf_model, effects, _guards, sibling = values
        outer_model.respond(
            function_call("outer_child", {"input": "Run the nested child."}, "outer-call")
        )
        outer_model.respond(assistant_output("outer complete"))
        outer_provider = NativeProvider(base_url=outer_model.url, api_key="synthetic-outer-parent")
        outer = Agent[dict[str, str]](
            instructions="Run the configured child.",
            model=MODEL,
            provider=outer_provider,
            tools=[middle.as_tool("outer_child")],
        )
        try:
            async with (
                outer.session(context=context) as session,
                outer.session(context=context) as foreign,
            ):
                turn = await session.submit("begin nested grant witness")
                boundary = await turn.wait_for_request_or_result()
                assert isinstance(boundary, PendingRequests)
                [request] = boundary.requests
                await sibling.wait()
                assert request.session_id != session.id
                assert request.name == "child_effect"
                assert request.call_id == "child-approval-call"
                assert effects == ["accepted-sibling"]
                reply = request.reply(True)
                with pytest.raises(InteractionExpiredError):
                    await foreign.respond(reply)
                await session.respond(reply)
                with pytest.raises(InteractionExpiredError):
                    await session.respond(reply)
                assert (await turn.get_result()).final_output == "outer complete"
                with pytest.raises(InteractionExpiredError):
                    await session.respond(reply)
                assert effects == ["accepted-sibling", "child-effect"]
                assert (
                    len(outer_model.requests)
                    == len(middle_model.requests)
                    == len(leaf_model.requests)
                    == 2
                )
                assert leaf.name == "Nested guarded child"
        finally:
            await outer_provider.aclose()
