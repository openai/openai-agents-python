"""The public opening ceiling, selection and confirmed roster cover real peer effects."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from agents import (
    Agent,
    CapabilityError,
    HttpMCP,
    InteractionExpiredError,
    NativeAgents,
    NativeError,
    ToolContext,
    ToolOutput,
    ToolOutputImage,
    ToolOutputText,
    ToolRef,
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
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_mcp_tools import Peer, declarations, mcp_call
from tests.test_native_session_input import IMAGE


async def test_reserved_python_tools_bind_context_and_preserve_bare_identity(
    responses: ResponsesFixture,
) -> None:
    effects: list[str] = []

    @function_tool
    async def lookup(ctx: ToolContext[list[str]], key: str) -> str:
        ctx.context.append(key)
        return "accepted-" + key

    @function_tool
    def optional(ctx: ToolContext[list[str] | None], key: str) -> ToolOutput:
        assert ctx.context is effects
        return ToolOutput.text(key)

    available = [lookup, optional]
    agent = Agent(instructions="Use selected tools.", tools=available, provider=provider(responses))
    available.clear()
    assert agent.tools == (lookup, optional)
    assert lookup.ref == ToolRef("lookup")
    assert lookup.ref != ToolRef("lookup", "functions")
    async with timeout(15), agent.session(context=effects) as session:
        responses.respond(function_call("lookup", {"key": "disabled"}, "disabled"))
        responses.respond(assistant_output("no tool"))
        assert (
            await (await session.submit("disabled", tools=[])).get_result()
        ).final_output == "no tool"
        assert effects == []
        assert ("functions", "lookup") not in declarations(responses.requests[0])

        with pytest.raises(CapabilityError):
            await session.submit("invalid", tools=[ToolRef("lookup", "functions")])
        with pytest.raises(CapabilityError):
            await session.submit("invalid", tools=[ToolRef("unknown")])

        responses.respond(function_call("lookup", {"key": "once"}, "enabled"))
        responses.respond(assistant_output("accepted"))
        completed = await (await session.submit("active", tools=[lookup.ref])).get_result()
        assert completed.final_output == "accepted"
        assert effects == ["once"]
        assert tool_output(responses.requests[-1], "enabled") == "accepted-once"
        assert ("functions", "lookup") in declarations(responses.requests[-2])
        assert ("functions", "optional") not in declarations(responses.requests[-2])
        responses.respond(assistant_output("defaults"))
        await (await session.submit("new operation")).get_result()
        assert set(declarations(responses.requests[-1])) == {
            ("functions", "lookup"),
            ("functions", "optional"),
        }


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.parametrize("kind", ["text", "image", "error", "parts", "native"])
async def test_native_typed_output_parts_and_manual_grants(
    responses: ResponsesFixture, manual: bool, kind: str
) -> None:
    from agents import FunctionTool

    effects: list[str] = []

    def output() -> ToolOutput:
        if kind == "text":
            return ToolOutput.text("accepted")
        if kind == "image":
            return ToolOutput.image(IMAGE)
        if kind == "error":
            return ToolOutput.error("failure receipt")
        if kind == "native":
            items: list[dict[str, Any]] = [{"type": "inputText", "text": "before correction"}]
            result = ToolOutput(content_items=items) if manual else ToolOutput(items, False)
            # Both the existing caller-owned list and the public fields remain mutable.
            assert result.content_items is items
            result.success = False
            items[0]["text"] = "failure receipt"
            result.content_items.append({"type": "inputText", "text": "accepted"})
            return result
        return ToolOutput([ToolOutputText("accepted picture"), ToolOutputImage(IMAGE)])

    @function_tool
    async def picture(key: str) -> ToolOutput:
        effects.append(key)
        return output()

    manual_tool = FunctionTool(
        name="picture", description="Read a picture", input_type=LookupArgs, output_type=ToolOutput
    )
    selected = manual_tool if manual else picture
    responses.respond(function_call("picture", {"key": "B"}, "picture"))
    responses.respond(assistant_output("settled"))
    async with (
        timeout(15),
        Agent(
            instructions="Use picture.", tools=[selected], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("read")
        if manual:
            request = await next_request(session.interactions(), manual_tool)
            with pytest.raises(ValueError, match="failed validation"):
                await request.respond("wrong type")  # type: ignore[arg-type]
            if kind == "native":
                result = output()
                result.content_items = [{"type": "inputText", "text": 42}]
                with pytest.raises(ValueError, match="failed validation"):
                    await request.respond(result)
            effects.append("B")
            await request.respond(output())
            with pytest.raises(InteractionExpiredError, match="foreign|expired|already"):
                await request.respond(output())
        assert (await turn.get_result()).final_output == "settled"
        history = [item async for item in session.items()]
        assert effects == ["B"]
        assert len(responses.requests) == 2
        item = next(
            item
            for item in responses.requests[1]["input"]
            if item.get("call_id") == "picture" and item["type"] == "function_call_output"
        )
        result = item["output"]
        encoded = json.dumps(result)
        if kind in ("image", "parts"):
            assert IMAGE in encoded
            assert IMAGE in json.dumps(history)
        if kind in ("text", "parts", "native"):
            assert "accepted" in encoded
        if kind in ("error", "native"):
            assert "failure receipt" in encoded


@pytest.mark.parametrize("image", ["https://example.invalid/image.png", "file-id", "/tmp/pic.png"])
def test_tool_output_image_admits_only_inline_data_urls(image: str) -> None:
    with pytest.raises(ValueError, match="base64 image data URL"):
        ToolOutput.image(image)


async def test_public_dormant_http_receipts_effects_and_admitted_leaves(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents._runtime.session as session_module

    peer = Peer(names=("find-record", "read", "excluded"))
    receipts: list[dict[str, Any]] = []
    check = session_module._check_mcp_receipt

    def record(raw, servers, available, generation):
        receipts.append(json.loads(raw))
        return check(raw, servers, available, generation)

    monkeypatch.setattr(session_module, "_check_mcp_receipt", record)
    async with timeout(25), peer.serve() as url:
        server = HttpMCP(name="docs-api", url=url, tools=["find-record", "read"])
        assert server.tool("find-record") == ToolRef("find_record", "mcp__docs_api")
        with pytest.raises(ValueError, match="not declared"):
            server.tool("excluded")
        roster = [server]
        async with Agent(
            instructions="Read selected records.",
            tools=roster,
            provider=provider(responses),
            model="gpt-5.5",
        ).session() as session:
            disabled = receipts[-1]["result"]
            assert disabled["ready"] is True and disabled["policy_generation"] == 0
            assert disabled["servers"]["docs-api"]["status"] == "disabled"
            assert disabled["servers"]["docs-api"]["tools"] == []
            assert peer.requests == []
            responses.respond(mcp_call("find_record", "mcp__docs_api", "dormant"))
            responses.respond(assistant_output("inactive"))
            assert (
                await (await session.submit("inactive", tools=[])).get_result()
            ).final_output == "inactive"
            assert not peer.requests
            assert not any(ns.startswith("mcp__") for ns, _ in declarations(responses.requests[0]))

            responses.respond(mcp_call("find_record", "mcp__docs_api", "first"))
            responses.respond(assistant_output("first"))
            assert (
                await (
                    await session.submit("selected", tools=[server.tool("find-record")])
                ).get_result()
            ).final_output == "first"
            active = receipts[-1]["result"]
            assert active["ready"] is True and active["policy_generation"] == 1
            assert active["servers"]["docs-api"]["status"] == "connected"
            assert [c["name"] for c in peer.calls] == ["find-record"]
            names = declarations(responses.requests[-2])
            assert ("mcp__docs_api", "find_record") in names
            assert ("mcp__docs_api", "read") not in names
            assert ("mcp__docs_api", "excluded") not in names

            count = len(peer.requests)
            responses.respond(mcp_call("find_record", "mcp__docs_api", "off"))
            responses.respond(assistant_output("off"))
            assert (
                await (await session.submit("off", tools=[])).get_result()
            ).final_output == "off"
            assert len(peer.requests) == count
            assert receipts[-1]["result"]["servers"]["docs-api"]["tools"] == []
            assert len(peer.calls) == 1
            with pytest.raises(CapabilityError):
                await session.submit("invalid", tools=[ToolRef("excluded", "mcp__docs_api")])
            with pytest.raises(CapabilityError):
                await session.submit("invalid", tools=[replace(server, name="docs.api")])
            with pytest.raises(CapabilityError):
                await session.submit("invalid", tools=[replace(server, tools=["find_record"])])
            assert len(peer.requests) == count
            assert len(receipts) == 3
            responses.respond(mcp_call("read", "mcp__docs_api", "again"))
            responses.respond(assistant_output("again"))
            assert (
                await (await session.submit("again", tools=[server])).get_result()
            ).final_output == "again"
            assert [c["name"] for c in peer.calls] == ["find-record", "read"]
            names = declarations(responses.requests[-2])
            assert ("mcp__docs_api", "find_record") in names
            assert ("mcp__docs_api", "read") in names
            assert ("mcp__docs_api", "excluded") not in names


async def test_failed_dormant_activation_fences_old_authority(
    responses: ResponsesFixture,
) -> None:
    peer, other = Peer(denied=True), Peer()
    effects: list[str] = []

    @function_tool
    def local(key: str) -> str:
        effects.append(key)
        return key

    async with timeout(20), peer.serve() as url, other.serve() as other_url:
        server = HttpMCP(name="records", url=url, tools=["search"], startup_timeout=1)
        roster = [local, server]
        async with Agent(
            instructions="Read.", tools=roster, model="gpt-5.5", provider=provider(responses)
        ).session() as session:
            assert peer.requests == []
            with pytest.raises(CapabilityError, match="captured"):
                await session.submit("invalid", tools=[replace(server, url=other_url)])
            assert peer.requests == other.requests == []
            submitted = await session.submit("unready", tools=[server])
            with pytest.raises(NativeError, match="connection not ready"):
                await submitted.get_result()
            assert peer.requests
            with pytest.raises(RuntimeError, match="cannot continue after an unconfirmed"):
                await session.submit("must not call old authority")
            assert effects == []
            assert other.requests == []


def test_native_agents_keyword_is_not_an_opening_path() -> None:
    with pytest.raises(TypeError, match="native_agents"):
        Agent(instructions="Read.", native_agents=NativeAgents())  # type: ignore[call-overload]
