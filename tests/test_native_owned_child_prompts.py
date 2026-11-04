"""Positive child prompts use original Core responders and exact child owners."""

import asyncio
import re
from contextlib import aclosing
from dataclasses import replace

import pytest

from agents import (
    ApprovalRequest,
    ChildInvocation,
    ElicitationRequest,
    HttpMCP,
    InteractionExpiredError,
    NativeAgents,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_child_agents import agent_for, child_peer, handles, trust
from tests.test_native_child_tool_integration import Memory, family_agent, family_peer
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_scoped_policies import AskingPeer


def interactive(agent):
    return replace(
        agent,
        tools=[
            replace(tool, interactions=True) if isinstance(tool, NativeAgents) else tool
            for tool in agent.tools
        ],
    )


async def cancel_child(session, turn):
    for child in [item async for item in session.subagents()]:
        if child.id == turn.child_id:
            assert await child.try_cancel(turn)
            return
    raise AssertionError("The exact child snapshot disappeared before cancellation")


@pytest.mark.parametrize("route", ["exec", "patch"])
async def test_owned_child_approvals_use_equal_call_ids_without_sibling_authority(
    tmp_path, monkeypatch, route
):
    async with (
        timeout(30),
        child_peer(("research", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        effect = tmp_path / "owned-child-effect"
        if route == "exec":
            peer.command = f"printf proof > {effect}"
            peer.extra_arguments = {
                "sandbox_permissions": "require_escalated",
                "justification": "Approve this exact child command",
            }
            original_response = peer.response
            completion_id = "shared-call-id"

            def response():
                nonlocal completion_id
                items = original_response()
                if items[0].get("item", {}).get("id") == "done-alpha":
                    body = peer.child_requests["alpha"][-1]
                    output = tool_output(body, completion_id)
                    running = re.search(r"(?m)^Process running with session ID (-?\d+)$", output)
                    if running:
                        assert any(tool.get("name") == "write_stdin" for tool in body["tools"])
                        completion_id = f"owned-alpha-wait-{len(peer.child_requests['alpha'])}"
                        arguments = {
                            "session_id": int(running[1]),
                            "chars": "",
                            "yield_time_ms": 1000,
                            "max_output_tokens": 100,
                        }
                        return [function_call("write_stdin", arguments, completion_id)]
                    assert re.search(r"(?m)^Process exited with code 0$", output), output
                return items

            peer.response = response
        else:
            peer.patch = f"*** Begin Patch\n*** Add File: {effect}\n+proof\n*** End Patch"
        for release in peer.release.values():
            release.set()
        async with interactive(agent_for(peer, executor)).session(
            environment=declaration(executor)
        ) as session:
            root = await session.submit("Start two children")
            assert (await root.get_result()).final_output == "root complete"
            async with aclosing(session.interactions()) as cursor:
                first, second = await anext(cursor), await anext(cursor)
            children = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            alpha, beta = (first, second) if first.subagent_id == alpha_id else (second, first)
            assert isinstance(alpha, ApprovalRequest) and isinstance(beta, ApprovalRequest)
            assert alpha._message["call_id"] == beta._message["call_id"] == "shared-call-id"
            assert alpha.subagent_id != beta.subagent_id
            assert alpha.submission_id == children[alpha.subagent_id].submission_id
            assert alpha.turn_id == children[alpha.subagent_id].turn_id
            assert beta.turn_id == children[beta.subagent_id].turn_id
            assert not effect.exists()
            await alpha.respond("allow_once")
            assert (await children[alpha.subagent_id].get_result()).final_output == "alpha complete"
            assert effect.read_text().strip() == "proof"
            # Alpha's consumed wrapper cannot answer the still-pending sibling.
            with pytest.raises(InteractionExpiredError):
                await alpha.respond("allow_once")
            assert beta in await session.pending_interactions()
            await beta.respond("deny")
            assert (await children[beta.subagent_id].get_result()).final_output == "beta complete"
            assert not await session.pending_interactions()
            assert (
                await (await session.submit("Continue root")).get_result()
            ).final_output == "root complete"


@pytest.mark.parametrize(
    "method", ["elicitation/create", "openai/form", "openai/elicitation/create"]
)
@pytest.mark.parametrize("approval_mode", ["approve", "auto"])
async def test_owned_child_http_form_reaches_original_turn_and_continues(
    tmp_path, monkeypatch, method, approval_mode
):
    memory = Memory()
    vault = AskingPeer(form_method=method)
    vault.annotations = None
    async with (
        timeout(30),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        original = peer.invoke

        def invoke(name, step):
            if step == 1:
                return (
                    mcp_call("search", "mcp__vault", "shared-mcp-id")
                    if name == "alpha"
                    else assistant_output("beta complete")
                )
            return original(name, step)

        peer.invoke = invoke

        def check_form_result(context, value):
            assert context.context is memory
            assert isinstance(context.submission, ChildInvocation)
            assert context.submission.child_id == peer.child_metadata["alpha"][0]["thread_id"]
            memory.mcp.append(context)
            assert context.server_name == "vault" and context.tool_name == "search"
            return {
                "content": [
                    {"type": "text", "text": "accepted-mcp:" + context.submission.child_id}
                ],
                "isError": False,
            }

        agent = interactive(family_agent(peer, executor, url, memory))
        agent = replace(
            agent,
            tools=[
                replace(
                    tool,
                    tools=["search"],
                    approval_mode=approval_mode,
                    transform_result=check_form_result,
                )
                if isinstance(tool, HttpMCP)
                else tool
                for tool in agent.tools
            ],
        )
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            root = await session.submit("Start alpha and beta")
            assert (await root.get_result()).final_output == "root complete"
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            for release in peer.release.values():
                release.set()
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            if approval_mode == "auto":
                assert isinstance(request, ApprovalRequest)
                await request.respond("allow_once")
                async with aclosing(session.interactions()) as cursor:
                    request = await anext(cursor)
            assert isinstance(request, ElicitationRequest)
            children = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            assert request.subagent_id == alpha_id
            assert request.turn_id == children[alpha_id].turn_id
            assert request.submission_id == children[alpha_id].submission_id
            assert request.call is None
            with pytest.raises(ValueError):
                await request.respond({"answer": 123})
            assert request in await session.pending_interactions()
            await request.respond({"answer": "child answer"})
            assert (await children[alpha_id].get_result()).final_output == "alpha complete"
            assert len(vault.answers) == len(memory.mcp) == 1
            assert memory.mcp[0].submission.turn_id == children[alpha_id].turn_id
            assert "accepted-mcp:" + alpha_id in tool_output(
                peer.child_requests["alpha"][-1], "shared-mcp-id"
            )
            assert vault.answers[0]["result"]["content"] == {"answer": "child answer"}
            with pytest.raises(InteractionExpiredError):
                await request.decline()
            for child in children.values():
                await child.get_result()
            assert not await session.pending_interactions()


async def test_response_suspended_before_native_consumption_expires_on_child_cancel(
    tmp_path, monkeypatch
):
    async with (
        timeout(30),
        child_peer(("default", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        effect = tmp_path / "expired-child-effect"
        peer.command = f"printf stale > {effect}"
        peer.extra_arguments = {
            "sandbox_permissions": "require_escalated",
            "justification": "Held reply",
        }
        for release in peer.release.values():
            release.set()
        async with interactive(agent_for(peer, executor)).session(
            environment=declaration(executor)
        ) as session:
            root = await session.submit("Open two children")
            await root.get_result()
            async with aclosing(session.interactions()) as cursor:
                first, second = await anext(cursor), await anext(cursor)
            children = {turn.child_id: turn for turn in await handles(session, 2)}
            entered, release = asyncio.Event(), asyncio.Event()
            native = session._native

            class HeldReply:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _respond_child_question(self, *args):
                    entered.set()
                    await release.wait()
                    return await native._respond_child_question(*args)

            session._native = HeldReply()
            responding = asyncio.create_task(first.respond("allow_once"))
            try:
                await entered.wait()
                cancelling = asyncio.create_task(cancel_child(session, children[first.subagent_id]))
                while await native._child_question_pending(first._message["_sdk_question_id"]):
                    await asyncio.sleep(0)
                release.set()
                # Child retirement cancels the owned enqueue if it is still
                # suspended; a consumed native reply instead reports expiry.
                with pytest.raises((InteractionExpiredError, asyncio.CancelledError)):
                    await responding
                await cancelling
                with pytest.raises(InteractionExpiredError):
                    await first.respond("allow_once")
                assert not effect.exists()
                # Cancellation of the first owner must not consume the other grant.
                session._native = native
                assert second in await session.pending_interactions()
                await second.respond("deny")
                await children[second.subagent_id].get_result()
            finally:
                release.set()
                await asyncio.gather(responding, return_exceptions=True)
                session._native = native


@pytest.mark.parametrize("reload", [False, True])
async def test_original_child_grant_cannot_answer_same_id_successor_or_reload(
    tmp_path, monkeypatch, reload
):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from tests.native_fixtures import function_call
    from tests.test_native_child_agents import ChildPeer, captured_turn, collaboration, metadata

    effect = tmp_path / "successor-effect"

    class ReusablePrompts(ChildPeer):
        def response(self):
            body = self.requests[-1]
            meta = metadata(body, self.request_headers[-1])
            if meta["agent_name"] == "/root":
                return super().response()
            name = meta["agent_name"].rsplit("/", 1)[-1]
            self.child_metadata.setdefault(name, []).append(meta)
            requests = self.child_requests.setdefault(name, [])
            requests.append(body)
            if (name == "alpha" and (len(requests) == 1 or getattr(self, "ask_again", False))) or (
                name == "beta" and len(requests) == 1
            ):
                if name == "alpha":
                    self.ask_again = False
                return [
                    function_call(
                        "exec_command",
                        {
                            "cmd": f"printf successor > {effect}",
                            "sandbox_permissions": "require_escalated",
                            "justification": "Exact successor only",
                        },
                        "same-approval-id",
                    )
                ]
            return [assistant_output(name + " complete")]

    peer = ReusablePrompts(("default", "default"))
    application = web.Application()
    application.router.add_post("/v1/responses", peer.handle)
    application.router.add_get("/v1/responses", peer.websocket)
    application.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(45),
        TestServer(application, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with interactive(agent_for(peer, executor)).session(
            environment=declaration(executor)
        ) as session:
            await (await session.submit("Start alpha and beta")).get_result()
            async with aclosing(session.interactions()) as cursor:
                first, second = await anext(cursor), await anext(cursor)
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            old, beta = (first, second) if first.subagent_id == alpha_id else (second, first)
            original = await captured_turn(session, alpha_id)
            assert original is not None
            old_id = old._message["_sdk_question_id"]
            # Leave the grant unanswered and retire the actual native source.
            await cancel_child(session, original)
            if reload:
                # Beta remains active. A third child forces idle alpha out of
                # the two-slot runtime before followup reloads its durable ID.
                peer.action = collaboration(
                    "spawn_agent",
                    {"task_name": "gamma", "message": "Finish now", "fork_turns": "none"},
                    "evict-original",
                )
                await (await session.submit("Start gamma")).get_result()
                while (
                    "gamma" not in peer.child_metadata
                    or (
                        gamma := await captured_turn(
                            session, peer.child_metadata["gamma"][0]["thread_id"]
                        )
                    )
                    is None
                ):
                    await asyncio.sleep(0)
                await gamma.get_result()
            # Cancelled alpha may not have sent its final model request. Force
            # the next actual alpha turn to issue the same approval call ID.
            peer.ask_again = True
            peer.action = collaboration(
                "followup_task",
                {"target": "/root/alpha", "message": "Issue another request"},
                "same-runtime-followup",
            )
            await (await session.submit("Continue alpha")).get_result()
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
                if request is beta:
                    request = await anext(cursor)
            current = await captured_turn(session, alpha_id)
            assert current is not None and request.subagent_id == alpha_id
            assert current.turn_id == request.turn_id != original.turn_id
            if reload:
                assert current.instance_id != original.instance_id
            else:
                assert current.instance_id == original.instance_id
            assert request._message["call_id"] == old._message["call_id"] == "same-approval-id"
            assert not await session._native._respond_child_question(
                old_id,
                "exec_approval",
                '{"id":"same-approval-id","turn_id":null,"decision":"approved"}',
            )
            with pytest.raises(InteractionExpiredError):
                await old.respond("allow_once")
            assert request in await session.pending_interactions()
            assert not effect.exists()
            await request.respond("deny")
            await current.get_result()
            await beta.respond("deny")
            assert not effect.exists()


@pytest.mark.parametrize("failure", ["prompt", "catalog"])
async def test_uncached_required_child_startup_fails_before_turn_publication(
    tmp_path, monkeypatch, failure
):
    import json

    from aiohttp import web

    from tests.test_native_mcp_tools import Peer

    class StartupPeer(AskingPeer):
        def __init__(self):
            super().__init__(startup=True)
            self.initializations = 0
            self.child_startup = asyncio.Event()

        async def handle(self, request):
            body = await request.json()
            if body.get("method") == "initialize":
                self.initializations += 1
                response = await Peer.handle(self, request)
                value = json.loads(response.body)
                value["result"]["capabilities"]["experimental"] = {
                    "codex/tool-catalog-cache": {"cacheable": False}
                }
                return web.json_response(value)
            if body.get("method") == "tools/list":
                if self.initializations == 1:
                    return await Peer.handle(self, request)
                self.child_startup.set()
                if failure == "catalog":
                    # Connection succeeded, but this candidate cannot supply
                    # the selected tool. Required validation must run before publication.
                    return web.json_response(
                        {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}}
                    )
            return await super().handle(request)

    vault = StartupPeer()
    async with (
        timeout(30),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        app = interactive(agent_for(peer, executor))
        app = replace(
            app,
            tools=[
                *app.tools,
                HttpMCP(name="vault", url=url, tools=["search"], approval_mode="approve"),
            ],
        )
        async with app.session(environment=declaration(executor)) as session:
            root = await session.submit("Attempt the child startup")
            await vault.child_startup.wait()
            assert (await root.get_result()).final_output == "root complete"
            assert not peer.child_requests
            assert not any([child.turn is not None async for child in session.subagents()])
            assert not await session.pending_interactions()
            assert not vault.calls
            if failure == "prompt":
                await vault.finished.wait()
                assert len(vault.answers) == 1
                assert vault.answers[0]["result"]["action"] == "cancel"
            assert (
                await (await session.submit("Continue the surviving parent")).get_result()
            ).final_output == "root complete"


@pytest.mark.parametrize("enabled,unsupported", [(True, False), (False, False), (True, True)])
def test_missing_child_grant_distinguishes_expiry_from_unsupported_prompt(enabled, unsupported):
    from agents import ChildInvocation, InteractionRequired, Session
    from agents.manual import _question

    owner = object.__new__(Session)
    owner._native_agents = NativeAgents(interactions=enabled)
    child = ChildInvocation(
        session_id="session", child_id="child", instance_id="incarnation", turn_id="turn"
    )
    message = {
        "type": "elicitation_request",
        "turn_id": "turn",
        "server_name": "source",
        "id": "entry",
        "request": {
            "mode": "form",
            "message": "Question",
            "requested_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        },
    }
    if unsupported:
        message["request"]["requested_schema"]["properties"]["answer"] = {
            "$ref": "https://example.invalid/schema.json"
        }
    expected = InteractionExpiredError if enabled and not unsupported else InteractionRequired
    with pytest.raises(expected):
        _question(owner, message, child)


@pytest.mark.parametrize("cancel", [False, True])
async def test_expired_missing_grant_delivery_preserves_original_native_outcome(
    tmp_path, monkeypatch, cancel
):
    import copy
    import json

    from agents import NativeError, Session
    from tests.test_native_child_agents import captured_turn, collaboration

    original_read = Session._read_child_event
    delayed = {}
    delivery_released = False
    question_ready, terminal_ready = asyncio.Event(), asyncio.Event()

    async with (
        timeout(40),
        child_peer(("default", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        effect = tmp_path / "must-not-run"
        peer.command = f"printf stale > {effect}"
        peer.extra_arguments = {
            "sandbox_permissions": "require_escalated",
            "justification": "Original child only",
        }
        for release in peer.release.values():
            release.set()

        async def hold_original_delivery(session, event):
            alpha = peer.child_metadata.get("alpha", [{}])[0].get("thread_id")
            kind = event["msg"]["type"]
            if (
                not delivery_released
                and event["subagent_id"] == alpha
                and kind == "exec_approval_request"
            ):
                delayed["question"] = copy.deepcopy(event)
                question_ready.set()
                return
            if (
                not delivery_released
                and event["subagent_id"] == alpha
                and kind in {"task_complete", "turn_complete", "turn_aborted"}
                and "terminal" not in delayed
            ):
                delayed["terminal"] = copy.deepcopy(event)
                terminal_ready.set()
                return
            await original_read(session, event)

        monkeypatch.setattr(Session, "_read_child_event", hold_original_delivery)
        async with interactive(agent_for(peer, executor)).session(
            environment=declaration(executor)
        ) as session:
            await (await session.submit("Start alpha and beta")).get_result()
            await question_ready.wait()
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            turns = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha = turns[alpha_id]
            grant_id = delayed["question"]["msg"]["_sdk_question_id"]
            cancelling = None
            try:
                if cancel:
                    cancelling = asyncio.create_task(cancel_child(session, alpha))
                else:
                    assert await session._native._respond_child_question(
                        grant_id,
                        "exec_approval",
                        json.dumps(
                            {
                                "id": "shared-call-id",
                                "turn_id": alpha.turn_id,
                                "decision": {"denied": {"rejection": "denied"}},
                            }
                        ),
                    )
                await terminal_ready.wait()
                assert not await session._native._child_question_pending(grant_id)
                # The native source really expired. At the highest controllable reader
                # boundary, reproduce the payload emitted when bridge capture returns None.
                # This changes no production scheduler and invents no child/turn identity.
                expired = delayed["question"]
                expired["msg"].pop("_sdk_question_id")
                await original_read(session, expired)
                await original_read(session, delayed["terminal"])
            finally:
                # Release current and future delivery before joining cancellation.
                # Even a failed assertion must not retain a terminal behind itself.
                delivery_released = True
                if "terminal" in delayed:
                    await original_read(session, delayed["terminal"])
                if cancelling is not None:
                    await cancelling
            if cancel:
                with pytest.raises(NativeError, match="^Native child turn was interrupted$"):
                    await alpha.get_result()
            else:
                assert (await alpha.get_result()).final_output == "alpha complete"
            async with aclosing(session.interactions()) as cursor:
                sibling = await anext(cursor)
            assert sibling.subagent_id != alpha_id
            assert not await session._native._respond_child_question(
                grant_id,
                "exec_approval",
                json.dumps(
                    {
                        "id": "shared-call-id",
                        "turn_id": alpha.turn_id,
                        "decision": "approved",
                    }
                ),
            )
            response = peer.response
            successor_metadata = []

            def successor_response():
                from tests.test_native_child_agents import metadata

                meta = metadata(peer.requests[-1], peer.request_headers[-1])
                if meta["agent_name"] == "/root/alpha" and meta["turn_id"] != alpha.turn_id:
                    successor_metadata.append(meta)
                    return [assistant_output("alpha successor complete")]
                return response()

            peer.response = successor_response
            peer.action = collaboration(
                "followup_task", {"target": "/root/alpha", "message": "Continue"}, "successor"
            )
            await (await session.submit("Continue alpha")).get_result()
            successor = await captured_turn(session, alpha_id)
            while successor is None or successor.turn_id == alpha.turn_id:
                await asyncio.sleep(0)
                successor = await captured_turn(session, alpha_id)
            assert (await successor.get_result()).final_output == "alpha successor complete"
            assert successor_metadata[0]["turn_id"] == successor.turn_id
            await original_read(session, expired)
            assert sibling in await session.pending_interactions()
            await sibling.respond("deny")
            await turns[sibling.subagent_id].get_result()
            assert not effect.exists()


@pytest.mark.parametrize("interactions", [False, True])
async def test_child_prompt_mode_preserves_root_policy_and_limits_child_approvals(
    tmp_path, monkeypatch, interactions
):
    from agents import Session
    from tests.test_native_root_environment_approval import Observe

    observed = []
    read_events = Session._read_events

    async def observe_from_opening(session):
        # Observe the actual Core SessionConfigured event, before the reader
        # can consume it. No native configuration or approval is substituted.
        capture = Observe(session._native)
        session._native = capture
        observed.append(capture)
        await read_events(session)

    monkeypatch.setattr(Session, "_read_events", observe_from_opening)
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        target = tmp_path / "child-policy-effect"
        peer.command = f"printf child > {target}"
        peer.extra_arguments = {
            "sandbox_permissions": "require_escalated",
            "justification": "Child approval only",
        }
        for release in peer.release.values():
            release.set()
        app = agent_for(peer, executor)
        if interactions:
            app = interactive(app)
        async with app.session(environment=declaration(executor)) as session:
            root = await session.submit("Start the child")
            assert (await root.get_result()).final_output == "root complete"
            configured = [
                event["msg"]
                for capture in observed
                for event in capture.events
                if event["msg"]["type"] == "session_configured" and not event.get("subagent_id")
            ]
            assert len(configured) == 1
            assert configured[0]["approval_policy"] == "never"
            child = (await handles(session, 1))[0]
            if interactions:
                async with aclosing(session.interactions()) as cursor:
                    request = await anext(cursor)
                assert isinstance(request, ApprovalRequest)
                assert request.subagent_id == child.child_id and request.turn_id == child.turn_id
                assert request.submission_id == child.submission_id
                assert not target.exists()
                await request.respond("allow_once")
                assert (await child.get_result()).final_output == "alpha complete"
                assert target.read_text() == "child"
            else:
                assert (await child.get_result()).final_output == "alpha complete"
                assert not await session.pending_interactions()
                assert not target.exists()
            assert (
                await (await session.submit("Continue root")).get_result()
            ).final_output == "root complete"
            root_questions = [
                event
                for capture in observed
                for event in capture.events
                if event["msg"]["type"] in {"exec_approval_request", "apply_patch_approval_request"}
                and not event.get("subagent_id")
            ]
            assert root_questions == []
