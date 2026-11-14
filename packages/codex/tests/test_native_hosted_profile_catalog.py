"""The native handoff offers each destination's own provider tool settings."""

from __future__ import annotations

import json

import pytest

from agents import Agent, CodeInterpreter, FileSearch, NativeProvider, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file_search", "code_interpreter"])
@pytest.mark.parametrize("distinct", [False, True], ids=["target-only", "different-settings"])
async def test_handoff_hosted_tools_keep_their_exact_profile_owner(responses, kind, distinct):
    def make(owner):
        if kind == "file_search":
            return FileSearch(
                vector_store_ids=[f"vs_{owner}"],
                max_num_results=3,
                include_search_results=owner == "destination",
            )
        return CodeInterpreter(
            container={"type": "auto", "file_ids": [f"file-{owner}"]},
            include_outputs=owner == "destination",
        )

    source_tool = make("source")
    target_tool = make("destination")
    root_tools = [source_tool] if distinct else []
    async with (
        timeout(25),
        NativeProvider(base_url=responses.url, api_key="synthetic-hosted-catalog") as provider,
    ):
        root = Agent(
            name="source",
            model="gpt-5.5",
            instructions="Source instructions.",
            tools=root_tools,
            provider=provider,
            handoffs=[lambda: target],
        )
        target = Agent(
            name="destination",
            model="gpt-5.5",
            instructions="Destination instructions.",
            tools=[target_tool],
            handoffs=[root],
        )
        responses.respond(function_call("transfer_to_destination", {}, "handoff-target"))
        responses.respond(assistant_output("at destination"))
        responses.respond(function_call("transfer_to_source", {}, "handoff-root"))
        responses.respond(assistant_output("at root"))
        responses.respond(assistant_output("without tools"))
        async with root.session() as session:
            assert (
                await (await session.submit("Transfer.")).get_result()
            ).final_output == "at destination"
            assert await session.get_active_agent() is target
            saved = (await session.save_state()).to_json()
        async with root.session(state=SessionState.from_json(saved)) as session:
            assert await session.get_active_agent() is target
            assert (await (await session.submit("Return.")).get_result()).final_output == "at root"
            assert await session.get_active_agent() is root

        async with root.session(state=SessionState.from_json(saved)) as session:
            assert await session.get_active_agent() is target
            result = await (await session.submit("No tools.", tools=[])).get_result()
            assert result.final_output == "without tools"
        assert not [tool for tool in responses.requests[-1]["tools"] if tool["type"] == kind]

    assert len(responses.requests) == 5
    for request, owner in zip(
        responses.requests[:4], ["source", "destination", "destination", "source"], strict=True
    ):
        tools = [tool for tool in request["tools"] if tool["type"] == kind]
        if owner == "source" and not distinct:
            assert not tools
        else:
            [tool] = tools
            if kind == "file_search":
                assert tool["vector_store_ids"] == [f"vs_{owner}"]
            else:
                assert tool["container"] == {"type": "auto", "file_ids": [f"file-{owner}"]}
        include = (
            "file_search_call.results" if kind == "file_search" else "code_interpreter_call.outputs"
        )
        assert (include in request.get("include", [])) is (owner == "destination")
        assert owner.capitalize() + " instructions." in json.dumps(request["input"])


def _credential_opening(provider, secret, include_outputs):
    destination = Agent(
        name="destination",
        model="gpt-5.5",
        instructions="Destination instructions.",
        tools=[
            CodeInterpreter(
                container={
                    "type": "auto",
                    "network_policy": {
                        "type": "allowlist",
                        "allowed_domains": ["example.invalid"],
                        "domain_secrets": [
                            {"domain": "example.invalid", "name": "key", "value": secret}
                        ],
                    },
                },
                include_outputs=include_outputs,
            ),
        ],
    )
    return Agent(
        name="source",
        model="gpt-5.5",
        instructions="Source instructions.",
        provider=provider,
        handoffs=[destination],
    ), destination


@pytest.mark.asyncio
async def test_hosted_checkpoint_rotates_secret_without_regranting_projection(responses):
    before = "first-hosted-credential-synthetic"
    after = "fresh-hosted-credential-synthetic"
    async with (
        timeout(25),
        NativeProvider(base_url=responses.url, api_key="synthetic-hosted") as provider,
    ):
        root, destination = _credential_opening(provider, before, True)
        responses.respond(function_call("transfer_to_destination", {}, "handoff"))
        responses.respond(assistant_output("before rotation"))
        async with root.session() as session:
            assert (await (await session.submit("Transfer.")).get_result()).final_output == (
                "before rotation"
            )
            assert await session.get_active_agent() is destination
            serialized = (await session.save_state()).to_json()
        assert before not in serialized

        changed, _ = _credential_opening(provider, after, False)
        with pytest.raises((RuntimeError, ValueError), match=r"(?i)(agent graph|declaration)"):
            async with changed.session(state=SessionState.from_json(serialized)):
                pytest.fail("Changed captured include projection was admitted")
        assert len(responses.requests) == 2

        fresh, fresh_destination = _credential_opening(provider, after, True)
        responses.respond(assistant_output("after rotation"))
        async with fresh.session(state=SessionState.from_json(serialized)) as session:
            assert await session.get_active_agent() is fresh_destination
            assert (await (await session.submit("Continue.")).get_result()).final_output == (
                "after rotation"
            )
            assert after not in (await session.save_state()).to_json()

    assert len(responses.requests) == 3
    assert not [t for t in responses.requests[0]["tools"] if t["type"] == "code_interpreter"]
    for request, secret in zip(responses.requests[1:], (before, after), strict=True):
        [code] = [t for t in request["tools"] if t["type"] == "code_interpreter"]
        assert code["container"]["network_policy"]["domain_secrets"][0]["value"] == secret
        assert "code_interpreter_call.outputs" in request["include"]


@pytest.mark.asyncio
async def test_handoff_cold_keeps_same_name_local_tool_in_its_profile(responses):
    effects = []

    @function_tool
    def file_search() -> str:
        """Look up the source Agent's local data."""
        effects.append("source-owned")
        return "source local result"

    async with (
        timeout(25),
        NativeProvider(base_url=responses.url, api_key="synthetic-hosted") as provider,
    ):
        source = Agent(
            name="source",
            model="gpt-5.5",
            instructions="Source instructions.",
            provider=provider,
            tools=[file_search],
            handoffs=[lambda: target],
        )
        target = Agent(
            name="destination",
            model="gpt-5.5",
            instructions="Destination instructions.",
            tools=[FileSearch(vector_store_ids=["vs_destination"])],
            handoffs=[source],
        )
        responses.respond(function_call("file_search", {}, "source-before"))
        responses.respond(function_call("transfer_to_destination", {}, "handoff-target"))
        responses.respond(assistant_output("at destination"))
        responses.respond(function_call("transfer_to_source", {}, "handoff-source"))
        responses.respond(function_call("file_search", {}, "source-after"))
        responses.respond(assistant_output("at source"))
        async with source.session() as session:
            assert (
                await (await session.submit("Find and transfer.")).get_result()
            ).final_output == ("at destination")
            assert effects == ["source-owned"]
            assert await session.get_active_agent() is target
            state = SessionState.from_json((await session.save_state()).to_json())
        async with source.session(state=state) as session:
            assert await session.get_active_agent() is target
            assert (await (await session.submit("Return and find.")).get_result()).final_output == (
                "at source"
            )
    assert effects == ["source-owned", "source-owned"]
    assert len(responses.requests) == 6
    for i, request in enumerate(responses.requests):
        matching = [
            tool["type"]
            for tool in request["tools"]
            if tool["type"] == "file_search" or tool.get("name") == "file_search"
        ]
        assert matching == (["file_search"] if i in (2, 3) else ["function"])
