from __future__ import annotations

import json

import pytest

from agents import Agent, NativeProvider, PendingRequests, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import MODEL, peer


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("capture", [False, True])
async def test_configured_extractor_can_use_accepted_owned_items(restore: bool, capture: bool):
    async with timeout(20), peer() as root, peer() as child:
        root.respond(function_call("delegate", {"input": "Check."}, "outer-call"))
        root.respond(assistant_output("parent done"))
        child.respond(function_call("lookup", {}, "inner-call"))
        child.respond(assistant_output("child done"))
        effects = []
        extracted = []

        @function_tool(needs_approval=True)
        def lookup() -> str:
            effects.append("lookup")
            return "synthetic value"

        def extract(result):
            extracted.append(result)
            return "child item summary"

        async with (
            NativeProvider(base_url=root.url, api_key="synthetic-root") as root_provider,
            NativeProvider(base_url=child.url, api_key="synthetic-child") as child_provider,
        ):
            specialist = Agent(
                name="specialist",
                instructions="Use lookup.",
                model=MODEL,
                provider=child_provider,
                tools=[lookup],
            )
            parent = Agent(
                name="parent",
                instructions="Use delegate.",
                model=MODEL,
                provider=root_provider,
                tools=[
                    specialist.as_tool(
                        "delegate",
                        custom_output_extractor=extract,
                        return_items=capture,
                    )
                ],
            )
            async with parent.session() as session:
                turn = await session.submit("Start.")
                pending = await turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                if restore:
                    state = SessionState.from_json((await session.suspend()).to_json())
                else:
                    await session.respond(pending.requests[0].reply(True))
                    assert (await turn.get_result()).final_output == "parent done"
            if restore:
                async with parent.session(state=state) as session:
                    turn = session.restored_turn
                    pending = await turn.wait_for_request_or_result()
                    assert isinstance(pending, PendingRequests)
                    await session.respond(pending.requests[0].reply(True))
                    assert (await turn.get_result()).final_output == "parent done"
            [result] = extracted
            assert result.final_output == "child done"
            if capture:
                assert result.items is not None
                expected = ["function_call_output", "message"]
                if not restore:
                    expected.insert(0, "function_call")
                assert [item.item["type"] for item in result.items] == expected
                assert all(item.agent is specialist for item in result.items)
                assert any(item.item.get("call_id") == "inner-call" for item in result.items)
            else:
                assert result.items is None
        assert effects == ["lookup"]
        assert len(root.requests) == len(child.requests) == 2
        wire = json.dumps(root.requests[1]["input"])
        assert "child item summary" in wire and "inner-call" not in wire
