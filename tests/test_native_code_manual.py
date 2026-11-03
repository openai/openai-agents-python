"""Live generated code waits on the exact owner's typed manual reply."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    FunctionTool,
    InteractionExpiredError,
    NativeAgents,
    NativeError,
    ToolRef,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import handles, metadata, trust
from tests.test_native_child_manual import ManualChildren, declaration_for
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_workflow_persistence import model_restore_agent


async def test_live_manual_code_can_replace_automatic_and_cannot_export_its_stack(
    responses: ResponsesFixture,
) -> None:
    automatic: list[str] = []

    @function_tool
    def lookup(key: str) -> str:
        automatic.append(key)
        return "automatic:" + key

    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    agent = Agent(
        instructions="Read both keys.",
        tools=[manual],
        provider=provider(responses),
        programmatic_tool_calling=True,
    )
    async with timeout(30), agent.session() as session:
        responses.respond(
            code_call(
                "const a=tools.lookup({key:'a'}),b=tools.lookup({key:'b'});"
                "text('RESULT:'+JSON.stringify(await Promise.all([a,b])));",
                "same-outer",
            )
        )
        responses.respond(assistant_output("both accepted"))
        original = await session.submit("Read both keys.")
        stream = session.interactions()
        by_key = {
            request.arguments.key: request
            for request in (await next_request(stream, manual), await next_request(stream, manual))
        }
        assert set(by_key) == {"a", "b"}
        assert all(
            request.submission_id == original.submission_id and request.turn_id == original.turn_id
            for request in by_key.values()
        )
        assert len({r.call_id for r in by_key.values()}) == 2
        assert "same-outer" not in {r.call_id for r in by_key.values()}
        assert all(
            r.call.route == "code" and r.call.call_id is r.call.outer_call_id is None
            for r in by_key.values()
        )
        assert all(r.call.cell_id is not None and r.call.nested_call_id for r in by_key.values())
        assert len({r.call.nested_call_id for r in by_key.values()}) == 2
        assert all(r.call.model == responses.requests[0]["model"] for r in by_key.values())
        with pytest.raises(RuntimeError, match="idle"):
            await session.save_state()
        with pytest.raises(
            RuntimeError,
            match="Code-mode manual checkpoints require unused code state|active cells",
        ):
            await session.suspend()
        with pytest.raises(ValueError, match="validation"):
            await by_key["a"].respond(7)
        competing = await asyncio.gather(
            by_key["a"].respond("accepted-a"),
            by_key["a"].respond("duplicate"),
            return_exceptions=True,
        )
        assert sum(result is None for result in competing) == 1
        assert sum(isinstance(result, InteractionExpiredError) for result in competing) == 1
        assert await by_key["b"].respond("accepted-b") is None
        assert (await (original).get_result()).final_output == "both accepted"
        rendered = code_output(responses.requests[1], "same-outer")
        assert "accepted-a" in rendered and "accepted-b" in rendered
        assert "duplicate" not in rendered and automatic == []
        assert (await session.save_state()).format_version == 6

        responses.respond(code_call("text(await tools.lookup({key:'fresh'}));", "same-outer"))
        responses.respond(assistant_output("automatic again"))
        fresh = await session.submit("Read the fresh value")
        current = await next_request(stream, manual)
        await current.respond("automatic:fresh")
        with pytest.raises(InteractionExpiredError, match="expired|foreign|already"):
            await by_key["b"].respond("stale")
        assert (await (fresh).get_result()).final_output == "automatic again"
        outputs = [
            item["output"]
            for item in responses.requests[-1]["input"]
            if item.get("type") == "custom_tool_call_output" and item.get("call_id") == "same-outer"
        ]
        assert len(outputs) == 2
        assert "automatic:fresh" in json.dumps(outputs[-1])
        assert automatic == []
        await stream.aclose()


class CodeManualChildren(ManualChildren):
    def response(self):
        events = super().response()
        path = metadata(self.requests[-1], self.request_headers[-1])["agent_name"]
        if path != "/root" and len(self.child_requests[path.rsplit("/", 1)[-1]]) == 1:
            name = path.rsplit("/", 1)[-1]
            return [
                code_call(
                    "text('OWNER:' + JSON.stringify(await tools.lookup("
                    + json.dumps({"key": name})
                    + ")));",
                    "same-outer",
                )
            ]
        return events


@pytest.mark.parametrize("cancel_alpha", [False, True], ids=["reply", "cancel"])
async def test_code_manual_children_keep_owners_after_parent_settles(
    tmp_path, monkeypatch, cancel_alpha: bool
) -> None:
    peer = CodeManualChildren()
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with (
        timeout(35),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, lookup)
        # Use the pinned model that enables V2 collaboration in code mode.
        agent = replace(agent, model=model_restore_agent(peer, executor).model)
        agent = replace(agent, programmatic_tool_calling=True)
        environment = declaration(executor)
        async with agent.session(environment=environment) as session:
            root = await session.submit("Start both children")
            assert (await (root).get_result()).final_output == "root complete"
            stream = session.interactions()
            by_name = {
                req.arguments.key: req
                for req in (await next_request(stream, lookup), await next_request(stream, lookup))
            }
            assert set(by_name) == {"alpha", "beta"}
            assert len({r.subagent_id for r in by_name.values()}) == 2
            by_id = {handle.child_id: handle for handle in await handles(session, 2)}
            alpha = by_id[by_name["alpha"].subagent_id]
            beta = by_id[by_name["beta"].subagent_id]
            with pytest.raises(RuntimeError, match="child"):
                await session.save_state()
            # Replacing manual declarations cannot bypass native child closure.
            with pytest.raises(RuntimeError, match="child"):
                publication = await session.submit(
                    "publication",
                    tools=[
                        environment,
                        next(t for t in agent.tools if isinstance(t, NativeAgents)),
                        ToolRef("exec", "functions"),
                        ToolRef("wait", "functions"),
                    ],
                )
                await publication.get_result()
            if cancel_alpha:
                # This exact-turn native guard is conditional, not a public
                # force or graph-wide interruption implementation.
                assert await session._native._cancel_child_turn(alpha._native) is True
                with pytest.raises((NativeError, asyncio.CancelledError)):
                    await (alpha).get_result()
                with pytest.raises(InteractionExpiredError, match="expired|foreign|already"):
                    await by_name["alpha"].respond("stale-alpha")
            else:
                await by_name["alpha"].respond("only-alpha")
                assert (await (alpha).get_result()).final_output == "alpha complete"
                assert "only-alpha" in code_output(peer.child_requests["alpha"][1], "same-outer")
            await by_name["beta"].respond("only-beta")
            assert (await (beta).get_result()).final_output == "beta complete"
            rendered = code_output(peer.child_requests["beta"][1], "same-outer")
            assert "only-beta" in rendered and "only-alpha" not in rendered
            with pytest.raises(InteractionExpiredError, match="expired|foreign|already"):
                await by_name["beta"].respond("duplicate")
            assert not any(item.get("method") == "process/start" for item in executor.requests)
            await stream.aclose()
