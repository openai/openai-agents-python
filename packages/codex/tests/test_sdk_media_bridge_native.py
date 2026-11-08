"""Public media behavior against the separately rebuilt Core image/file layers."""

from typing import Any

import pytest

from agents import (
    Agent,
    NativeProvider,
    SessionState,
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
