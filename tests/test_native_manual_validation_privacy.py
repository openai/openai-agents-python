"""Public manual validation keeps validator failures out of the caller exception."""

import traceback
from typing import Any, cast

import pytest
from pydantic import BaseModel

from agents import Agent, FunctionTool
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import LookupArgs, next_request, provider


@pytest.mark.asyncio
async def test_rejected_manual_output_has_no_validator_exception_chain_and_can_retry(
    responses,
):
    class Answer(BaseModel):
        value: int

    tool = FunctionTool(
        name="lookup", description="Lookup", input_type=LookupArgs, output_type=Answer
    )
    owner = provider(responses)
    agent = Agent(instructions="Use lookup.", tools=[tool], provider=owner)
    responses.respond(function_call("lookup", {"key": "a"}, "one"))
    responses.respond(assistant_output("done"))
    async with timeout(15), agent.session() as session:
        handle = await session.submit("lookup")
        request = await next_request(session.interactions(), tool)
        secret = "private-value-must-not-enter-validation-trace"
        with pytest.raises(ValueError, match="failed validation") as caught:
            await request.respond(cast(Any, {"value": secret}))
        assert caught.value.__context__ is None
        assert secret not in "".join(traceback.format_exception(caught.value))
        assert len(responses.requests) == 1
        assert await request.respond(cast(Any, {"value": 7})) is None
        assert (await (handle).get_result()).final_output == "done"
        assert len(responses.requests) == 2
    await owner.aclose()
