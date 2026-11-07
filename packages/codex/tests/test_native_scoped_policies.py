"""Public policies and actual native prompt enqueue: synthetic loopback peers only."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, cast

import pytest
from aiohttp import web

from agents import (
    Agent,
    Allow,
    ApprovalRequest,
    Deny,
    ElicitationRequest,
    HttpMCP,
    Interaction,
    InteractionExpiredError,
    InteractionRequired,
    MCPCallContext,
    MCPResultContext,
    NativeError,
    NativeToolCall,
    ReplaceArguments,
    ToolContext,
    ToolOutput,
    TurnStoppedError,
    UrlRequest,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_provider_websockets import peer as websocket_peer


@dataclass
class Account:
    tenant: str


@pytest.fixture(params=["before", "approval", "transform_result"])
def required_mcp_policy(request: pytest.FixtureRequest) -> tuple[str, dict[str, Any]]:
    def before(ctx: MCPCallContext[Account], call: NativeToolCall) -> Allow:
        assert ctx.context.tenant == "current"
        return Allow()

    def approval(ctx: MCPCallContext[Account], call: NativeToolCall) -> bool:
        return ctx.context.tenant == "current"

    def after(ctx: MCPResultContext[Account], result: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": f"tenant:{ctx.context.tenant}"}],
            "isError": result["isError"],
        }

    hook = request.param
    return hook, {
        "before": before if hook == "before" else None,
        "approval": approval if hook == "approval" else None,
        "transform_result": after if hook == "transform_result" else None,
    }


def selected(call_id: str, key: str) -> dict[str, Any]:
    event = mcp_call("search", "mcp__records", call_id)
    event["item"]["id"] = "fc_" + call_id
    event["item"]["arguments"] = json.dumps({"key": key})
    return event


@pytest.mark.parametrize("hook", ["before", "after"])
async def test_function_policy_requires_context_before_opening(
    responses: ResponsesFixture, hook: str
):
    effects = []

    def before(ctx: ToolContext[Account]) -> Allow:
        effects.append(ctx.context.tenant)
        return Allow()

    def after(ctx: ToolContext[Account], output: ToolOutput) -> ToolOutput:
        effects.append(ctx.context.tenant)
        return output

    policy = {
        "before": before if hook == "before" else None,
        "after": after if hook == "after" else None,
    }

    @function_tool(**policy)
    def send_invoice(invoice: str) -> str:
        effects.append(invoice)
        return invoice

    peer = Peer(names=("search",))
    async with timeout(15), peer.serve() as url:
        native = provider(responses)
        server = HttpMCP(name="records", url=url, tools=["search"])
        agent = Agent(instructions="Process.", tools=[send_invoice, server], provider=native)
        # Validation cannot bind context that a future Session will supply.
        assert agent.validate() is None
        with pytest.raises(TypeError, match="requires? application context"):
            async with agent.session() as session:
                await session.submit("Select")
        assert effects == []
        assert not peer.requests and not peer.calls
        assert not responses.requests


@pytest.mark.parametrize("hook", ["before", "after"])
async def test_replaced_function_policy_rejects_without_publishing_or_retiring_roster(
    responses: ResponsesFixture, hook: str
):
    effects = []

    def before(ctx: ToolContext[Account]) -> Allow:
        effects.append(ctx.context.tenant)
        return Allow()

    def after(ctx: ToolContext[Account], output: ToolOutput) -> ToolOutput:
        effects.append(ctx.context.tenant)
        return output

    @function_tool
    def send_invoice(invoice: str) -> str:
        effects.append(invoice)
        return "sent:" + invoice

    peer = Peer(names=("search",))
    async with timeout(15), peer.serve() as url:
        server = HttpMCP(name="records", url=url, tools=["search"])
        roster = [send_invoice, server]
        agent = Agent(instructions="Process.", tools=roster, provider=provider(responses))
        async with agent.session() as session:
            assert not peer.requests
            guarded = replace(
                send_invoice,
                before=before if hook == "before" else None,
                after=after if hook == "after" else None,
            )
            with pytest.raises(ValueError, match="captured"):
                await session.submit("invalid", tools=[guarded, server])
            # Activating the dormant server and the new binding must be atomic.
            assert not peer.requests and not peer.calls
            assert not responses.requests and effects == []
            responses.respond(function_call("send_invoice", {"invoice": "example"}, "invoice"))
            responses.respond(assistant_output("done"))
            turn = await session.submit("Send it.", tools=[send_invoice])
            assert (await turn.get_result()).final_output == "done"
            assert tool_output(responses.requests[-1], "invoice") == "sent:example"
            assert effects == ["example"]
            assert not peer.requests


async def test_mcp_policy_context_is_required_on_activation(
    responses: ResponsesFixture, required_mcp_policy: tuple[str, dict[str, Any]]
) -> None:
    hook, policy = required_mcp_policy
    effects = []

    @function_tool
    def receipt(value: str) -> str:
        effects.append(value)
        return "sent:" + value

    remote = Peer(names=("search", "excluded"))
    async with timeout(25), remote.serve() as url:
        server = HttpMCP(
            name="records",
            url=url,
            tools=["search", "excluded"],
            bearer_token="synthetic-unselected-policy-token",
            **policy,
        )
        roster = [receipt, server]
        agent = Agent(
            instructions="Use selected tools.", tools=roster, provider=provider(responses)
        )
        assert agent.validate() is None
        async with agent.session() as session:
            assert not remote.ready_entered.is_set()
            assert not remote.requests and not remote.auth
            with pytest.raises(TypeError, match=f"MCP {hook} requires application context"):
                await session.submit("protected", tools=[receipt, server.tool("search")])
            assert not remote.requests and not remote.calls and not remote.auth
            assert not responses.requests and not effects
            responses.respond(function_call("receipt", {"value": "current"}, "old-roster"))
            responses.respond(assistant_output("finished"))
            turn = await session.submit("Run the receipt.", tools=[receipt])
            assert (await turn.get_result()).final_output == "finished"
            assert effects == ["current"]
            assert tool_output(responses.requests[-1], "old-roster") == "sent:current"
            assert "synthetic-unselected-policy-token" not in json.dumps(responses.requests)
            assert not remote.requests and not remote.auth

        responses.respond(selected("selected-context", "key"))
        responses.respond(assistant_output("read"))
        current = replace(server, tools=["search"], bearer_token="synthetic-active-policy-token")
        async with agent.session(context=Account("current"), tools=[receipt, current]) as session:
            assert not remote.requests and not remote.auth
            assert (
                await (
                    await session.submit("Read the record.", tools=[receipt, current])
                ).get_result()
            ).final_output == "read"
            if hook == "transform_result":
                assert "tenant:current" in json.dumps(responses.requests[-1])
        assert [call["name"] for call in remote.calls] == ["search"]
        assert set(remote.auth) == {"Bearer synthetic-active-policy-token"}


async def test_mcp_open_and_restore_check_selected_binding_context(
    responses: ResponsesFixture, required_mcp_policy: tuple[str, dict[str, Any]]
) -> None:
    hook, policy = required_mcp_policy
    remote = Peer(names=("search",))
    async with timeout(25), remote.serve() as url:
        ordinary = HttpMCP(name="records", url=url, tools=["search"])
        protected = replace(ordinary, **policy, bearer_token="synthetic-protected-token")
        active_policy = [protected]
        agent = Agent(
            instructions="Read selected records.",
            tools=active_policy,
            provider=provider(responses),
        )
        # Deferred binding checks its policy at the operation that selects it.
        with pytest.raises(TypeError, match=f"MCP {hook} requires application context"):
            async with agent.session() as session:
                await session.submit("Select protected")
        assert not remote.requests and not remote.auth

        responses.respond(assistant_output("saved"))
        source = replace(agent, tools=[ordinary])
        async with source.session() as session:
            assert (await (await session.submit("Save.")).get_result()).final_output == "saved"
            saved = await session.save_state()
        prior_requests = list(remote.requests)
        prior_auth = list(remote.auth)
        with pytest.raises(TypeError, match=f"MCP {hook} requires application context"):
            async with agent.session(state=saved) as restored:
                await restored.submit("Select protected")
        assert remote.requests == prior_requests and remote.auth == prior_auth

        responses.respond(selected("restored-context", "fresh"))
        responses.respond(assistant_output("restored"))
        async with agent.session(context=Account("current"), state=saved) as session:
            assert (await (await session.submit("Read.")).get_result()).final_output == "restored"
        assert remote.calls[-1]["arguments"] == {"key": "fresh"}
        assert set(remote.auth[len(prior_auth) :]) == {"Bearer synthetic-protected-token"}


async def test_function_policy_context_from_session_with_partial_and_callable(
    responses: ResponsesFixture,
):
    effects = []

    def check(tenant: str, ctx: ToolContext[Account]) -> Allow:
        assert ctx.context.tenant == tenant
        effects.append("before")
        return Allow()

    class Receipt:
        def __call__(self, ctx: ToolContext[Account], output: ToolOutput) -> ToolOutput:
            effects.append("after")
            assert output == ToolOutput.text("sent")
            return ToolOutput.text(ctx.context.tenant + ":safe")

    @function_tool
    def send_invoice(invoice: str) -> str:
        effects.append(invoice)
        return "sent"

    guarded = replace(send_invoice, before=partial(check, "account"), after=Receipt())
    responses.respond(function_call("send_invoice", {"invoice": "example"}, "invoice"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Process.", tools=[guarded], provider=provider(responses))
    assert agent.validate() is None
    async with timeout(15), agent.session(context=Account("account")) as session:
        turn = await session.submit("Send it.")
        assert (await turn.get_result()).final_output == "done"
        assert tool_output(responses.requests[-1], "invoice") == "account:safe"
        assert effects == ["before", "example", "after"]


async def test_replaced_function_policy_allows_optional_and_any_context(
    responses: ResponsesFixture,
):
    effects = []

    def required(ctx: ToolContext[Account], output: ToolOutput) -> ToolOutput:
        return ToolOutput.text(ctx.context.tenant)

    def before(label: str, ctx: ToolContext[Account | None]) -> Allow:
        assert ctx.context is None
        effects.append(label)
        return Allow()

    class Receipt:
        def __call__(self, ctx: ToolContext[Any], output: ToolOutput) -> ToolOutput:
            assert ctx.context is None
            effects.append("after")
            return output

    @function_tool(after=required)
    def send_invoice(invoice: str) -> str:
        effects.append(invoice)
        return "sent"

    current = replace(
        send_invoice,
        before=partial(before, "before"),
        after=Receipt(),
    )
    responses.respond(function_call("send_invoice", {"invoice": "example"}, "invoice"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Process.", tools=[current], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        turn = await session.submit("Send it.")
        assert (await turn.get_result()).final_output == "done"
        assert tool_output(responses.requests[-1], "invoice") == "sent"
        assert effects == ["before", "example", "after"]


@pytest.mark.parametrize("deny", [False, True])
async def test_function_validated_input_output_and_local_denial(
    responses: ResponsesFixture, deny: bool
):
    received = []

    def before(ctx: ToolContext[None]):
        assert ctx.call_id == "original" and ctx.original_arguments == {"number": 4}
        assert ctx.call.model == responses.requests[0]["model"]
        assert ctx.call.model is not None and ctx.call.role is None
        return Deny("quota exceeded") if deny else ReplaceArguments({"number": "7"})

    def after(ctx: ToolContext[None], value: ToolOutput):
        assert ctx.effective_arguments == {"number": 7}
        assert ctx.call.model == responses.requests[0]["model"]
        assert value == ToolOutput.text("14")
        return ToolOutput.text("safe reply")

    @function_tool(before=before, after=after)
    async def calculate(ctx: ToolContext[None], number: int) -> int:
        assert ctx.call.model == responses.requests[0]["model"]
        received.append((ctx.call_id, number))
        return number * 2

    responses.respond(function_call("calculate", {"number": 4}, "original"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(15),
        Agent(
            instructions="Calculate", provider=provider(responses), tools=[calculate]
        ).session() as session,
    ):
        turn = await session.submit("go")
        assert (await turn.get_result()).final_output == "done"
        assert received == ([] if deny else [("original", 7)])
        assert ("quota exceeded" if deny else "safe reply") in tool_output(
            responses.requests[-1], "original"
        )
        history = [item async for item in session.items()]
        call = next(
            item
            for item in history
            if item.get("type") == "function_call" and item.get("call_id") == "original"
        )
        assert json.loads(call["arguments"]) == {"number": 4}


async def test_scoped_callback_rejection_drops_sdk_owned_hook_aliases(responses: ResponsesFixture):
    from _agents_codex import session as runtime_session

    secret = "SYNTHETIC_scoped_alias_private_8470"

    def before(ctx: ToolContext[None]):
        return ReplaceArguments({"values": {"name": "base"}})

    @function_tool(before=before, after=lambda ctx, value: value)
    async def take(ctx: ToolContext[None], values: dict[str, str]) -> int:
        values["rejected"] = secret
        return cast(Any, values)

    responses.respond(function_call("take", {"values": {"name": "sent"}}, "alias"))
    async with (
        timeout(15),
        Agent(
            instructions="Use take", provider=provider(responses), tools=[take]
        ).session() as session,
    ):
        turn = await session.submit("call")
        with pytest.raises(ValueError, match="Tool result failed validation") as caught:
            await turn.get_result()
        tb = caught.value.__traceback__
        session_frames = 0
        while tb:
            if tb.tb_frame.f_code.co_filename == runtime_session.__file__:
                session_frames += 1
                assert secret not in repr(tb.tb_frame.f_locals), tb.tb_frame.f_code.co_name
            tb = tb.tb_next
        assert session_frames > 0
        assert secret not in repr([item async for item in session.items()])
        assert len(responses.requests) == 1


async def test_mcp_two_overlapping_replacements_approval_sees_only_own_input(
    responses: ResponsesFixture,
):
    peer = Peer(names=("search",))
    all_before, approve_b = asyncio.Event(), asyncio.Event()
    original: dict[str, str] = {}
    approved: dict[str, str] = {}

    async def before(ctx: MCPCallContext[None], call: NativeToolCall):
        key = call.input["key"]
        assert ctx.call.nested_call_id is not None
        original[ctx.call.nested_call_id] = key
        if len(original) == 2:
            all_before.set()
        await all_before.wait()
        return ReplaceArguments({"key": "effective-" + key})

    async def approval(ctx: MCPCallContext[None], call: NativeToolCall) -> bool:
        key = call.input["key"]
        assert ctx.call.nested_call_id is not None
        approved[ctx.call.nested_call_id] = key
        if key == "effective-B":
            approve_b.set()
        else:
            await approve_b.wait()
        return True

    async with timeout(20), peer.serve() as url:
        agent = Agent(
            instructions="Run both",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    before=before,
                    approval=approval,
                )
            ],
            programmatic_tool_calling=True,
        )
        responses.respond(
            code_call(
                'const values = await Promise.all([tools.mcp__records__search({key:"A"}),'
                'tools.mcp__records__search({key:"B"})]); text(values);',
                "paired",
            )
        )
        responses.respond(assistant_output("done"))
        async with agent.session() as session:
            assert (await (await session.submit("go")).get_result()).final_output == "done"
        assert len(original) == len(approved) == 2
        assert len(set(original)) == 2
        assert approved == {key: "effective-" + value for key, value in original.items()}
        assert {call["arguments"]["key"] for call in peer.calls} == {"effective-A", "effective-B"}


async def test_mcp_allow_after_before_mutation_approves_native_input(
    responses: ResponsesFixture,
):
    peer = Peer(names=("search",))
    original = {"key": {"value": "original-disallowed"}}
    metadata = []
    approval_inputs = []

    def before(ctx: MCPCallContext[None], call: NativeToolCall):
        metadata.append((ctx.call.nested_call_id, replace(call, input=None)))
        call.input["key"]["value"] = "safe-approved"
        return Allow()

    def approval(ctx: MCPCallContext[None], call: NativeToolCall) -> bool:
        metadata.append((ctx.call.nested_call_id, replace(call, input=None)))
        approval_inputs.append(call.input)
        return call.input["key"]["value"] == "safe-approved"

    async with timeout(15), peer.serve() as url:
        responses.respond(
            code_call(
                'try { text(await tools.mcp__records__search({key:{value:"original-disallowed"}}));'
                " } catch (error) { text(String(error)); }",
                "original-call",
            )
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Search",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    before=before,
                    approval=approval,
                )
            ],
            programmatic_tool_calling=True,
        ).session() as session:
            turn = await session.submit("go")
            assert (await turn.get_result()).final_output == "done"
            assert {"approval": approval_inputs, "peer": peer.calls} == {
                "approval": [original],
                "peer": [],
            }
            assert len(metadata) == 2 and metadata[0] == metadata[1]
            ctx_id, call = metadata[0]
            assert ctx_id == call.runtime_tool_call_id
            assert call.outer_call_id == "original-call"
            assert call.turn_id == turn.turn_id
            responses.respond(assistant_output("next"))
            assert (await (await session.submit("continue")).get_result()).final_output == "next"


async def test_environment_allow_after_before_mutation_approves_native_input(
    responses: ResponsesFixture, tmp_path, monkeypatch
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_executor_tools import declaration

    original = "printf original-disallowed > original.txt"
    safe = "printf safe-approved"
    contexts = []
    approval_inputs = []

    def before(context, value):
        contexts.append(context)
        value["cmd"] = safe
        return Allow()

    def approval(context, value):
        contexts.append(context)
        approval_inputs.append(value)
        return value["cmd"] == safe

    async with timeout(25), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        responses.respond(function_call("exec_command", {"cmd": original}, "original-call"))
        responses.respond(assistant_output("done"))
        async with Agent(instructions="Run", provider=provider(responses)).session(
            environment=declaration(peer, transform_arguments=before, approval=approval)
        ) as session:
            turn = await session.submit("go")
            outcome = (await asyncio.gather(turn.get_result(), return_exceptions=True))[0]
            starts = [item for item in peer.requests if item.get("method") == "process/start"]
            assert {
                "approval": approval_inputs,
                "peer": starts,
                "marker": (peer.root / "original.txt").exists(),
            } == {"approval": [{"cmd": original}], "peer": [], "marker": False}
            assert isinstance(outcome, NativeError)
            assert len(contexts) == 1
            assert contexts[0].call_id == "original-call" and contexts[0].turn_id == turn.turn_id
            # The unused scripted completion can serve the next turn after the local denial.
            assert (await (await session.submit("continue")).get_result()).final_output == "done"


@pytest.mark.parametrize("allow", [False, True], ids=["deny", "allow"])
async def test_http_direct_mcp_continuation_uses_effective_input(
    responses: ResponsesFixture, allow: bool
):
    peer = Peer(names=("search",))
    observed = []

    def before(ctx, call):
        assert ctx.call.call_id == call.call_id == "direct-one"
        return ReplaceArguments({"key": "effective"})

    def approval(ctx, call):
        observed.append((ctx.call.call_id, json.loads(call.input)))
        return allow

    async with timeout(15), peer.serve() as url:
        responses.respond(selected("direct-one", "original"))
        responses.respond(assistant_output("finished"))
        agent = Agent(
            instructions="Search",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    before=before,
                    approval=approval,
                )
            ],
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            if allow:
                assert (await turn.get_result()).final_output == "finished"
                inputs = responses.requests[-1]["input"]
                native_call = next(
                    item
                    for item in inputs
                    if item.get("call_id") == "direct-one" and item.get("type") == "function_call"
                )
                assert json.loads(native_call["arguments"]) == {"key": "effective"}
                assert peer.calls[0]["arguments"] == {"key": "effective"}
            else:
                with pytest.raises(NativeError):
                    await turn.get_result()
                assert not peer.calls and len(responses.requests) == 1
        assert observed == [("direct-one", {"key": "effective"})]


@pytest.mark.parametrize("hook", ["before", "approval"])
async def test_ws_mcp_policy_admits_and_activates_only_selected_bindings(hook: str) -> None:
    effects = []

    @function_tool
    def receipt(value: str) -> str:
        effects.append(value)
        return "sent:" + value

    protected = {
        "before": (lambda ctx, call: Allow()) if hook == "before" else None,
        "approval": (lambda ctx, call: True) if hook == "approval" else None,
    }
    remote = Peer(names=("search",))
    async with timeout(20), remote.serve() as url, websocket_peer() as model:
        server = HttpMCP(name="records", url=url, tools=["search"], **protected)
        roster = [receipt, server]
        agent = Agent(instructions="Use tools.", tools=roster, provider=model.provider())
        async with agent.session() as session:
            assert not remote.requests and not remote.ready_entered.is_set()
            model.respond(function_call("receipt", {"value": "current"}, "ws-receipt"))
            model.respond(assistant_output("finished"))
            turn = await session.submit("Run the available receipt tool.", tools=[receipt])
            assert (await turn.get_result()).final_output == "finished"
            assert effects == ["current"]
            assert tool_output(model.requests[-1], "ws-receipt") == "sent:current"
            assert model.frames and not model.http_requests
            assert not remote.requests and not remote.ready_entered.is_set()
            model.respond(selected("activate", "owned"))
            model.respond(assistant_output("done"))
            assert (await (await session.submit("Search now")).get_result()).final_output == "done"
            assert [c["arguments"] for c in remote.calls] == [{"key": "owned"}]
            assert not model.http_requests


@pytest.mark.parametrize("allow", [True, False])
async def test_actual_native_mcp_approval_enqueue_and_no_duplicate(
    responses: ResponsesFixture, allow: bool
):
    peer = Peer(names=("search",), annotations=None)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("approval", "requested"))
        responses.respond(assistant_output("done"))
        agent = Agent(
            instructions="Search",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"])],
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            cursor = session.interactions()
            request = await anext(cursor)
            assert isinstance(request, ApprovalRequest)
            assert request.turn_id == turn.turn_id and request.subagent_id is None
            assert request in await session.pending_interactions()
            with pytest.raises(ValueError, match="allow_once"):
                await request.respond(cast(Any, "allow_always"))
            assert request in await session.pending_interactions()
            replies = await asyncio.gather(
                request.respond("allow_once" if allow else "deny"),
                request.respond("allow_once"),
                return_exceptions=True,
            )
            assert replies[0] is None and isinstance(replies[1], InteractionExpiredError)
            assert request not in await session.pending_interactions()
            result = await turn.get_result()
            assert result.final_output == "done"
            assert len(peer.calls) == int(allow)
            await cursor.aclose()


class AskingPeer(Peer):
    """Actual MCP server-to-client request over the tools/call or tools/list SSE."""

    def __init__(
        self,
        *,
        startup=False,
        unsupported=False,
        form_mode="form",
        schema=None,
        form_method="elicitation/create",
        form_meta=None,
    ):
        super().__init__(names=("search",))
        self.startup = startup
        self.unsupported = unsupported
        self.form_mode = form_mode
        self.form_method = form_method
        self.form_meta = form_meta
        self.schema = schema
        self.answers: list[dict[str, Any]] = []
        self.prompted = asyncio.Event()
        self.answered = asyncio.Event()
        self.finished = asyncio.Event()

    async def handle(self, request):
        body = await request.json()
        if body.get("id") == 873 and "method" not in body:
            self.answers.append(body)
            self.answered.set()
            return web.Response(status=202)
        if body.get("method") == ("tools/list" if self.startup else "tools/call"):
            self.requests.append(body)
            stream = web.StreamResponse(
                headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
            )
            await stream.prepare(request)
            fields = (
                self.schema
                if self.schema is not None
                else {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                }
            )
            prompt = {
                "jsonrpc": "2.0",
                "id": 873,
                "method": self.form_method,
                "params": {
                    "mode": "url",
                    "message": "Synthetic unsupported question",
                    "url": "https://example.invalid/never-visit",
                    "elicitationId": "synthetic-unsupported",
                }
                if self.unsupported
                else {
                    **({"mode": self.form_mode} if self.form_mode is not None else {}),
                    "message": "Synthetic question",
                    "requestedSchema": fields,
                    **({"_meta": self.form_meta} if self.form_meta is not None else {}),
                },
            }
            try:
                await stream.write(
                    ("event: message\ndata: " + json.dumps(prompt) + "\n\n").encode()
                )
                self.prompted.set()
                await self.answered.wait()
                if self.startup:
                    result = {"tools": []}
                else:
                    self.calls.append(body["params"])
                    result = {"content": [{"type": "text", "text": json.dumps(self.answers)}]}
                await stream.write(
                    (
                        "event: message\ndata: "
                        + json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})
                        + "\n\n"
                    ).encode()
                )
                return stream
            finally:
                self.finished.set()
        return await super().handle(request)


@pytest.mark.parametrize(
    ("form_mode", "reply"),
    [
        ("form", "respond"),
        ("form", "decline"),
        ("openai/form", "respond"),
        ("openaiForm", "decline"),
    ],
)
async def test_native_form_validation_and_once_only_enqueue(
    responses: ResponsesFixture, form_mode: str, reply: str
):
    peer = AskingPeer(form_mode=form_mode)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("form", "ordinary"))
        responses.respond(assistant_output("done"))
        agent = Agent(
            instructions="Ask",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            async for request in session.interactions():
                assert isinstance(request, ElicitationRequest)
                schema = cast(dict, request.schema)
                schema["properties"].clear()
                with pytest.raises(ValueError, match="form"):
                    await request.respond({"answer": 43})
                assert not peer.answers
                with pytest.raises(ValueError, match="form"):
                    await request.respond({})
                if reply == "respond":
                    await request.respond({"answer": "synthetic-input"})
                else:
                    await request.decline()
                # Invalid values are correctable only while this exact form lives.
                for invalid in ({}, {"answer": 43}, {"unknown": "private-input"}):
                    with pytest.raises(InteractionExpiredError, match="expired|answered"):
                        await request.respond(invalid)
                with pytest.raises(InteractionExpiredError, match="expired|answered"):
                    await request.decline()
                break
            assert (await turn.get_result()).final_output == "done"
            assert len(peer.answers) == 1
            result = peer.answers[0]["result"]
            assert result["action"] == ("accept" if reply == "respond" else "decline")
            if reply == "respond":
                assert result["content"] == {"answer": "synthetic-input"}
            with pytest.raises(InteractionExpiredError, match="expired|answered"):
                await request.respond({})


async def test_startup_form_fails_with_joined_cleanup(responses: ResponsesFixture):
    peer = AskingPeer(startup=True)
    async with timeout(18), peer.serve() as url:
        native = provider(responses)
        agent = Agent(
            instructions="Ask",
            provider=native,
            tools=[HttpMCP(name="records", url=url, tools=["search"])],
        )
        with pytest.raises(InteractionRequired, match="startup"):
            async with agent.session() as session:
                assert not peer.requests
                turn = await session.submit("Activate server")
                await turn.get_result()
        assert peer.prompted.is_set()
        # Native shutdown may notify the peer of cancellation. The SDK never
        # enqueues an accepted startup reply, and no provider request starts.
        assert all(reply.get("result") == {"action": "cancel"} for reply in peer.answers)
        assert not responses.requests
        await native.aclose()


@pytest.mark.parametrize("opening_kind", ["warning", "raw_response_item"])
async def test_opening_observation_cannot_block_selected_startup_form(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, opening_kind: str
):
    from agents import _native
    from agents.session import Session

    parked, deliver, consumed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_next = _native.NativeThread._next_session_event
    original_execute = Session._execute

    async def next_actual(thread):
        raw = await original_next(thread)
        event = json.loads(raw)
        if not parked.is_set() and event is not None:
            message = event.get("msg", {})
            if message.get("type") == opening_kind:
                # Hold the real native opening event until logical admission.
                parked.set()
                await deliver.wait()
                asyncio.get_running_loop().call_soon(consumed.set)
        return raw

    async def execute_after_reader(session, state, *args, **kwargs):
        await parked.wait()
        deliver.set()
        await consumed.wait()
        return await original_execute(session, state, *args, **kwargs)

    monkeypatch.setattr(_native.NativeThread, "_next_session_event", next_actual)
    monkeypatch.setattr(Session, "_execute", execute_after_reader)
    # Reuse the exact real MCP form, no-answer, no-model and joined-close assertions.
    await test_startup_form_fails_with_joined_cleanup(responses)


async def test_dormant_startup_form_fails_joins_and_fences_unconfirmed_roster(
    responses: ResponsesFixture,
):
    peer = AskingPeer(startup=True)
    effects: list[str] = []

    @function_tool
    def local() -> str:
        effects.append("called")
        return "record"

    async with timeout(25), peer.serve() as url:
        native = provider(responses)
        server = HttpMCP(name="records", url=url, tools=["search"], startup_timeout=10)
        roster = [local, server]
        async with Agent(instructions="Ask", provider=native, tools=roster).session() as session:
            assert not peer.requests
            assert session.active_turn is None
            submitted = await session.submit("Activate", tools=[server])
            with pytest.raises(InteractionRequired, match="startup"):
                # Reject and join before the unanswerable server's startup timeout.
                async with timeout(3):
                    await submitted.get_result()
            assert peer.prompted.is_set()
            assert all(reply.get("result") == {"action": "cancel"} for reply in peer.answers)
            assert not responses.requests
            with pytest.raises(RuntimeError, match="cannot continue after an unconfirmed"):
                await session.submit("must not use the old tools")
            assert not effects
        # Only closing was supported after unconfirmed publication. That joins
        # the Session's resources and leaves the borrowed provider usable.
        responses.respond(assistant_output("recovered"))
        async with Agent(instructions="Read", provider=native).session() as replacement:
            assert (await (await replacement.submit("go")).get_result()).final_output == "recovered"
        await native.aclose()
        assert len(responses.requests) == 1
        assert all(reply.get("result") == {"action": "cancel"} for reply in peer.answers)


async def test_url_prompt_rejects_form_values_without_enqueuing_acceptance(
    responses: ResponsesFixture,
):
    peer = AskingPeer(unsupported=True)
    async with timeout(18), peer.serve() as url:
        responses.respond(selected("form", "ordinary"))
        responses.respond(assistant_output("done"))
        native = provider(responses)
        agent = Agent(
            instructions="Ask",
            provider=native,
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")],
        )
        async with agent.session() as session:
            turn = await session.submit("go")
            async for request in session.interactions():
                assert isinstance(request, UrlRequest) and request.mode == "url"
                assert not hasattr(request, "schema")
                assert not hasattr(request, "respond")
                assert request.submission_id is None
                assert request.turn_id is None
                assert "example.invalid/never-visit" in request.description
                assert "example.invalid" not in repr(request)
                with pytest.raises(ValueError, match="not form values"):
                    request.reply({"answer": "never accept"})
                assert not peer.answers
                assert await session.pending_interactions() == (request,)
                await request.decline()
                break
            assert (await turn.get_result()).final_output == "done"
            assert [reply["result"]["action"] for reply in peer.answers] == ["decline"]
            assert peer.answers[0]["result"].get("content") is None
        await native.aclose()


async def test_run_handler_can_answer_native_approval(responses: ResponsesFixture):
    peer = Peer(names=("search",), annotations=None)

    def answer(request: Interaction):
        assert isinstance(request, ApprovalRequest)
        return request.reply("allow_once")

    async with timeout(18), peer.serve() as url:
        responses.respond(selected("run-approval", "go"))
        responses.respond(assistant_output("done"))
        agent = Agent(
            instructions="Search",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"])],
        )
        assert (await agent.run("go", on_interaction=answer)).final_output == "done"
        assert len(peer.calls) == 1


async def test_nested_run_without_handler_rejects_its_own_native_prompt(
    responses: ResponsesFixture,
):
    peer = Peer(names=("search",), annotations=None)
    shared = provider(responses)
    async with timeout(18), peer.serve() as url, shared:
        responses.respond(function_call("ask_child", {"input": "search"}, "child"))
        responses.respond(selected("child-approval", "go"))
        child = Agent(
            instructions="Search", tools=[HttpMCP(name="records", url=url, tools=["search"])]
        )

        @function_tool
        async def ask_child(input: str) -> str:
            result = await child.run(input, provider=shared)
            return result.final_output

        parent = Agent(instructions="Search.", provider=shared, tools=[ask_child])
        with pytest.raises(InteractionRequired, match="on_interaction"):
            await parent.run("go")
        assert not peer.calls


async def test_environment_concurrent_stdin_input_owned_through_approval(
    responses: ResponsesFixture, tmp_path, monkeypatch
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_executor_tools import declaration

    both = asyncio.Event()
    second_approved = asyncio.Event()
    original: dict[str, int] = {}
    approval_inputs: dict[str, int] = {}

    async def before(context, value):
        assert context.tool_name == "write_stdin"
        assert context.call_id is None and context.runtime_tool_call_id is not None
        ident = context.runtime_tool_call_id
        original[ident] = value["session_id"]
        if len(original) == 2:
            both.set()
        await both.wait()
        return ReplaceArguments({"session_id": value["session_id"] + 100})

    async def approval(context, value):
        ident = context.runtime_tool_call_id
        approval_inputs[ident] = value["session_id"]
        if value["session_id"] == 739002:
            second_approved.set()
        else:
            await second_approved.wait()
        # Approval cannot alter the separately held input actually given to Core.
        value["session_id"] = 55555
        return True

    async with timeout(20), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        responses.respond(
            code_call(
                "const results = await Promise.allSettled([tools.write_stdin({session_id:739001}),"
                "tools.write_stdin({session_id:739002})]); text(JSON.stringify(results));",
                "concurrent-stdin",
            )
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Run stdin", provider=provider(responses), programmatic_tool_calling=True
        ).session(
            environment=declaration(peer, transform_arguments=before, approval=approval)
        ) as session:
            turn = await session.submit("go")
            assert (await turn.get_result()).final_output == "done"
        assert len(original) == len(approval_inputs) == 2
        assert approval_inputs == original
        text = json.dumps(responses.requests[-1])
        assert "739101" in text and "739102" in text
        assert "55555" not in text
        assert not any(request.get("method") == "process/start" for request in peer.requests)


async def test_callback_quota_recipe_limits_effects_without_new_runtime_budget(
    responses: ResponsesFixture,
):
    from examples.codex.callback_quota import Quota, lookup

    responses.respond(
        function_call("lookup", {"record": "one"}, "one"),
        function_call("lookup", {"record": "two"}, "two"),
        function_call("lookup", {"record": "three"}, "three"),
    )
    responses.respond(assistant_output("done"))
    budget = Quota(remaining=2)
    agent: Agent[Quota, str] = Agent(
        instructions="Use lookup", tools=[lookup], provider=provider(responses)
    )
    async with timeout(15), agent.session(context=budget) as session:
        assert (await (await session.submit("go")).get_result()).final_output == "done"
        outputs = [tool_output(responses.requests[-1], name) for name in ("one", "two", "three")]
        assert sum("Information about" in output for output in outputs) == 2
        assert sum("Callback quota reached" in output for output in outputs) == 1
        assert budget.remaining == 0


@pytest.mark.parametrize("kind", ["approval", "server-form"])
async def test_root_native_mcp_reply_retirement_owns_its_enqueue(responses, kind):
    peer = Peer(names=("search",), annotations=None) if kind == "approval" else AskingPeer()
    async with timeout(24), peer.serve() as url:
        native = provider(responses)
        try:
            server = HttpMCP(
                name="records",
                url=url,
                tools=["search"],
                approval_mode="native" if kind == "approval" else "preapproved",
            )
            responses.respond(selected("repeated-native-id", "old-effect"))
            async with Agent(instructions="Search", provider=native, tools=[server]).session() as s:
                reached = asyncio.Event()
                cancelled = asyncio.Event()
                released = asyncio.Event()
                original = s._native

                class HeldReply:
                    def __getattr__(self, name):
                        return getattr(original, name)

                    async def call(self, operation, *args):
                        if operation == "resolve_elicitation" and not reached.is_set():
                            reached.set()
                            try:
                                await released.wait()
                            except asyncio.CancelledError:
                                cancelled.set()
                                await released.wait()
                                raise
                        return await original.call(operation, *args)

                s._native = HeldReply()
                turn = await s.submit("Start the old request")
                cursor = s.interactions()
                request = await anext(cursor)
                await cursor.aclose()
                assert isinstance(
                    request, ApprovalRequest if kind == "approval" else ElicitationRequest
                )
                response = asyncio.create_task(
                    request.respond("allow_once")
                    if kind == "approval"
                    else request.respond({"answer": "owned-old-form"})
                )
                stop = notice = None
                try:
                    await reached.wait()
                    stop = asyncio.create_task(turn.cancel())
                    notice = asyncio.create_task(cancelled.wait())
                    done, _ = await asyncio.wait(
                        (stop, notice), return_when=asyncio.FIRST_COMPLETED
                    )
                    assert notice in done, "Old native MCP reply escaped its root retirement"
                    assert not stop.done()
                    with pytest.raises(
                        RuntimeError, match="Session is finishing root interruption"
                    ):
                        await s.submit("Do not admit a new turn until the old reply is joined")
                finally:
                    released.set()
                    if notice is not None:
                        notice.cancel()
                        await asyncio.gather(notice, return_exceptions=True)
                    if stop is not None:
                        await stop
                    result = await asyncio.gather(response, return_exceptions=True)
                assert isinstance(result[0], asyncio.CancelledError)
                with pytest.raises(TurnStoppedError, match="^Turn stopped$"):
                    await turn.get_result()
                if kind == "approval":
                    assert not peer.calls
                    responses.respond(selected("repeated-native-id", "new-unapproved-effect"))
                    responses.respond(assistant_output("denied safely"))
                    successor = await s.submit("Same native tool call ID, new arguments")
                    cursor = s.interactions()
                    fresh = await anext(cursor)
                    await cursor.aclose()
                    assert isinstance(fresh, ApprovalRequest)
                    assert fresh.turn_id != turn.turn_id and not peer.calls
                    with pytest.raises(InteractionExpiredError, match="expired|answered"):
                        await request.respond("allow_once")
                    await fresh.respond("deny")
                    assert (await successor.get_result()).final_output == "denied safely"
                    assert not peer.calls
                else:
                    # Core may notify the real peer that the old request was cancelled.
                    # A response enqueue result never proves the peer has applied it.
                    assert all(
                        answer.get("result") == {"action": "cancel"} for answer in peer.answers
                    )
                    responses.respond(assistant_output("new turn"))
                    assert (
                        await (await s.submit("Continue without a form")).get_result()
                    ).final_output == "new turn"
        finally:
            await native.aclose()
