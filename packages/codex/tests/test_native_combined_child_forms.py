"""Nested forms keep their actual child owner across native transports."""

import asyncio
import json
import os
from contextlib import aclosing
from copy import deepcopy
from dataclasses import replace

import pytest

from agents import ChildInvocation, ElicitationRequest, HttpMCP, InteractionExpiredError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, tool_output
from tests.test_native_child_agents import agent_for, child_peer, handles, trust
from tests.test_native_child_tool_integration import Memory, family_agent, family_peer
from tests.test_native_executor_tools import declaration
from tests.test_native_extension_forms import SCHEMA, VALID
from tests.test_native_mcp_stdio import records
from tests.test_native_mcp_tools import mcp_call
from tests.test_native_owned_child_prompts import interactive
from tests.test_native_owned_child_stdio import (
    external,
    install_stdio_program,
    matching_turn,
    next_form,
    owned_stdio,
    process_gone,
)
from tests.test_native_scoped_policies import AskingPeer


@pytest.mark.parametrize("method", ["elicitation/create", "openai/form"])
async def test_nested_http_form_reaches_original_child_and_rejects_invalid_reply(
    tmp_path, monkeypatch, method
):
    vault = AskingPeer(form_method=method, schema=SCHEMA)
    vault.annotations = None
    memory = Memory()
    async with (
        timeout(40),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        original = peer.invoke

        def invoke(name, step):
            if step == 1:
                return (
                    mcp_call("search", "mcp__vault", "nested-child-call")
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

        app = interactive(family_agent(peer, executor, url, memory))
        app = replace(
            app,
            tools=[
                replace(
                    tool,
                    tools=["search"],
                    approval_mode="preapproved",
                    transform_result=check_form_result,
                )
                if isinstance(tool, HttpMCP)
                else tool
                for tool in app.tools
            ],
        )
        async with app.session(context=memory, environment=declaration(executor)) as session:
            assert (await (await session.submit("Start both")).get_result()).final_output == (
                "root complete"
            )
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            for release in peer.release.values():
                release.set()
            async with aclosing(session.interactions()) as cursor:
                question = await anext(cursor)
            assert isinstance(question, ElicitationRequest)
            children = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            assert question.subagent_id == alpha_id
            assert question.turn_id == children[alpha_id].turn_id
            assert question.submission_id == children[alpha_id].submission_id
            assert question.schema == SCHEMA
            with pytest.raises(ValueError, match="MCP form failed validation"):
                await question.respond({**VALID, "tags": ["x"]})
            assert question in await session.pending_interactions() and not vault.answers
            await question.respond(deepcopy(VALID))
            assert (await children[alpha_id].get_result()).final_output == "alpha complete"
            assert len(vault.answers) == len(memory.mcp) == 1
            assert memory.mcp[0].submission.turn_id == children[alpha_id].turn_id
            assert "accepted-mcp:" + alpha_id in tool_output(
                peer.child_requests["alpha"][-1], "nested-child-call"
            )
            assert vault.answers[0]["result"]["content"] == VALID
            with pytest.raises(InteractionExpiredError):
                await question.respond(VALID)
            for child in children.values():
                await child.get_result()


@pytest.mark.parametrize("method", ["elicitation/create", "openai/form"])
async def test_nested_owned_stdio_forms_keep_siblings_separate_and_join_retirement(
    tmp_path, monkeypatch, method
):
    schema_file = tmp_path / "nested-form.json"
    schema_file.write_text(json.dumps(SCHEMA))
    async with (
        timeout(50),
        child_peer(("default", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        install_stdio_program(peer)
        app = interactive(agent_for(peer, executor))
        app = replace(
            app,
            tools=[
                *app.tools,
                owned_stdio(tmp_path, "--form-schema", str(schema_file), "--form-method", method),
            ],
        )
        async with app.session(environment=declaration(executor)) as session:
            assert (await (await session.submit("Start both")).get_result()).final_output == (
                "root complete"
            )
            first = await next_form(session)
            async with aclosing(session.interactions()) as cursor:
                second = await anext(cursor)
                if second is first:
                    second = await anext(cursor)
            assert isinstance(second, ElicitationRequest)
            assert first.subagent_id != second.subagent_id
            assert first.schema == second.schema == SCHEMA
            owner, sibling = (
                await matching_turn(session, first),
                await matching_turn(session, second),
            )
            with pytest.raises(ValueError, match="MCP form failed validation"):
                await first.respond({**VALID, "profile": {"name": "sample", "enabled": "yes"}})
            assert first in await session.pending_interactions()
            assert second in await session.pending_interactions()
            assert not any(row["event"] == "answer" for row in records(tmp_path / "stdio"))
            await first.respond(deepcopy(VALID))
            await owner.get_result()
            assert second in await session.pending_interactions()
            with pytest.raises(InteractionExpiredError):
                await first.respond({**VALID, "count": 3})
            assert len(external(tmp_path)) == 1
            await second.respond({**deepcopy(VALID), "count": 3})
            await sibling.get_result()
            effects = external(tmp_path)
            assert len(effects) == 2 and {row["local_calls"] for row in effects} == {1}
            assert len({row["pid"] for row in effects}) == 2
            assert sorted(row["answer"]["count"] for row in effects) == [2, 3]
            answers = [row for row in records(tmp_path / "stdio") if row["event"] == "answer"]
            assert len(answers) == 2
            assert all(
                row["message"]["result"]["content"]["tags"] == VALID["tags"] for row in answers
            )
            if os.name != "nt":
                for row in effects:
                    process_gone(row["pid"])
