from __future__ import annotations

import pytest

from agents import (
    Agent,
    HostedMCP,
    NativeError,
    NativeProvider,
    SessionState,
    WebSearch,
    function_tool,
)
from tests.native_fixtures import assistant_output, responses as responses


def _servers():
    return [
        HostedMCP(
            tool_config={
                "type": "mcp",
                "server_label": label,
                "server_url": f"https://{label}.invalid/mcp",
                "require_approval": "never",
                "allowed_tools": {"tool_names": [name], "read_only": True},
            }
        )
        for label, name in (("alpha", "lookup"), ("beta", "read_beta"))
    ]


@pytest.mark.parametrize("kind", ["web_search", "web_search_preview"])
async def test_native_preview_selector_preserves_ga_catalog_and_exact_wire(responses, kind):
    responses.respond(assistant_output("done"))
    effects = []

    @function_tool
    def web_search_preview() -> str:
        effects.append("called")
        return "unselected"

    async with NativeProvider(base_url=responses.url, api_key="synthetic-selector") as provider:
        agent = Agent(
            model="gpt-5.5",
            instructions="Inspect the offered search.",
            provider=provider,
            tools=[WebSearch(route="hosted"), web_search_preview],
            model_settings={"tool_choice": {"type": kind}},
        )
        result = await agent.run("Search.")
        assert result.final_output == "done"
    assert responses.requests[0]["tool_choice"] == {"type": kind}
    assert any(tool["type"] == "web_search" for tool in responses.requests[0]["tools"])
    assert effects == []


async def test_native_bare_mcp_keeps_two_servers_and_scoped_update_restore(responses):
    for _ in range(4):
        responses.respond(assistant_output("done"))

    @function_tool
    def ordinary() -> str:
        return "unselected"

    scoped = {"type": "mcp", "server_label": "alpha", "name": "lookup"}
    async with NativeProvider(base_url=responses.url, api_key="synthetic-selector") as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect both offered servers.",
            provider=provider,
            tools=[*_servers(), ordinary],
            model_settings={"tool_choice": {"type": "mcp"}},
        )
        async with agent.session() as session:
            await (await session.submit("Choose any offered MCP server.")).get_result()
            await session.update(model_settings={"tool_choice": scoped})
            await (await session.submit("Choose alpha.lookup.")).get_result()
            saved = SessionState.from_json((await session.save_state()).to_json())
            await session.update(model_settings={"tool_choice": {"type": "mcp"}})
            await (await session.submit("Return to the offered category.")).get_result()
        async with agent.session(state=saved) as restored:
            await (await restored.submit("Retain the captured exact selection.")).get_result()
    assert [r["tool_choice"] for r in responses.requests] == [
        {"type": "mcp"},
        scoped,
        {"type": "mcp"},
        scoped,
    ]
    for request in responses.requests:
        assert [tool["server_label"] for tool in request["tools"] if tool["type"] == "mcp"] == [
            "alpha",
            "beta",
        ]
        assert any(
            tool["type"] == "function" and tool["name"] == "ordinary" for tool in request["tools"]
        )
        assert all(
            tool["allowed_tools"]["read_only"] for tool in request["tools"] if tool["type"] == "mcp"
        )


@pytest.mark.parametrize(
    "selector,tools",
    [
        ({"type": "mcp"}, []),
        ({"type": "mcp", "server_label": "foreign"}, _servers()),
        ({"type": "mcp", "server_label": "alpha", "name": "read_beta"}, _servers()),
        ({"type": "web_search_preview"}, []),
    ],
)
async def test_native_hosted_selector_requires_the_final_offered_catalog(
    responses, selector, tools
):
    async with NativeProvider(base_url=responses.url, api_key="synthetic-selector") as provider:
        agent = Agent(
            model="fixture-hosted-choice",
            instructions="Inspect.",
            provider=provider,
            tools=tools,
            model_settings={"tool_choice": selector},
        )
        with pytest.raises(NativeError, match="(?i)(hosted.*catalog|hosted.*choice)"):
            await agent.run("Do not acquire a foreign tool.")
    assert responses.requests == []


@pytest.mark.parametrize(
    "selector",
    [
        {"type": "mcp", "name": "lookup"},
        {"type": "mcp", "server_label": None},
        {"type": "mcp", "server_label": False},
        {"type": "mcp", "server_label": " "},
        {"type": "mcp", "server_label": "alpha", "name": None},
        {"type": "mcp", "server_label": "alpha", "name": " "},
        {"type": "mcp", "server_label": "alpha", "extra": "unadmitted"},
    ],
)
def test_invalid_selector_stays_outside_resources(selector):
    with pytest.raises(ValueError, match="hosted"):
        Agent(
            model="fixture-hosted-choice",
            instructions="Inspect.",
            model_settings={"tool_choice": selector},
        )
