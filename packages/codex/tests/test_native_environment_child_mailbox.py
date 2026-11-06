"""Mail may preempt undispatched child work without leaving a phantom call or output."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, Allow, HttpMCP, NativeAgents, NativeError, ReplaceArguments
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_child_agents import (
    ChildPeer,
    agent_for,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer


class MailboxOverlap(ChildPeer):
    def __init__(self, boundary: str, policy: str) -> None:
        super().__init__(("research",))
        self.boundary = boundary
        self.policy = policy
        self.at_display_done = asyncio.Event()
        self.finish_display = asyncio.Event()
        self.before_event = self.hold_display

    async def hold_display(self, event: dict[str, Any]) -> None:
        if (
            event.get("type") == "response.output_item.done"
            and event.get("item", {}).get("id") == "display-alpha"
        ):
            self.at_display_done.set()
            await self.finish_display.wait()

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        path = metadata(body, self.request_headers[-1])["agent_name"]
        if path == "/root":
            return super().response()
        assert path == "/root/alpha"
        requests = self.child_requests.setdefault("alpha", [])
        requests.append(body)
        if len(requests) > 1:
            return [assistant_output("child completed")]
        call = function_call(
            "exec_command",
            {"cmd": "printf old-mailbox-argument >> mailbox-original"},
            "held-child-call",
        )
        call["item"]["id"] = "fc_mailbox_real"
        added_call = {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**call["item"], "arguments": ""},
        }
        call["output_index"] = 1
        if self.boundary == "commentary":
            display = assistant_output("Before the pending action")
            display["item"].update(id="display-alpha", phase="commentary")
            display_added = {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**display["item"], "content": []},
            }
        else:
            display = {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "display-alpha",
                    "summary": [{"type": "summary_text", "text": "Before the pending action"}],
                },
            }
            display_added = {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**display["item"], "summary": []},
            }
        display["output_index"] = 0
        if self.policy in {"no-policy", "approval-only"}:
            # A future call, after the display boundary, remains eligible for
            # Core's normal mail preemption when no direct edit is possible.
            return [display_added, display, added_call, call]
        return [display_added, added_call, call, display]


@pytest.mark.parametrize("boundary", ["reasoning", "commentary"])
@pytest.mark.parametrize("policy", ["before", "no-policy", "approval-only", "cancel"])
async def test_child_mail_preempts_undispatched_calls_and_reaches_next_request(
    tmp_path, monkeypatch, boundary, policy
):
    model = MailboxOverlap(boundary, policy)
    app = web.Application()
    app.router.add_post("/v1/responses", model.handle)
    app.router.add_get("/v1/responses", model.websocket)
    app.on_shutdown.append(model.close_sockets)
    decisions = []
    approved = asyncio.Event()

    async def before(ctx, value):
        decisions.append(("before", ctx.call_id))
        return ReplaceArguments(
            {**value, "cmd": "printf effective-mailbox-argument >> mailbox-effective"}
        )

    async def approval(ctx, value):
        decisions.append(("approval", ctx.call_id))
        assert value["cmd"] == "printf old-mailbox-argument >> mailbox-original"
        approved.set()
        return True

    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        model.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        try:
            selected = declaration(
                executor,
                **(
                    {}
                    if policy == "no-policy"
                    else {
                        "transform_arguments": before if policy != "approval-only" else None,
                        "approval": approval,
                    }
                ),
            )
            async with agent_for(model, executor, websocket=True).session(
                environment=selected
            ) as session:
                assert (
                    await (await session.submit("Start alpha")).get_result()
                ).final_output == "root complete"
                child = (await handles(session, 1))[0]
                await model.at_display_done.wait()
                if policy in {"before", "cancel"}:
                    await approved.wait()
                assert not (executor.root / "mailbox-original").exists()
                assert not (executor.root / "mailbox-effective").exists()
                if policy == "cancel":
                    # An approved call cannot survive cancellation of its owner.
                    subagent = next(
                        entry
                        for entry in [item async for item in session.subagents()]
                        if entry.id == child.child_id
                    )
                    assert await subagent.try_cancel(child)
                    model.finish_display.set()
                    with pytest.raises(NativeError, match="interrupt|cancel"):
                        await child.get_result()
                    assert len(model.child_requests["alpha"]) == 1
                    assert (
                        await (await session.submit("Continue the root")).get_result()
                    ).final_output == "root complete"
                    continue_input = json.dumps(model.requests[-1]["input"])
                    assert "held-child-call" not in continue_input
                    assert not (executor.root / "mailbox-original").exists()
                    assert not (executor.root / "mailbox-effective").exists()
                    return
                model.action = collaboration(
                    "send_message",
                    {"target": "/root/alpha", "message": "WORKER_MAIL_334_RETAIN"},
                    "send-mail",
                )
                assert (
                    await (await session.submit("Send mail")).get_result()
                ).final_output == "root complete"
                model.finish_display.set()
                assert (await child.get_result()).final_output == "child completed"
                next_input = model.child_requests["alpha"][1]["input"]
                calls = [
                    i
                    for i in next_input
                    if i.get("type") == "function_call" and i.get("call_id") == "held-child-call"
                ]
                outputs = [
                    i
                    for i in next_input
                    if i.get("type") == "function_call_output"
                    and i.get("call_id") == "held-child-call"
                ]
                assert "WORKER_MAIL_334_RETAIN" in json.dumps(next_input)
                assert not calls and not outputs, next_input
                if policy == "before":
                    assert decisions == [
                        ("approval", "held-child-call"),
                        ("before", "held-child-call"),
                    ]
                elif policy == "approval-only":
                    # Interception may read ahead before Core acts on mail.
                    # Approval still cannot create an execution or result.
                    assert decisions in ([], [("approval", "held-child-call")])
                else:
                    assert not decisions
                assert "old-mailbox-argument" not in json.dumps(next_input)
                assert "effective-mailbox-argument" not in json.dumps(next_input)
                assert not (executor.root / "mailbox-original").exists()
                assert not (executor.root / "mailbox-effective").exists()
                assert not model.http_requests
        finally:
            model.finish_display.set()


@pytest.mark.parametrize("websocket", [False, True], ids=["http", "ws"])
@pytest.mark.parametrize(
    "with_before", [None, False, True], ids=["no-policy", "approval-only", "before"]
)
@pytest.mark.parametrize("binding", ["same", "selected", "rebind"])
async def test_child_mail_during_mcp_selection_and_idle_rebind(
    tmp_path, monkeypatch, websocket, with_before, binding
):
    # A change of policy on the same admitted resource must not change when
    # Core delivers mail or narrow the public idle rebind operation.
    model = MailboxOverlap("commentary", "no-policy")
    vault = Peer(names=("search",))
    app = web.Application()
    app.router.add_post("/v1/responses", model.handle)
    app.router.add_get("/v1/responses", model.websocket)
    app.on_shutdown.append(model.close_sockets)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        model.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        template = agent_for(model, executor, websocket=websocket)
        selected = HttpMCP(
            name="records",
            url=url,
            tools=["search"],
            **(
                {}
                if with_before is None
                else {
                    "before": (lambda ctx, call: Allow()) if with_before else None,
                    "approval": lambda ctx, call: True,
                }
            ),
        )
        roster = [selected]
        agent = Agent(
            instructions=template.instructions,
            provider=template.provider,
            model=template.model,
            tools=[
                *[t for t in roster if not isinstance(t, NativeAgents)],
                next(t for t in template.tools if isinstance(t, NativeAgents)),
            ],
        )
        assert agent.validate() is None
        try:
            async with agent.session(environment=declaration(executor)) as session:
                assert (
                    await (await session.submit("Start alpha")).get_result()
                ).final_output == "root complete"
                child = (await handles(session, 1))[0]
                await model.at_display_done.wait()
                model.action = collaboration(
                    "send_message",
                    {"target": "/root/alpha", "message": "MCP_MAIL_TIMING_334"},
                    "send-mcp-policy-mail",
                )
                assert (
                    await (await session.submit("Mail")).get_result()
                ).final_output == "root complete"
                model.finish_display.set()
                assert (await child.get_result()).final_output == "child completed"
                next_input = model.child_requests["alpha"][1]["input"]
                calls = [
                    i
                    for i in next_input
                    if i.get("type") == "function_call" and i.get("call_id") == "held-child-call"
                ]
                outputs = [
                    i
                    for i in next_input
                    if i.get("type") == "function_call_output"
                    and i.get("call_id") == "held-child-call"
                ]
                assert "MCP_MAIL_TIMING_334" in json.dumps(next_input)
                assert not calls and not outputs, next_input
                assert not (executor.root / "mailbox-original").exists()
                assert not vault.calls
        finally:
            model.finish_display.set()
