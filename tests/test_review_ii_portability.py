"""Suspended manual state resumes across isolated Python processes."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

from agents import (
    Agent,
    FunctionTool,
    InteractionExpiredError,
    InteractionRequired,
    NativeProvider,
    function_tool,
)
from agents.observation import ToolFinished
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request

# This worker uses no test imports or source path injection. Isolated Python
# loads the installed SDK outside the checkout, including editable development
# installs. A wheel installation must resolve to site-packages.
MANUAL_WORKER = textwrap.dedent(
    """
    import asyncio
    import json
    import sys
    from importlib.metadata import distribution
    from pathlib import Path

    import agents
    from pydantic import BaseModel
    from agents import Agent, FunctionTool, NativeProvider, function_tool
    from agents.state import SessionState

    install = json.loads(distribution("openai-agents").read_text("direct_url.json") or "{}")
    if not install.get("dir_info", {}).get("editable", False):
        assert "site-packages" in Path(agents.__file__).parts, agents.__file__
    data = json.load(sys.stdin)

    class LookupArgs(BaseModel):
        key: str

    @function_tool(description="Record one already completed operation.")
    async def record_once(key: str) -> str:
        raise AssertionError("The restored Session replayed the completed operation: " + key)

    async def main():
        lookup = FunctionTool(
            name="lookup", description="Look up one label", input_type=LookupArgs, output_type=str
        )
        provider = NativeProvider(
            base_url=data["url"], api_key="current-process-provider-credential"
        )
        agent = Agent(
            instructions="Record once, then look up the label.",
            tools=[record_once, lookup], provider=provider,
        )
        saved = SessionState.from_json(data["checkpoint"])
        async with agent.session(state=saved) as session:
            stream = session.interactions()
            async for event in stream:
                request = lookup.match(event)
                if request is not None:
                    break
            else:
                raise AssertionError("Restored Session did not issue a fresh manual request")
            assert request.turn_id == data["turn_id"]
            assert request.call_id == data["call_id"]
            assert request.arguments.key == "label-a"
            await request.respond("current-process-label")
            result = await session.restored_turn.get_result()
            turn = result.turn
            assert turn.turn_id == data["turn_id"]
            assert result.turn is turn
            outputs = [
                item async for item in session.items()
                if item.get("type") == "function_call_output"
            ]
            await stream.aclose()
            print(json.dumps({
                "turn_id": turn.turn_id,
                "final_output": result.final_output,
                "call_ids": [item["call_id"] for item in outputs],
                "agents_file": agents.__file__,
            }))
        await provider.aclose()

    asyncio.run(main())
    """
)


async def test_suspended_manual_json_resumes_in_isolated_process_without_replaying_tool(
    responses: ResponsesFixture,
) -> None:
    completed = []

    @function_tool(description="Record one already completed operation.")
    async def record_once(key: str) -> str:
        completed.append(key)
        return "original-operation-receipt:" + key

    lookup = FunctionTool(
        name="lookup", description="Look up one label", input_type=LookupArgs, output_type=str
    )
    provider = NativeProvider(base_url=responses.url, api_key="old-process-provider-credential")
    agent = Agent(
        instructions="Record once, then look up the label.",
        tools=[record_once, lookup],
        provider=provider,
    )
    responses.respond(
        function_call("record_once", {"key": "effect-a"}, "done-once"),
        function_call("lookup", {"key": "label-a"}, "pending-manual"),
    )
    async with timeout(50):
        async with agent.session() as source:
            turn = await source.submit("Record and read")
            original = await next_request(source.interactions(), lookup)
            # A separate cursor also sees completion when it preceded the manual
            # request. Suspension must wait until the automatic call has settled.
            events = source.events()
            async for event in events:
                if isinstance(event, ToolFinished) and event.call_id == "done-once":
                    break
            await events.aclose()
            while True:
                try:
                    state = await source.suspend()
                    break
                except RuntimeError as error:
                    if str(error) != "The active submission is not a settled manual boundary":
                        raise
                    await asyncio.sleep(0)
            encoded = state.to_json()
            assert completed == ["effect-a"]
            assert len(responses.requests) == 1
            assert "old-process-provider-credential" not in encoded
            with pytest.raises(InteractionRequired) as held:
                await turn.get_result()
            assert held.value.state is not None
            with pytest.raises(InteractionExpiredError, match="foreign|expired"):
                await original.respond("obsolete")
            with pytest.raises(RuntimeError):
                await source.submit("try to start retired source")
        await provider.aclose()

        responses.respond(assistant_output("continued from current process"))
        payload = {
            "checkpoint": encoded,
            "url": responses.url,
            "turn_id": turn.turn_id,
            "call_id": original.call_id,
        }
        with tempfile.TemporaryDirectory(prefix="codex-manual-resume-") as elsewhere:
            assert not Path(elsewhere).resolve().is_relative_to(Path(__file__).resolve().parents[1])
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                "-c",
                MANUAL_WORKER,
                cwd=elsewhere,
                env=os.environ.copy(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await process.communicate(json.dumps(payload).encode())
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            assert process.returncode == 0, (stdout.decode(), stderr.decode())
        receipt = json.loads(stdout)
        assert receipt["turn_id"] == turn.turn_id
        assert receipt["final_output"] == "continued from current process"
        assert sorted(receipt["call_ids"]) == ["done-once", "pending-manual"]
        assert completed == ["effect-a"]
        assert len(responses.requests) == 2
        assert (
            tool_output(responses.requests[-1], "done-once")
            == "original-operation-receipt:effect-a"
        )
        assert tool_output(responses.requests[-1], "pending-manual") == "current-process-label"
        assert (
            responses.request_headers[0]["authorization"]
            == "Bearer old-process-provider-credential"
        )
        assert (
            responses.request_headers[-1]["authorization"]
            == "Bearer current-process-provider-credential"
        )
