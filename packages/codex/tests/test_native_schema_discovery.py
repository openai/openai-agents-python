"""Core's own selected schema search, inside the finite opening ceiling."""

import json
from dataclasses import replace

import pytest

from agents import (
    Agent,
    CapabilityError,
    HttpMCP,
    InteractionRequired,
    NativeOutput,
    NativeProvider,
    PromptedOutput,
    ToolRef,
    Turn,
    function_tool,
)
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_deferred_jobs import Journal, Output, Provider, declared, done
from tests.test_deferred_prompted_output import FinalReport
from tests.test_native_manual_tools import next_request, provider
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


def search(query, call_id):
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "tool_search_call",
            "call_id": call_id,
            "execution": "client",
            "arguments": {"query": query, "limit": 5},
        },
    }


def discovered(model, call_id):
    return [
        i
        for i in model.requests[-1]["input"]
        if i.get("type") == "tool_search_output" and i.get("call_id") == call_id
    ][-1]


@pytest.mark.parametrize("deferred", [False, True])
async def test_only_current_schemas_and_effects_are_discoverable(responses, deferred):
    calls = []

    @function_tool(
        description="Find the skylark invoice using the current accounting roster",
        defer_loading=deferred,
    )
    async def skylark_invoice(order_id: str) -> str:
        calls.append(order_id)
        return "paid:" + order_id

    @function_tool(
        description="Destroy the skylark records, a reserved and dormant operation",
        defer_loading=deferred,
    )
    async def skylark_delete(secret_id: str) -> str:
        pytest.fail("Discovery granted an inactive tool")

    provider = NativeProvider(base_url=responses.url, api_key="synthetic-current")
    roster = [skylark_invoice, skylark_delete]
    responses.respond(search("skylark", "search-1"))
    responses.respond(function_call("skylark_invoice", {"order_id": "A1"}, "use-1"))
    responses.respond(assistant_output("settled"))
    agent = Agent(instructions="Act.", model="gpt-5.5", tools=roster, provider=provider)
    async with timeout(25), provider, agent.session() as session:
        assert (
            await (await session.submit("find", tools=[skylark_invoice])).get_result()
        ).final_output == "settled"
        assert calls == ["A1"]
        offered = json.dumps(responses.requests[0]["tools"])
        assert "skylark_delete" not in offered
        assert ("tool_search" in offered) == deferred
        result = json.dumps(discovered(responses, "search-1"))
        assert ("skylark_invoice" in result) == deferred
        assert "skylark_delete" not in result
        if deferred:

            @function_tool(name="skylark_invoice", description="New current skylark invoice schema")
            async def changed(amount: int) -> str:
                calls.append(amount)
                return "changed"

            with pytest.raises(CapabilityError, match="captured"):
                await session.submit("new schema", tools=[changed])
            assert calls == ["A1"] and len(responses.requests) == 3


@pytest.mark.parametrize("deferred", [False, True])
async def test_fork_retains_current_schema_and_opening_discovery_without_new_grants(
    responses, deferred
):
    calls = []

    @function_tool(description="Find the current skylark invoice", defer_loading=deferred)
    async def skylark_invoice(amount: int) -> str:
        assert amount == 13, "Fork reused the source callback instead of its current binding"
        calls.append(amount)
        return "paid:" + str(amount)

    @function_tool(description="Delete skylark invoices, never selected", defer_loading=deferred)
    async def skylark_delete(secret_id: str) -> str:
        pytest.fail("Fork granted an inactive operation")

    roster = [skylark_invoice, skylark_delete]
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-current")
    source = Agent(instructions="Act.", model="gpt-5.5", tools=roster, provider=provider)
    async with timeout(30), provider, source.session() as session:
        # Selection now belongs to a promoted step; it cannot replace a
        # captured schema through the removed idle setter.
        responses.respond(assistant_output("source selected"))
        assert (
            await (await session.submit("select", tools=[skylark_invoice])).get_result()
        ).final_output == "source selected"
        before = len(responses.requests)
        state = await session.fork()
        assert len(responses.requests) == before and not calls

        @function_tool(
            name="skylark_invoice",
            description="Find the current skylark invoice",
            defer_loading=deferred,
        )
        async def current_invoice(amount: int) -> str:
            assert amount == 12, "Source lost its own binding after fork"
            calls.append(amount)
            return "paid:" + str(amount)

        destination = Agent(
            instructions="Act.",
            model="gpt-5.5",
            tools=[current_invoice, skylark_delete],
            provider=provider,
        )
        async with destination.session(state=state) as restored:
            assert restored.id != session.id
            responses.respond(search("skylark invoice", "fork-schema"))
            responses.respond(function_call("skylark_invoice", {"amount": 12}, "fork-use"))
            responses.respond(assistant_output("done"))
            assert (
                await (await restored.submit("current", tools=[current_invoice])).get_result()
            ).final_output == "done"
            assert calls == [12]
            selected = json.dumps(discovered(responses, "fork-schema"))
            assert "skylark_delete" not in selected
            assert ("skylark_invoice" in selected) == deferred
            if deferred:
                assert "amount" in selected and "order_id" not in selected
        responses.respond(function_call("skylark_invoice", {"amount": 13}, "source-use"))
        responses.respond(assistant_output("source"))
        assert (
            await (await session.submit("still owned", tools=[skylark_invoice])).get_result()
        ).final_output == "source"
        assert calls == [12, 13]


async def test_supplied_compaction_keeps_current_deferred_python_selection(responses):
    calls = []

    @function_tool(description="Find the current skylark invoice", defer_loading=True)
    async def skylark_invoice(amount: int) -> str:
        calls.append(amount)
        return "current paid"

    @function_tool(description="Destroy skylark invoices, never selected", defer_loading=True)
    async def skylark_delete(secret_id: str) -> str:
        pytest.fail("Compaction activated an unselected tool")

    roster = [skylark_invoice, skylark_delete]
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-current")
    agent = Agent(instructions="Act.", model="gpt-5.5", tools=roster, provider=provider)
    async with timeout(30), provider, agent.session() as session:
        # Idle opening and settled operation both keep the admitted schema
        # presentation. A supplied summary makes no model request of its own.
        await session.compact(summary="No work has been done yet.")
        assert not responses.requests
        responses.respond(assistant_output("settled"))
        assert (
            await (
                await session.submit("Remember skylark invoices.", tools=[skylark_invoice])
            ).get_result()
        ).final_output == ("settled")

        before = len(responses.requests)
        await session.compact(summary="Only the current skylark invoice is selected.")
        assert len(responses.requests) == before
        responses.respond(search("skylark invoice", "compacted-schema"))
        responses.respond(function_call("skylark_invoice", {"amount": 12}, "current-call"))
        responses.respond(assistant_output("paid"))
        assert (
            await (await session.submit("Find the invoice.", tools=[skylark_invoice])).get_result()
        ).final_output == "paid"

        first = responses.requests[before]
        assert "tool_search" in json.dumps(first["tools"])
        assert ("functions", "skylark_invoice") not in declarations(first)
        found = json.dumps(discovered(responses, "compacted-schema"))
        assert "skylark_invoice" in found and "amount" in found
        assert "order_id" not in found and "skylark_delete" not in found
        assert calls == [12]
        assert "current paid" in json.dumps(responses.requests[-1]["input"])


async def test_mcp_discovery_activation_and_current_restore(responses):
    old = Peer(names=("lookup", "unrelated"), label="old")
    destination = Peer(names=("lookup", "unrelated"), label="destination-current")
    async with timeout(35), old.serve() as old_url, destination.serve() as dest_url:
        remote = HttpMCP(
            name="files",
            url=old_url,
            bearer_token="old-private",
            tools=["lookup"],
            approval_mode="approve",
            defer_loading=True,
        )
        roster = [remote]
        provider = NativeProvider(base_url=responses.url, api_key="model-key")
        agent = Agent(
            instructions="Use current tools.", tools=roster, model="gpt-5.5", provider=provider
        )
        assert agent.validate() is None
        assert not old.requests and not destination.requests
        async with provider:
            async with agent.session() as source:
                assert not old.requests
                responses.respond(search("synthetic record lookup", "dormant-search"))
                responses.respond(assistant_output("no selection"))
                assert (
                    await (await source.submit("lookup", tools=[])).get_result()
                ).final_output == "no selection"
                assert not old.requests
                assert discovered(responses, "dormant-search")["tools"] == []
                responses.respond(search("synthetic record lookup", "current-search"))
                responses.respond(mcp_call("lookup", "mcp__files", "current-call"))
                responses.respond(assistant_output("source done"))
                assert (
                    await (
                        await source.submit("source", tools=[remote.tool("lookup")])
                    ).get_result()
                ).final_output == "source done"
                result = json.dumps(discovered(responses, "current-search"))
                assert "lookup" in result and "unrelated" not in result
                assert len(old.calls) == 1
                state = await source.save_state()
            fresh = HttpMCP(
                name="files",
                url=dest_url,
                bearer_token="destination-private",
                tools=["lookup"],
                approval_mode="approve",
                defer_loading=True,
            )
            fresh_roster = [fresh]
            restored = Agent(
                instructions="Use current tools.",
                tools=fresh_roster,
                model="gpt-5.5",
                provider=provider,
            )
            async with restored.session(state=state) as current:
                assert not destination.requests
                responses.respond(search("synthetic record lookup", "restored-dormant"))
                responses.respond(assistant_output("no leaked grants"))
                assert (
                    await (await current.submit("check", tools=[])).get_result()
                ).final_output == "no leaked grants"
                assert not destination.requests and len(old.calls) == 1
                assert discovered(responses, "restored-dormant")["tools"] == []
                with pytest.raises(CapabilityError):
                    await current.submit("unknown", tools=[ToolRef("unrelated", "mcp__files")])
                assert not destination.requests
                responses.respond(search("synthetic record lookup", "destination-search"))
                responses.respond(mcp_call("lookup", "mcp__files", "destination-call"))
                responses.respond(assistant_output("destination done"))
                assert (
                    await (await current.submit("current")).get_result()
                ).final_output == "destination done"
                result = json.dumps(discovered(responses, "destination-search"))
                assert "lookup" in result and "unrelated" not in result
                assert len(destination.calls) == 1 and len(old.calls) == 1
                assert set(destination.auth) == {"Bearer destination-private"}
                assert "destination-current" in json.dumps(responses.requests[-1])


async def test_code_search_cannot_widen_peer_names(responses):
    peer = Peer(names=("lookup", "unrelated"))
    async with timeout(35), peer.serve() as url:
        remote = HttpMCP(
            name="files", url=url, tools=["lookup"], approval_mode="approve", defer_loading=True
        )
        provider = NativeProvider(base_url=responses.url, api_key="code-search-key")
        agent = Agent(
            instructions="Use current tools.",
            tools=[remote],
            model="gpt-5.5",
            provider=provider,
            programmatic_tool_calling=True,
        )
        responses.respond(search("synthetic record lookup unrelated", "code-search"))
        responses.respond(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "call_id": "exec-current",
                    "name": "exec",
                    "input": "text({selected:await tools.mcp__files__lookup({key:'K'}),"
                    "not_granted: typeof tools.mcp__files__unrelated})",
                },
            }
        )
        responses.respond(assistant_output("code done"))
        async with provider, agent.session() as current:
            assert (
                await (await current.submit("current")).get_result()
            ).final_output == "code done"
        result = json.dumps(discovered(responses, "code-search"))
        assert "lookup" in result and "unrelated" not in result
        assert [c["name"] for c in peer.calls] == ["lookup"]
        assert "undefined" in json.dumps(responses.requests[-1])


async def test_discovery_cannot_be_added_after_open_or_activate_unknown_tool(responses):
    @function_tool
    async def admitted() -> str:
        return "only admitted"

    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    async with timeout(25), provider:
        async with Agent(
            instructions="Act", tools=[admitted], provider=provider
        ).session() as current:
            with pytest.raises(CapabilityError, match="captured"):
                await current.submit("New discovery", tools=[replace(admitted, defer_loading=True)])
            assert not responses.requests
            responses.respond(function_call("admitted", {}, "original"))
            responses.respond(assistant_output("original survived"))
            assert (
                await (await current.submit("original")).get_result()
            ).final_output == "original survived"


async def test_native_default_model_keeps_selected_tools_usable(responses, tmp_path):
    @function_tool(description="Get a known skylark record", defer_loading=True)
    async def skylark() -> str:
        return "bound callback"

    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    agent = Agent(
        instructions="Act.",
        tools=[skylark],
        provider=provider,
    )
    responses.respond(function_call("skylark", {}, "default-invoke"))
    responses.respond(assistant_output("catalog"))
    async with timeout(25), provider, agent.session() as session:
        assert (await (await session.submit("inventory")).get_result()).final_output == "catalog"
        request = responses.requests[0]
        (tmp_path / "native-default-request.json").write_text(json.dumps(request, indent=2))
        assert ("functions", "skylark") in declarations(request), request
        assert "tool_search" not in json.dumps(request)
        assert len(responses.requests) == 2
        assert "bound callback" in json.dumps(responses.requests[1]["input"])


async def test_native_model_change_adapts_only_the_current_python_and_mcp_roster(responses):
    calls = []

    @function_tool(description="Find the enabled skylark invoice", defer_loading=True)
    async def skylark_invoice(key: str) -> str:
        calls.append(key)
        return "paid:" + key

    @function_tool(description="Delete the skylark invoices", defer_loading=True)
    async def skylark_delete(key: str) -> str:
        pytest.fail("Unselected tool was activated")

    peer = Peer(names=("lookup", "hidden"), label="current-source")
    async with timeout(40), peer.serve() as url:
        remote = HttpMCP(
            name="records", url=url, tools=["lookup"], approval_mode="approve", defer_loading=True
        )
        roster = [skylark_invoice, skylark_delete, remote]
        provider = NativeProvider(base_url=responses.url, api_key="native-default-and-change")
        agent = Agent(instructions="Use only current tools", tools=roster, provider=provider)
        async with provider, agent.session() as session:
            responses.respond(assistant_output("original model"))
            await (await session.submit("offered", tools=[skylark_invoice, remote])).get_result()
            default_request = responses.requests[0]
            default_model = default_request["model"]
            assert set(declarations(default_request)) == {
                ("functions", "skylark_invoice"),
                ("mcp__records", "lookup"),
            }, default_request
            assert "tool_search" not in json.dumps(default_request)
            assert not peer.calls and not calls
            await session.update(model="gpt-5.5")
            # Core compacts with the previous model when the native comp_hash
            # changes. This is a distinct request, not the new model's catalog.
            responses.respond(assistant_output("Only the current granted tools are available."))
            responses.respond(search("skylark invoice", "capable-search"))
            responses.respond(function_call("skylark_invoice", {"key": "first"}, "invoice-now"))
            responses.respond(assistant_output("capable"))
            capable_start = len(responses.requests)
            await (await session.submit("search", tools=[skylark_invoice, remote])).get_result()
            compaction = responses.requests[capable_start]
            assert compaction["model"] == default_model
            assert not compaction.get("tools")
            first = responses.requests[capable_start + 1]
            assert first["model"] == "gpt-5.5"
            assert "tool_search" in json.dumps(first.get("tools", [])), first
            assert ("functions", "skylark_invoice") not in declarations(first)
            found = json.dumps(discovered(responses, "capable-search"))
            assert "skylark_invoice" in found and "skylark_delete" not in found
            assert calls == ["first"]
            await session.update(model=default_model)
            responses.respond(assistant_output("The only selected tool is current records lookup."))
            responses.respond(mcp_call("lookup", "mcp__records", "after-change"))
            responses.respond(assistant_output("back to default"))
            changed_start = len(responses.requests)
            await (await session.submit("current selection", tools=[remote])).get_result()
            compaction = responses.requests[changed_start]
            assert compaction["model"] == "gpt-5.5"
            assert not compaction.get("tools")
            changed = responses.requests[changed_start + 1]
            assert changed["model"] == default_model
            assert set(declarations(changed)) == {("mcp__records", "lookup")}
            assert len(peer.calls) == 1 and calls == ["first"]
            await session.update(model="gpt-5.5")
            responses.respond(assistant_output("Only the current records lookup remains selected."))
            responses.respond(search("synthetic lookup", "current-search"))
            responses.respond(assistant_output("only current remote schema"))
            await (await session.submit("offered now", tools=[remote])).get_result()
            found = json.dumps(discovered(responses, "current-search"))
            assert "lookup" in found
            assert "hidden" not in found and "skylark" not in found
            assert len(peer.calls) == 1 and calls == ["first"]


async def test_discovered_prompted_deferred_tool_keeps_identity_and_current_restored_grants(
    responses,
):
    journal, backend = Journal(), Provider([done("job-rendered")])
    jobs, tool = declared(journal, backend, defer_loading=True)

    @function_tool(
        description="Render a private destructive job; never selected", defer_loading=True
    )
    async def render_delete(pages: int) -> str:
        pytest.fail("Discovery activated the dormant tool")

    roster = [tool, render_delete]
    context = object()
    default = NativeOutput(str)
    agent = Agent(
        instructions="Render.",
        model="gpt-5.5",
        tools=roster,
        provider=provider(responses),
        output_type=default,
    )
    responses.respond(search("render pages", "render-schema"))
    responses.respond(function_call("render", {"pages": 2}, "render-once"))
    responses.respond(assistant_output('{"report":"typed prompted","pages":2}'))
    responses.respond(assistant_output('{"response":"future native"}'))
    async with timeout(30), agent.session(context=context) as source:
        original = await source.submit(
            "Render", tools=[tool], output_type=PromptedOutput(FinalReport)
        )
        request = await next_request(source.interactions(), tool)
        result = json.dumps(discovered(responses, "render-schema"))
        assert "render" in result and "render_delete" not in result
        assert not backend.calls
        async with jobs.start(source, request) as (current, outcome):
            assert isinstance(outcome, Turn) and outcome is current.restored_turn
            with pytest.raises((ValueError, RuntimeError)):
                await request.respond(Output(report="stale"))
            assert (await outcome.get_result()).final_output == FinalReport(
                report="typed prompted", pages=2
            )
            assert (
                await (await current.submit("next")).get_result()
            ).final_output == "future native"
        with pytest.raises(InteractionRequired):
            await original.get_result()
    assert backend.calls == [("start", 2)] and backend.active == 0
    assert journal.log.count("prepare") == journal.log.count("save_result") == 1
    assert len(responses.requests) == 4


@pytest.mark.parametrize("invalid_receipt", ["missing", "wrong_id", "server", "unfinished"])
async def test_search_receipt_is_required_before_manual_restore(responses, invalid_receipt):
    journal, backend = Journal(), Provider([done("should-not-run")])
    jobs, tool = declared(journal, backend, defer_loading=True)
    agent = Agent(
        instructions="Render",
        model="gpt-5.5",
        tools=[tool],
        provider=provider(responses),
    )
    responses.respond(search("render", "schema-receipt"))
    responses.respond(function_call("render", {"pages": 2}, "pending-render"))
    async with agent.session() as source:
        original = await source.submit("Prepare")
        request = await next_request(source.interactions(), tool)
        saved = await jobs.prepare(source, request)
        with pytest.raises(InteractionRequired):
            await original.get_result()
    raw = json.loads(saved.to_json())
    receipts = [
        row
        for row in raw["history"]
        if row["type"] == "response_item"
        and row["payload"]["type"] == "tool_search_output"
        and row["payload"]["call_id"] == "schema-receipt"
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    if invalid_receipt == "missing":
        raw["history"].remove(receipt)
    elif invalid_receipt == "wrong_id":
        receipt["payload"]["call_id"] = "not-the-native-call"
    elif invalid_receipt == "server":
        receipt["payload"]["execution"] = "server"
    else:
        receipt["payload"]["status"] = "in_progress"
    changed = SessionState.from_json(json.dumps(raw))
    before = len(responses.requests)
    with pytest.raises((ValueError, RuntimeError), match="nonportable|portable"):
        async with agent.session(state=changed):
            pytest.fail("Incomplete search became a restored manual session")
    assert len(responses.requests) == before and not backend.calls


async def test_mcp_presentation_stays_with_its_captured_object(responses):
    delayed = Peer(names=("lookup", "hidden"), label="delayed")
    immediate = Peer(names=("search",), label="immediate")
    async with timeout(30), delayed.serve() as delayed_url, immediate.serve() as immediate_url:
        lazy = HttpMCP(name="lazy", url=delayed_url, tools=["lookup", "hidden"], defer_loading=True)
        eager = HttpMCP(name="eager", url=immediate_url, tools=["search"])
        agent = Agent(
            instructions="Use only admitted leaves",
            tools=[lazy, eager],
            model="gpt-5.5",
            provider=provider(responses),
        )
        async with agent.session() as session:
            assert not delayed.requests and not immediate.requests
            responses.respond(search("synthetic record lookup", "lazy-search"))
            responses.respond(mcp_call("lookup", "mcp__lazy", "lazy-effect"))
            responses.respond(mcp_call("search", "mcp__eager", "direct-effect"))
            responses.respond(mcp_call("hidden", "mcp__lazy", "forbidden-effect"))
            responses.respond(assistant_output("finished"))
            assert (
                await (
                    await session.submit("Read", tools=[lazy.tool("lookup"), eager])
                ).get_result()
            ).final_output == "finished"
            first = responses.requests[0]
            assert ("mcp__eager", "search") in declarations(first)
            assert ("mcp__lazy", "lookup") not in declarations(first)
            assert "tool_search" in json.dumps(first["tools"])
            found = json.dumps(discovered(responses, "lazy-search"))
            assert "lookup" in found and "hidden" not in found
            assert [call["_meta"]["callId"] for call in delayed.calls] == ["lazy-effect"]
            assert [call["_meta"]["callId"] for call in immediate.calls] == ["direct-effect"]


@pytest.mark.parametrize("family", ["python", "mcp"])
async def test_empty_with_deferred_opening_is_really_empty_or_refuses_before_sampling(
    responses, family
):
    calls = []
    peer = Peer()

    @function_tool(defer_loading=True)
    def tool() -> str:
        calls.append("tool")
        return "effect"

    async with timeout(20), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"], defer_loading=True)
        async with Agent(
            instructions="Honor an empty operation",
            tools=[tool] if family == "python" else [remote],
            model="gpt-5.5",
            provider=provider(responses),
        ).session() as session:
            responses.respond(assistant_output("empty"))
            try:
                turn = await session.submit("None permitted", tools=[])
            except CapabilityError:
                assert not responses.requests and not calls and not peer.requests
            else:
                await turn.get_result()
                assert responses.requests[0].get("tools", []) == []
                assert not calls and not peer.requests
