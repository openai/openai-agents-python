"""Public media behavior against the separately rebuilt Core image/file layers."""

import asyncio
import os
from typing import Any

import pytest

from agents import (
    Agent,
    CustomStorage,
    NativeProvider,
    SessionState,
    SQLiteSessionStore,
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
            accepted_output = next(
                item["output"]
                for item in responses.requests[1]["input"]
                if item.get("type") == "custom_tool_call_output"
                and item.get("call_id") == "file-cell"
            )
            assert accepted_output[0]["type"] == "input_text"
            assert accepted_output[0]["text"].startswith("Script completed\n")
            assert accepted_output[0]["text"].endswith("Output:\n")
            assert accepted_output[1:] == expected
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
            == accepted_output
        )
    assert routes == ["code"]
    assert len(responses.requests) == 3


@pytest.mark.skipif(os.name != "posix", reason="Reference adapter requires POSIX locks")
async def test_persistent_media_reopens_as_data_without_replaying_the_tool(
    responses: ResponsesFixture, tmp_path
) -> None:
    effects: list[str] = []

    @function_tool
    def reference() -> ToolOutput:
        effects.append("once")
        return ToolOutput(
            [
                ToolOutputImage(file_id="stored-image", detail="high"),
                ToolOutputFile(file_url="https://files.invalid/stored.pdf", filename="stored.pdf"),
            ]
        )

    responses.respond(function_call("reference", {}, "stored-media"))
    responses.respond(assistant_output("stored"))
    responses.respond(assistant_output("reopened"))
    store = SQLiteSessionStore(tmp_path / "media.sqlite")
    agent = Agent(
        instructions="Use reference once.",
        model="gpt-5.5",
        tools=[reference],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-stored-media"),
    )
    user_content = [{"type": "input_file", "file_id": "stored-document", "filename": "user.pdf"}]
    expected_output = [
        {"type": "input_image", "file_id": "stored-image", "detail": "high"},
        {
            "type": "input_file",
            "file_url": "https://files.invalid/stored.pdf",
            "filename": "stored.pdf",
        },
    ]
    async with timeout(30), agent.session(storage=CustomStorage(store)) as session:
        result = await (
            await session.submit({"role": "user", "content": user_content})
        ).get_result()
        assert result.final_output == "stored"
        identity = session.id
        assert media([item async for item in session.items()]) == user_content
    async with (
        timeout(30),
        agent.session(storage=CustomStorage(store), resume=identity) as reopened,
    ):
        current = [item async for item in reopened.items()]
        assert media(current) == user_content
        assert (
            next(
                item["output"]
                for item in current
                if item.get("call_id") == "stored-media"
                and item.get("type") == "function_call_output"
            )
            == expected_output
        )
        assert (await (await reopened.submit("Continue.")).get_result()).final_output == "reopened"
        assert (
            next(
                item["output"]
                for item in responses.requests[2]["input"]
                if item.get("call_id") == "stored-media"
                and item.get("type") == "function_call_output"
            )
            == expected_output
        )
    assert effects == ["once"]
    assert len(responses.requests) == 3
