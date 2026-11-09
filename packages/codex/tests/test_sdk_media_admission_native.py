"""The supported native profile rejects a known lossy image format before publication."""

import json
from collections import deque

import pytest

from agents import Agent, NativeAgents, NativeError, ToolContext, ToolOutput, function_tool
from agents._native import NativeThread
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_child_agents import collaboration, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_terminal_tools import provider

IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg=="
)


@pytest.mark.parametrize("lane", ["seed", "user", "tool"])
async def test_detail_based_inline_low_is_rejected_instead_of_becoming_text(responses, lane):
    @function_tool
    def picture() -> ToolOutput:
        return ToolOutput.image(IMAGE, detail="low")

    image = {
        "role": "user",
        "content": [{"type": "input_image", "image_url": IMAGE, "detail": "low"}],
    }
    agent = Agent(
        instructions="Use the image.",
        model="gpt-5.5",
        tools=[picture] if lane == "tool" else [],
        provider=provider(responses),
    )
    if lane == "tool":
        responses.respond(function_call("picture", {}, "lossy-image"))
    async with timeout(30):
        with pytest.raises(
            (NativeError, RuntimeError, ValueError), match="detail 'low' is not supported"
        ):
            async with agent.session(
                initial_messages=[image] if lane == "seed" else None
            ) as session:
                result = await (
                    await session.submit(image if lane == "user" else "show it")
                ).get_result()
                pytest.fail(f"lossy media produced a Completed result: {result!r}")
    assert len(responses.requests) == (1 if lane == "tool" else 0)


@pytest.mark.parametrize("media", ["image", "file"])
async def test_retired_automatic_media_reply_returns_false_without_validation(
    responses, monkeypatch, media
):
    replies = []
    original = NativeThread._respond_python_tool

    def capture(self, reply):
        replies.append(reply)
        return original(self, reply)

    monkeypatch.setattr(NativeThread, "_respond_python_tool", capture)

    @function_tool
    def settled() -> str:
        return "settled"

    responses.respond(function_call("settled", {}, "retired-media"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(30),
        Agent(
            instructions="Use settled.",
            model="gpt-5.5",
            tools=[settled],
            provider=provider(responses),
        ).session() as session,
    ):
        assert (await (await session.submit("go")).get_result()).final_output == "done"
        reply = json.loads(replies[0])
        candidate = (
            ToolOutput.image(IMAGE, detail="low")
            if media == "image"
            else ToolOutput.file(file_id="retired-file")
        )
        reply["response"] = candidate.to_native()
        assert not await session._native._respond_python_tool(json.dumps(reply))
        assert len(responses.requests) == 2


async def test_child_media_rejection_stops_only_its_owner(responses, tmp_path, monkeypatch):
    paths, calls = [], []

    @function_tool
    def picture(ctx: ToolContext[None]) -> ToolOutput:
        calls.append(ctx.call)
        return ToolOutput.image(IMAGE, detail="low")

    class Program(deque):
        def __bool__(self):
            return True

        def popleft(self):
            path = metadata(responses.requests[-1], responses.request_headers[-1])["agent_name"]
            paths.append(path)
            if path != "/root":
                if paths.count(path) == 1:
                    return [function_call("picture", {}, "child-lossy-image")]
                return [assistant_output("unwanted child continuation")]
            if paths.count(path) == 1:
                return [
                    collaboration(
                        "spawn_agent",
                        {"task_name": "child", "message": "Use picture.", "fork_turns": "none"},
                        "spawn-child",
                    )
                ]
            return [assistant_output("root remains usable")]

    responses.replies = Program()
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Start the child.",
            model="gpt-5.5",
            tools=[picture, NativeAgents(max_children=1)],
            provider=provider(responses),
        )
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Start.")
            assert (await root.get_result()).final_output == "root remains usable"
            [child] = await handles(session, 1)
            with pytest.raises(
                (NativeError, RuntimeError, ValueError), match="detail 'low' is not supported"
            ):
                await child.get_result()
            assert len(calls) == 1
            assert calls[0].child_id == child.child_id
            assert calls[0].child_incarnation == child._native.incarnation
            assert calls[0].turn_id == child.turn_id
            assert len([path for path in paths if path != "/root"]) == 1
            assert "unwanted child continuation" not in (await session.save_state()).to_json()
            assert (
                await (await session.submit("Continue explicitly.")).get_result()
            ).final_output == "root remains usable"
            assert len([path for path in paths if path != "/root"]) == 1
