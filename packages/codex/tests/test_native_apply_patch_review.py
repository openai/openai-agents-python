import asyncio
import json
from dataclasses import replace

from agents import Agent, FunctionTool, NativeProvider, SessionState, apply_patch_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_apply_patch import Editor, patch_call
from tests.test_native_manual_tools import LookupArgs, next_request


async def test_typed_patch_after_data_retains_accepted_private_record(responses):
    editor = Editor()

    def after(ctx, output):
        output.application_data = {"secret": "private patch record"}
        return output

    decl = replace(apply_patch_tool(editor), after=after)
    responses.respond(patch_call({"type": "delete_file", "path": "owned"}))
    responses.respond(assistant_output("first"))
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(instructions="Use editor.", model="gpt-5", provider=provider, tools=[decl])
        async with agent.session() as session:
            result = await (await session.submit("edit")).get_result()
            assert len(result.application_tool_outputs) == 1
            assert result.application_tool_outputs[0].application_data == {
                "secret": "private patch record"
            }
            saved = SessionState.from_json((await session.save_state()).to_json())
            assert saved.application_tool_outputs[0].application_data == {
                "secret": "private patch record"
            }
            assert "private patch record" not in json.dumps(saved.history)
        responses.respond(assistant_output("continued"))
        async with agent.session(state=saved) as resumed:
            result = await (await resumed.submit("continue")).get_result()
            assert result.final_output == "continued"
    assert len(editor.operations) == 1
    assert "private patch record" not in json.dumps(responses.requests)


async def test_completed_patch_allows_manual_checkpoint_without_replay(responses):
    editor = Editor()
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    responses.respond(
        patch_call({"type": "delete_file", "path": "owned"}),
        function_call("lookup", {"key": "A"}, "manual-A"),
    )
    async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
        agent = Agent(
            instructions="Use editor and lookup.",
            model="gpt-5",
            provider=provider,
            tools=[apply_patch_tool(editor), manual],
        )
        async with agent.session() as session:
            await session.submit("edit then lookup")
            request = await next_request(session.interactions(), manual)

            async def settled():
                while True:
                    items = [item async for item in session.items()]
                    if any(
                        item.get("type") == "function_call_output"
                        and item.get("call_id") == "native-patch"
                        for item in items
                    ):
                        return
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(settled(), 5)
            saved = SessionState.from_json((await session.suspend()).to_json())

        assert len(editor.operations) == 1
        async with agent.session(state=saved) as resumed:
            current = await next_request(resumed.interactions(), manual)
            assert current.call_id == request.call_id
            responses.respond(assistant_output("done"))
            await current.respond("answer")
            assert (await resumed.restored_turn.get_result()).final_output == "done"
    assert len(editor.operations) == 1
    assert len(responses.requests) == 2
