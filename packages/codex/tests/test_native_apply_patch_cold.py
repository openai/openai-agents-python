"""Batch editor execution without a previously imported tools facade."""

import asyncio
import json
import sys

from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


async def test_cold_batch_executes_in_order_and_restore_does_not_replay(responses):
    operations = [
        {"type": "create_file", "path": "synthetic.txt", "diff": "+initial"},
        {"type": "update_file", "path": "synthetic.txt", "diff": "-missing\n+updated"},
        {"type": "delete_file", "path": "synthetic.txt"},
    ]
    responses.respond(function_call("apply_patch_batch", {"operations": operations}, "cold-batch"))
    responses.respond(assistant_output("done"))
    responses.respond(assistant_output("continued"))
    program = """
import asyncio
import sys

import agents

assert "_agents_codex" not in sys.modules
assert "agents.tools" not in sys.modules
from agents import (
    Agent, ApplyPatchResult, FunctionTool, NativeProvider, SessionState, apply_patch_batch_tool,
)
assert "agents.tools" not in sys.modules

effects = []

class Editor:
    def create_file(self, operation):
        effects.append((operation.type, operation.path))
        return ApplyPatchResult("completed", "created")

    async def update_file(self, operation):
        effects.append((operation.type, operation.path))
        return ApplyPatchResult("failed", "refused")

    def delete_file(self, operation):
        effects.append((operation.type, operation.path))
        return ApplyPatchResult("completed", "deleted")

async def main():
    tool = apply_patch_batch_tool(Editor())
    assert type(tool) is FunctionTool
    assert "agents.tools" not in sys.modules
    expected = [
        ("create_file", "synthetic.txt"),
        ("update_file", "synthetic.txt"),
        ("delete_file", "synthetic.txt"),
    ]
    async with NativeProvider(base_url=sys.argv[1], api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use the ordered editor.", model="gpt-5", provider=provider,
            tools=[tool],
        )
        async with agent.session() as session:
            result = await (await session.submit("edit")).get_result()
            assert result.final_output == "done"
            assert effects == expected
            saved = SessionState.from_json((await session.save_state()).to_json())
        async with agent.session(state=saved) as restored:
            result = await (await restored.submit("continue")).get_result()
            assert result.final_output == "continued"
    assert effects == expected

asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        program,
        responses.url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    assert len(responses.requests) == 3
    expected_output = {
        "results": [
            {"status": "completed", "output": "created"},
            {"status": "failed", "output": "refused"},
            {"status": "completed", "output": "deleted"},
        ]
    }
    for request in responses.requests[1:]:
        assert json.loads(tool_output(request, "cold-batch")) == expected_output
