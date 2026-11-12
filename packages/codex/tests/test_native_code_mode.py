"""The bundled code host must consume only current, accepted tool values."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from xml.etree import ElementTree

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    Allow,
    FunctionTool,
    HttpMCP,
    MCPResultContext,
    NativeProvider,
    ToolCallInfo,
    ToolContext,
    ToolRef,
    TurnStoppedError,
    function_tool,
)
from agents.realtime import realtime_handoff
from examples.codex.validated_artifact import ArtifactContext, publish_xml
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer


def code_call(code: str, call_id: str) -> dict[str, Any]:
    # This is the Responses custom-tool envelope. The SDK and bundled native
    # host, including nested-tool dispatch, run without a simulated executor.
    return {
        "type": "response.output_item.done",
        "item": {"type": "custom_tool_call", "name": "exec", "call_id": call_id, "input": code},
    }


def code_output(request: dict[str, Any], call_id: str) -> str:
    return tool_output(request, call_id, "custom_tool_call_output")


def declared_names(request: dict[str, Any]) -> set[str]:
    names = set()
    tools = list(request.get("tools", []))
    for item in request.get("input", []):
        if item.get("type") == "additional_tools":
            tools.extend(item["tools"])
    for group in tools:
        if group["type"] == "namespace":
            names.update(item["name"] for item in group["tools"])
        elif "name" in group:
            names.add(group["name"])
    return names


async def test_code_mode_excludes_handoffs_but_keeps_direct_profile_transition(
    responses: ResponsesFixture,
) -> None:
    class TransferInput(BaseModel):
        reason: str

    effects: list[str] = []
    first = Agent(name="B", instructions="Destination B", programmatic_tool_calling=True)
    second = Agent(name="C", instructions="Destination C", programmatic_tool_calling=True)
    source = Agent(
        name="A",
        instructions="Original source",
        provider=provider(responses),
        programmatic_tool_calling=True,
        handoffs=[
            realtime_handoff(
                first,
                name="route_b",
                input_type=TransferInput,
                on_handoff=lambda _context, _payload: effects.append("B"),
            ),
            realtime_handoff(
                second,
                name="route_c",
                input_type=TransferInput,
                on_handoff=lambda _context, _payload: effects.append("C"),
            ),
        ],
    )
    responses.respond(
        code_call(
            'for (const name of ["route_b", "route_c"]) { '
            'try { await tools[name]({reason:"nested"}); } '
            "catch (error) { text(String(error)); } }",
            "nested-handoffs",
        )
    )
    responses.respond(assistant_output("Source response"))
    responses.respond(function_call("route_b", {"reason": "direct"}, "direct-handoff"))
    responses.respond(assistant_output("Destination response"))
    async with timeout(45), source.session() as session:
        result = await (await session.submit("Try both nested handoffs")).get_result()
        assert result.final_output == "Source response"
        assert await session.get_active_agent() is source
        assert effects == []

        direct = await (await session.submit("Transfer directly to B")).get_result()
        assert direct.final_output == "Destination response"
        assert await session.get_active_agent() is first
        assert effects == ["B"]


async def test_code_mode_python_validation_runs_before_immediate_code_consumers(
    responses: ResponsesFixture,
) -> None:
    seen: list[tuple[str, str]] = []
    policy_calls: list[ToolCallInfo] = []
    callback_calls: list[tuple[ToolCallInfo, str]] = []

    def before(ctx: ToolContext[None]) -> Allow:
        policy_calls.append(ctx.call)
        return Allow()

    @function_tool(before=before)
    async def lookup(ctx: ToolContext[None], key: str) -> str:
        callback_calls.append((ctx.call, ctx.call_id))
        seen.append(("requested", key))
        effective = "owned-tenant"
        seen.append(("effect", effective))
        record = {"tenant": effective, "private": "python-private-73bde"}
        seen.append(("validated", record["tenant"]))
        return record["tenant"] + "-accepted"

    responses.respond(
        code_call('text("CODE_RESULT:" + JSON.stringify(await tools.lookup({key:"raw"})));', "c1")
    )
    responses.respond(assistant_output("first done"))
    # Direct calls use the same tool body even when code mode is enabled.
    responses.respond(function_call("lookup", {"key": "direct"}, "direct"))
    responses.respond(assistant_output("direct done"))
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(45), agent.session() as session:
        first = await session.submit("Lookup through generated code.")
        assert (await (first).get_result()).final_output == "first done"
        assert "owned-tenant-accepted" in code_output(responses.requests[1], "c1")
        direct = await session.submit("Lookup directly.")
        assert (await (direct).get_result()).final_output == "direct done"
        assert "owned-tenant-accepted" in tool_output(responses.requests[3], "direct")
        items = [entry async for entry in session.items()]
        saved = (await session.save_state()).to_json()

    assert [info.route for info in policy_calls] == ["code", "direct"]
    assert [info for info, _ in callback_calls] == policy_calls
    code, direct_info = policy_calls
    assert code.call_id is None
    assert code.cell_id is not None and code.nested_call_id is not None
    # Core supplies no outer provider ID to its Python ToolExecutor.
    assert code.outer_call_id is None
    assert direct_info.call_id == "direct"
    assert direct_info.cell_id is direct_info.nested_call_id is direct_info.outer_call_id is None
    assert callback_calls[1][1] == "direct"
    assert callback_calls[0][1]
    assert seen == [
        ("requested", "raw"),
        ("effect", "owned-tenant"),
        ("validated", "owned-tenant"),
        ("requested", "direct"),
        ("effect", "owned-tenant"),
        ("validated", "owned-tenant"),
    ]
    assert "exec" in declared_names(responses.requests[0])
    assert "python-private-73bde" not in json.dumps(responses.requests)
    assert "python-private-73bde" not in json.dumps(items)
    assert "python-private-73bde" not in saved


@pytest.mark.parametrize("deferred", [False, True], ids=["eager", "deferred"])
async def test_function_tool_callers_control_schema_and_native_dispatch(
    responses: ResponsesFixture, deferred: bool
) -> None:
    seen: list[tuple[str, str]] = []

    def before(ctx: ToolContext[None]) -> Allow:
        seen.append(("code-only policy", ctx.call.route))
        return Allow()

    @function_tool(allowed_callers=("code",), defer_loading=deferred, before=before)
    def code_only() -> str:
        seen.append(("code-only body", "accepted"))
        return "code-only accepted"

    @function_tool(allowed_callers=("direct",))
    def direct_only() -> str:
        seen.append(("direct-only body", "accepted"))
        return "direct-only accepted"

    @function_tool(allowed_callers=("code", "direct"))
    def either() -> str:
        seen.append(("either body", "accepted"))
        return "either route accepted"

    agent = Agent(
        instructions="Call the tools through their permitted routes.",
        model="gpt-5.5",
        tools=[code_only, direct_only, either],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    responses.respond(function_call("code_only", {}, "forged-direct"))
    responses.respond(assistant_output("Direct call blocked."))
    responses.respond(
        code_call(
            "try { text(await tools.direct_only({})); } "
            'catch { text("nested direct call blocked"); } '
            "text(await tools.code_only({})); text(await tools.either({}));",
            "caller-routes",
        )
    )
    responses.respond(assistant_output("Code call finished."))
    responses.respond(function_call("direct_only", {}, "allowed-direct"))
    responses.respond(assistant_output("Direct call finished."))
    responses.respond(function_call("either", {}, "either-direct"))
    responses.respond(assistant_output("Either route finished."))
    responses.respond(function_call("code_only", {}, "restored-forged-direct"))
    responses.respond(assistant_output("Restored direct call blocked."))

    async with timeout(45), agent.session() as session:
        first = await session.submit("Try a forged direct call.")
        assert (await first.get_result()).final_output == "Direct call blocked."
        assert seen == []
        assert {"direct_only", "either", "exec"} <= declared_names(responses.requests[0])
        assert "code_only" not in declared_names(responses.requests[0])
        assert "code-only accepted" not in tool_output(responses.requests[1], "forged-direct")

        code = await session.submit("Try both tools in a code cell.")
        assert (await code.get_result()).final_output == "Code call finished."
        output = code_output(responses.requests[3], "caller-routes")
        assert "code-only accepted" in output
        assert "either route accepted" in output
        assert "nested direct call blocked" in output
        assert "direct-only accepted" not in output

        direct = await session.submit("Call the direct tool.")
        assert (await direct.get_result()).final_output == "Direct call finished."
        assert "direct-only accepted" in tool_output(responses.requests[5], "allowed-direct")

        either_direct = await session.submit("Call the tool that allows both routes.")
        assert (await either_direct.get_result()).final_output == "Either route finished."
        assert "either route accepted" in tool_output(responses.requests[7], "either-direct")
        saved = await session.save_state()

    async with timeout(45), agent.session(state=saved) as restored:
        blocked = await restored.submit("Try a forged direct call after restore.")
        assert (await blocked.get_result()).final_output == "Restored direct call blocked."
        assert "code-only accepted" not in tool_output(
            responses.requests[9], "restored-forged-direct"
        )

    assert seen == [
        ("code-only policy", "code"),
        ("code-only body", "accepted"),
        ("either body", "accepted"),
        ("direct-only body", "accepted"),
        ("either body", "accepted"),
    ]


async def test_function_tool_callers_follow_the_restored_handoff_profile(
    responses: ResponsesFixture,
) -> None:
    calls: list[str] = []

    @function_tool(allowed_callers=("code",))
    def worker_only(ctx: ToolContext[None]) -> str:
        calls.append(ctx.call.route)
        return "worker tool accepted"

    current = provider(responses)
    worker = Agent(
        name="Worker",
        model="gpt-5.5",
        instructions="Use the worker tool.",
        tools=[worker_only],
        provider=current,
        programmatic_tool_calling=True,
    )
    root = Agent(
        name="Root",
        model="gpt-5.5",
        instructions="Hand off to Worker.",
        handoffs=[worker],
        provider=current,
        programmatic_tool_calling=True,
    )
    responses.respond(function_call("transfer_to_worker", {}, "handoff"))
    responses.respond(assistant_output("Worker ready."))
    responses.respond(function_call("worker_only", {}, "restored-direct"))
    responses.respond(assistant_output("Direct call blocked."))
    responses.respond(code_call("text(await tools.worker_only({}));", "restored-code"))
    responses.respond(assistant_output("Code call finished."))

    async with timeout(45), root.session() as source:
        assert (await (await source.submit("Switch to Worker.")).get_result()).final_output
        assert await source.get_active_agent() is worker
        saved = await source.save_state()

    async with timeout(45), root.session(state=saved) as restored:
        direct = await restored.submit("Try direct.")
        assert (await direct.get_result()).final_output == "Direct call blocked."
        assert calls == []
        assert "worker_only" not in declared_names(responses.requests[2])
        code = await restored.submit("Use a code cell.")
        assert (await code.get_result()).final_output == "Code call finished."
        assert "worker tool accepted" in code_output(responses.requests[5], "restored-code")

    assert calls == ["code"]


async def test_function_tool_callers_apply_to_custom_string_tools(
    responses: ResponsesFixture,
) -> None:
    raw_input = "  unquoted string  "
    calls: list[tuple[str, str]] = []

    @function_tool(allowed_callers=("code",))
    def raw_editor(ctx: ToolContext[None], source: str) -> str:
        calls.append((ctx.call.route, source))
        return "raw string accepted"

    agent = Agent(
        instructions="Use the raw editor.",
        model="gpt-5.5",
        tools=[raw_editor.as_custom()],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "raw_editor",
                "call_id": "forged-raw",
                "input": raw_input,
            },
        }
    )
    responses.respond(assistant_output("Direct call blocked."))
    responses.respond(code_call(f"text(await tools.raw_editor({json.dumps(raw_input)}));", "cell"))
    responses.respond(assistant_output("Code call finished."))

    async with timeout(45), agent.session() as session:
        direct = await session.submit("Try direct.")
        assert (await direct.get_result()).final_output == "Direct call blocked."
        assert "raw_editor" not in declared_names(responses.requests[0])
        assert calls == []
        code = await session.submit("Use a code cell.")
        assert (await code.get_result()).final_output == "Code call finished."
        assert "raw string accepted" in code_output(responses.requests[3], "cell")

    assert calls == [("code", raw_input)]


async def test_async_native_mcp_policy_precedes_code_and_preserves_unselected_tools(
    responses: ResponsesFixture,
) -> None:
    protected = Peer(names=("find-record",), label="original-private-mcp-c046")
    plain = Peer(names=("find-record",), label="plain-mcp-value-189d")
    entered = asyncio.Event()
    proceed = asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def accept(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        seen.append((ctx.server_name, ctx.tool_name))
        assert value["structuredContent"] == {"receipt": protected.label}
        entered.set()
        await proceed.wait()
        return {
            "content": [{"type": "text", "text": "authorized-mcp-result-2fa1"}],
            "structuredContent": {"receipt": "authorized-mcp-result-2fa1"},
            "isError": False,
        }

    responses.respond(
        code_call(
            'const privateValue=await tools.mcp__vault__find_record({key:"A"});'
            'const ordinary=await tools.mcp__plain__find_record({key:"B"});'
            'text("CONSUMED:" + JSON.stringify([privateValue,ordinary]));',
            "mcp-code",
        )
    )
    responses.respond(assistant_output("done"))
    async with timeout(45), protected.serve() as private_url, plain.serve() as plain_url:
        agent = Agent(
            instructions="Read selected MCP tools.",
            tools=[
                HttpMCP(
                    name="vault",
                    url=private_url,
                    tools=["find-record"],
                    transform_result=accept,
                ),
                HttpMCP(name="plain", url=plain_url, tools=["find-record"]),
            ],
            model="gpt-5.5",
            provider=provider(responses),
            programmatic_tool_calling=True,
        )
        async with agent.session() as session:
            handle = await session.submit("Read selected records through code.")
            try:
                await entered.wait()
                assert len(responses.requests) == 1
                assert len(protected.calls) == 1
                assert plain.calls == []
            finally:
                proceed.set()
            assert (await (handle).get_result()).final_output == "done"
            accepted = code_output(responses.requests[1], "mcp-code")
            assert "authorized-mcp-result-2fa1" in accepted
            assert plain.label in accepted
            assert protected.label not in accepted
            assert seen == [("vault", "find-record")]
            assert [(call["name"], call["arguments"]) for call in protected.calls] == [
                ("find-record", {"key": "A"})
            ]
            assert [(call["name"], call["arguments"]) for call in plain.calls] == [
                ("find-record", {"key": "B"})
            ]
            items = [entry async for entry in session.items()]
            saved = (await session.save_state()).to_json()
    for channel in (json.dumps(responses.requests), json.dumps(items), saved):
        assert protected.label not in channel


async def test_code_validation_failure_returns_feedback_and_model_repairs_in_same_turn(
    responses: ResponsesFixture, tmp_path
) -> None:
    invalid = "<presentation><slide></presentation>"
    valid = "<presentation><slide>Simple explanation.</slide></presentation>"
    output_path = tmp_path / "presentation.xml"
    ctx = ArtifactContext(memory={}, output_path=output_path)
    responses.respond(
        code_call(f"text(await tools.publish_xml({{xml:{json.dumps(invalid)}}}));", "draft")
    )
    responses.respond(
        code_call(f"text(await tools.publish_xml({{xml:{json.dumps(valid)}}}));", "repair")
    )
    responses.respond(assistant_output("ready"))
    saw_rejection = False

    async def before(event: dict[str, Any]) -> None:
        nonlocal saw_rejection
        if event["type"] == "response.created" and len(responses.requests) == 2:
            assert "XML is not well formed" in code_output(responses.requests[-1], "draft")
            assert not output_path.exists()
            saw_rejection = True

    responses.before_event = before
    agent = Agent[ArtifactContext, str](
        instructions="Publish a validated artifact and repair failures.",
        tools=[publish_xml],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(45), agent.session(context=ctx) as session:
        handle = await session.submit("Produce the presentation.")
        assert (await (handle).get_result()).final_output == "ready"
    assert saw_rejection
    assert len(responses.requests) == 3
    assert "Published validated XML" in code_output(responses.requests[2], "repair")
    assert output_path.read_text(encoding="utf-8") == valid
    assert ElementTree.parse(output_path).getroot().find("slide").text == "Simple explanation."


@pytest.mark.parametrize("kind", ["python", "mcp"])
async def test_stopping_a_cell_awaiting_tool_processing_discards_late_result(
    responses: ResponsesFixture, kind: str
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    proceed = asyncio.Event()
    peer = Peer(names=("search",), label="raw-value-63c1")
    effects: list[str] = []

    async def wait_policy() -> None:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await proceed.wait()

    async def mcp_policy(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        await wait_policy()
        return {"content": [{"type": "text", "text": "unaccepted-late-value-1457"}]}

    @function_tool
    async def lookup(key: str) -> str:
        effects.append(key)
        await wait_policy()
        return "unaccepted-late-value-1457"

    async with timeout(45), peer.serve() as url:
        tools = (
            [lookup]
            if kind == "python"
            else [HttpMCP(name="vault", url=url, tools=["search"], transform_result=mcp_policy)]
        )
        tool_name = "lookup" if kind == "python" else "mcp__vault__search"
        responses.respond(code_call(f'text(await tools.{tool_name}({{key:"A"}}));', "awaiting"))
        agent = Agent(
            instructions="Read.",
            tools=tools,
            provider=provider(responses),
            programmatic_tool_calling=True,
        )
        async with agent.session() as session:
            handle = await session.submit("Read.")
            stopping = None
            try:
                await entered.wait()
                with pytest.raises(RuntimeError, match="requires an idle Session"):
                    await session.save_state()
                stopping = asyncio.create_task(handle.cancel())
                await cancelled.wait()
                assert not stopping.done()
            finally:
                proceed.set()
                if stopping is not None:
                    await stopping
            with pytest.raises(TurnStoppedError):
                await handle.get_result()
            assert len(responses.requests) == 1
            # The same session remains usable, without committing late output
            # into the next request or replaying the original side effect.
            responses.respond(assistant_output("continued"))
            second = await session.submit("Continue without any tool.")
            assert (await (second).get_result()).final_output == "continued"
            items = [entry async for entry in session.items()]
    assert effects == (["A"] if kind == "python" else [])
    assert len(peer.calls) == (1 if kind == "mcp" else 0)
    assert "unaccepted-late-value-1457" not in json.dumps(responses.requests)
    assert "unaccepted-late-value-1457" not in json.dumps(items)
    assert "raw-value-63c1" not in json.dumps(responses.requests)


async def test_code_consumers_obey_current_catalog_and_opening_ceiling(
    responses: ResponsesFixture,
) -> None:
    effects: list[str] = []

    @function_tool
    async def lookup() -> str:
        effects.append("lookup")
        return "declared-value"

    @function_tool
    async def forbidden() -> str:
        effects.append("forbidden")
        return "not-allowed"

    agent = Agent(
        instructions="Use available tools.",
        tools=[lookup],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(45), agent.session() as session:
        responses.respond(code_call("text(await tools.lookup({}));", "initial"))
        responses.respond(assistant_output("initial"))
        initial = await session.submit("Initial lookup.")
        await (initial).get_result()
        assert "declared-value" in code_output(responses.requests[1], "initial")
        with pytest.raises(ValueError, match="captured"):
            await session.submit("select", tools=[forbidden])
        responses.respond(
            code_call(
                "text(JSON.stringify([typeof tools.lookup, typeof tools.forbidden]));",
                "absent",
            )
        )
        responses.respond(assistant_output("absent"))
        absent = await session.submit(
            "Inspect current tools.",
            tools=[ToolRef("exec", "functions"), ToolRef("wait", "functions")],
        )
        await (absent).get_result()
        receipt = code_output(responses.requests[3], "absent")
        assert receipt.count("undefined") == 2
        assert effects == ["lookup"]
        responses.respond(code_call("text(await tools.lookup({}));", "restored"))
        responses.respond(assistant_output("restored"))
        restored = await session.submit("Lookup again.")
        await (restored).get_result()
        assert "declared-value" in code_output(responses.requests[5], "restored")
    assert effects == ["lookup", "lookup"]


@pytest.mark.parametrize("enable_after_restore", [False, True])
async def test_idle_restore_uses_current_code_mode_declaration_and_provider(
    responses: ResponsesFixture, enable_after_restore: bool
) -> None:
    effects: list[str] = []

    @function_tool
    async def lookup(key: str) -> str:
        effects.append(key)
        return "reply-for-" + key

    first_provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-old-code-mode-key", supports_websockets=False
    )
    responses.respond(code_call('text(await tools.lookup({key:"before"}));', "initial"))
    responses.respond(assistant_output("saved"))
    original = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=first_provider,
        programmatic_tool_calling=True,
    )
    async with timeout(45), original.session() as session:
        initial = await session.submit("Remember the first call.")
        await (initial).get_result()
        saved = await session.save_state()
    assert "synthetic-old-code-mode-key" not in saved.to_json()
    assert effects == ["before"]

    current_provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-current-code-mode-key", supports_websockets=False
    )
    if enable_after_restore:
        responses.respond(code_call('text(await tools.lookup({key:"after"}));', "second"))
    else:
        responses.respond(function_call("lookup", {"key": "after"}, "second"))
    responses.respond(assistant_output("continued"))
    restored = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=current_provider,
        programmatic_tool_calling=enable_after_restore,
    )
    async with timeout(45), restored.session(state=saved) as session:
        second = await session.submit("Continue with current tools.")
        assert (await (second).get_result()).final_output == "continued"
    assert ("exec" in declared_names(responses.requests[2])) is enable_after_restore
    assert all(
        headers["authorization"] == "Bearer synthetic-current-code-mode-key"
        for headers in responses.request_headers[2:]
    )
    assert effects == ["before", "after"]
    if enable_after_restore:
        assert "reply-for-after" in code_output(responses.requests[3], "second")
    else:
        assert "reply-for-after" in tool_output(responses.requests[3], "second")
    await first_provider.aclose()
    await current_provider.aclose()


async def test_generated_names_rejected_and_existing_tool_can_become_manual(
    responses: ResponsesFixture,
) -> None:
    class Args(BaseModel):
        key: str

    manual = FunctionTool(
        name="lookup", description="Look up a key", input_type=Args, output_type=str
    )
    for name in ("exec", "wait"):

        @function_tool(name=name)
        async def collision() -> str:
            return "must not run"

        with pytest.raises(ValueError, match="reserves the tool names"):
            async with Agent(
                instructions="Read.", tools=[collision], programmatic_tool_calling=True
            ).session():
                pytest.fail("Opened a session with a reserved code-mode name")

    @function_tool
    async def lookup(key: str) -> str:
        return key

    async with Agent(
        instructions="Read.",
        tools=[lookup],
        provider=provider(responses),
        programmatic_tool_calling=True,
    ).session() as session:
        with pytest.raises(ValueError, match="captured"):
            await session.submit("select", tools=[manual])
    assert responses.requests == []


@pytest.mark.parametrize("already_compacted", [False, True], ids=["full", "model-compacted"])
async def test_disabled_code_mode_retains_inert_values_until_enabled_restore(
    responses, tmp_path, monkeypatch, already_compacted
):
    from dataclasses import replace

    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-original-value-key", supports_websockets=False
    )
    agent = Agent(
        instructions="Store and read the retained value.",
        provider=provider,
        programmatic_tool_calling=True,
        model="gpt-5.5",
    )
    responses.respond(code_call('store("retained", 29); text("stored");', "seed"))
    responses.respond(assistant_output("saved"))
    async with timeout(45), agent.session() as source:
        await (await source.submit("Store a value")).get_result()
        if already_compacted:
            responses.respond(assistant_output("Native summary of the settled code."))
            await source.compact()
        saved = await source.save_state()
    current = NativeProvider(
        base_url=responses.url, api_key="synthetic-current-value-key", supports_websockets=False
    )
    disabled = replace(agent, provider=current, programmatic_tool_calling=False)
    before = len(responses.requests)
    import importlib
    import json

    native = importlib.import_module("agents._native")
    create = native.create_checkpoint_engine

    async def without_helper(config_json, runtime_json, *args, **kwargs):
        runtime = json.loads(runtime_json)
        runtime["code_mode_host_exe"] = str(tmp_path / "nonexistent-code-mode-helper")
        return await create(config_json, json.dumps(runtime), *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(native, "create_checkpoint_engine", without_helper)
        async with timeout(45), disabled.session(state=saved) as dormant:
            before_compaction = await dormant.save_state()
            owner = dormant._native
            before_context = json.loads(await owner._read_model_context())
            with pytest.raises(RuntimeError, match="Supplied compaction requires verified unused"):
                await dormant.compact(summary="Do not replace the context of stored code.")
            assert dormant._native is owner
            assert json.loads(await owner._read_model_context()) == before_context
            retained = await dormant.save_state()
            assert retained.history[: len(before_compaction.history)] == before_compaction.history
            settings = before_compaction.history[-1]
            assert settings["type"] == "event_msg"
            assert settings["payload"]["type"] == "thread_settings_applied"
            # The attempted compaction export and this save each checkpoint settings.
            assert retained.history[len(before_compaction.history) :] == [settings, settings]
            assert retained.root_code_mode == saved.root_code_mode
    assert len(responses.requests) == before
    enabled = replace(disabled, programmatic_tool_calling=True)
    responses.respond(code_call('text(load("retained"));', "read"))
    responses.respond(assistant_output("restored"))
    async with timeout(45), enabled.session(state=retained) as destination:
        assert (
            await (await destination.submit("Read retained value")).get_result()
        ).final_output == "restored"
    assert "29" in code_output(responses.requests[-1], "read")
    assert all(
        headers["authorization"] == "Bearer synthetic-current-value-key"
        for headers in responses.request_headers[before:]
    )
    await provider.aclose()
    await current.aclose()


@pytest.mark.parametrize("stored", [False, True], ids=["unused", "stored-values"])
async def test_initial_cell_dormant_snapshot_compaction_depends_on_retained_values(
    responses, tmp_path, monkeypatch, stored
):
    import importlib
    from dataclasses import replace

    agent = Agent(
        instructions="Use code only when requested.",
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-unused-dormant", supports_websockets=False
        ),
        programmatic_tool_calling=True,
        model="gpt-5.5",
    )
    async with timeout(45), agent.session() as source:
        saved = await source.save_state()
    assert saved.root_code_mode is not None
    assert saved.root_code_mode["next_cell_id"] == 1
    assert saved.root_code_mode["stored_values"] == saved.root_code_mode["completed_cells"] == {}
    if stored:
        # The native snapshot contract accepts stored JSON at the initial counter.
        saved = saved.model_copy(
            update={"root_code_mode": {**saved.root_code_mode, "stored_values": {"retained": 29}}}
        )
    native = importlib.import_module("agents._native")
    create = native.create_checkpoint_engine

    async def without_helper(config_json, runtime_json, *args, **kwargs):
        runtime = json.loads(runtime_json)
        runtime["code_mode_host_exe"] = str(tmp_path / "nonexistent-code-mode-helper")
        return await create(config_json, json.dumps(runtime), *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(native, "create_checkpoint_engine", without_helper)
        async with (
            timeout(45),
            replace(agent, programmatic_tool_calling=False).session(state=saved) as dormant,
        ):
            before_compaction = await dormant.save_state()
            owner = dormant._native
            before_context = json.loads(await owner._read_model_context())
            if stored:
                with pytest.raises(
                    RuntimeError, match="Supplied compaction requires verified unused"
                ):
                    await dormant.compact(summary="Do not replace retained values.")
                assert dormant._native is owner
                assert json.loads(await owner._read_model_context()) == before_context
            else:
                await dormant.compact(summary="No code has run yet.")
            retained = await dormant.save_state()
            if stored:
                assert (
                    retained.history[: len(before_compaction.history)] == before_compaction.history
                )
                settings = before_compaction.history[-1]
                assert settings["type"] == "event_msg"
                assert settings["payload"]["type"] == "thread_settings_applied"
                assert retained.history[len(before_compaction.history) :] == [settings, settings]
            assert retained.root_code_mode == saved.root_code_mode
    assert responses.requests == []
    code = 'text(load("retained"));' if stored else 'text("first code after dormant summary");'
    expected = "29" if stored else "first code after dormant summary"
    responses.respond(code_call(code, "first-code"))
    responses.respond(assistant_output("first code completed"))
    async with timeout(45), agent.session(state=retained) as enabled:
        assert (await (await enabled.submit("Run the first cell.")).get_result()).final_output == (
            "first code completed"
        )
    assert expected in code_output(responses.requests[-1], "first-code")
    await agent.provider.aclose()
