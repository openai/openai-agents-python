"""Public media behavior against the separately rebuilt Core image/file layers."""

import asyncio
from typing import Any

import pytest

from agents import (
    Agent,
    NativeProvider,
    SessionState,
    ToolContext,
    ToolOutput,
    ToolOutputFile,
    ToolOutputImage,
    ToolOutputText,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)


def media(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        part
        for item in items
        for part in item.get("content", [])
        if part["type"] in {"input_image", "input_file"}
    ]


@pytest.mark.parametrize(
    "reference",
    [
        {"file_id": "opaque-document"},
        {"file_data": "data:application/pdf;base64,JVBERi0xLjcK"},
        {"file_url": "https://files.invalid/document.pdf"},
    ],
)
async def test_public_user_tool_media_survive_current_and_portable_restore_without_replay(
    responses: ResponsesFixture, reference: dict[str, str]
) -> None:
    effects: list[str] = []

    @function_tool
    def picture() -> ToolOutput:
        effects.append("once")
        return ToolOutput(
            [
                ToolOutputText("before"),
                ToolOutputImage(file_id="opaque-tool-image", detail="low"),
                ToolOutputFile(**reference, filename="tool.pdf"),
                ToolOutputText("after"),
            ]
        )

    responses.respond(function_call("picture", {}, "media-call"))
    responses.respond(assistant_output("accepted"))
    responses.respond(assistant_output("restored"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-media-bridge")
    agent = Agent(
        instructions="Use picture once.", model="gpt-5.5", tools=[picture], provider=provider
    )
    expected = [
        {"type": "input_image", "file_id": "opaque-user-image", "detail": "low"},
        {"type": "input_file", **reference, "filename": "user.pdf"},
    ]
    async with timeout(30), agent.session() as session:
        result = await (await session.submit({"role": "user", "content": expected})).get_result()  # type: ignore[typeddict-item]
        assert result.final_output == "accepted"
        assert media(responses.requests[0]["input"]) == expected
        current = [item async for item in session.items()]
        assert media(current) == expected
        output = next(
            item["output"]
            for item in responses.requests[1]["input"]
            if item.get("call_id") == "media-call" and item["type"] == "function_call_output"
        )
        assert output == [
            {"type": "input_text", "text": "before"},
            {"type": "input_image", "file_id": "opaque-tool-image", "detail": "low"},
            {"type": "input_file", **reference, "filename": "tool.pdf"},
            {"type": "input_text", "text": "after"},
        ]
        assert output == next(
            item["output"] for item in current if item["type"] == "function_call_output"
        )
        state = SessionState.from_json((await session.save_state()).to_json())
    async with timeout(30), agent.session(state=state) as restored:
        assert media([item async for item in restored.items()]) == expected
        assert (
            await (await restored.submit("continue without repeating picture")).get_result()
        ).final_output == "restored"
        assert output == next(
            item["output"]
            for item in responses.requests[2]["input"]
            if item.get("call_id") == "media-call" and item["type"] == "function_call_output"
        )
    assert effects == ["once"]
    assert len(responses.requests) == 3


async def test_code_file_awaits_the_owned_callback_and_restores_without_replay(
    responses: ResponsesFixture,
) -> None:
    from tests.test_native_code_mode import code_call

    entered, release = asyncio.Event(), asyncio.Event()
    routes: list[str] = []

    @function_tool
    async def reference(ctx: ToolContext[None]) -> ToolOutput:
        routes.append(ctx.call.route)
        entered.set()
        await release.wait()
        return ToolOutput(
            [
                ToolOutputText("before-file"),
                ToolOutputFile(file_id="opaque-code-file", filename="code.pdf"),
                ToolOutputText("after-file"),
            ]
        )

    responses.respond(
        code_call(
            "for (const part of await tools.reference({})) { "
            'if (part.type === "input_file") { const {type, ...source} = part; file(source); } '
            'else if (part.type === "input_text") { text(part.text); } }',
            "file-cell",
        )
    )
    responses.respond(assistant_output("cell complete"))
    responses.respond(assistant_output("cell restored"))
    expected = [
        {"type": "input_text", "text": "before-file"},
        {"type": "input_file", "file_id": "opaque-code-file", "filename": "code.pdf"},
        {"type": "input_text", "text": "after-file"},
    ]
    agent = Agent(
        instructions="Use reference from code once.",
        model="gpt-5.5",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-code-file"),
        tools=[reference],
        programmatic_tool_calling=True,
    )
    async with timeout(45), agent.session() as session:
        try:
            handle = await session.submit("Run the cell.")
            await entered.wait()
            assert len(responses.requests) == 1
            with pytest.raises(RuntimeError, match="idle Session"):
                await session.save_state()
            release.set()
            assert (await handle.get_result()).final_output == "cell complete"
            assert (
                next(
                    item["output"]
                    for item in responses.requests[1]["input"]
                    if item.get("type") == "custom_tool_call_output"
                    and item.get("call_id") == "file-cell"
                )
                == expected
            )
            state = SessionState.from_json((await session.save_state()).to_json())
        finally:
            release.set()
    async with timeout(30), agent.session(state=state) as restored:
        assert (await (await restored.submit("Continue.")).get_result()).final_output == (
            "cell restored"
        )
        assert (
            next(
                item["output"]
                for item in responses.requests[2]["input"]
                if item.get("type") == "custom_tool_call_output"
                and item.get("call_id") == "file-cell"
            )
            == expected
        )
    assert routes == ["code"]
    assert len(responses.requests) == 3
