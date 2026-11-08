"""The supported native profile rejects a known lossy image format before publication."""

import pytest

from agents import Agent, NativeError, ToolOutput, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import function_call, responses as responses
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
