from typing import Any

import pytest

from agents import Agent, Allow, ApprovalRequested, ReplaceArguments, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import provider


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("mode", ["replace", "mutate", "boolean", "reorder", "allow"])
async def test_approved_arguments_bind_the_effect(responses, mode, restored):
    effects, guards = [], []

    def raw(value):
        return {"payload": {"value": value}, "order": {"a": 1, "b": 2}}

    def before(context):
        guards.append("entered")
        if len(guards) == 1:
            return ReplaceArguments(raw(1))
        if mode == "replace":
            return ReplaceArguments(raw(2))
        if mode == "boolean":
            return ReplaceArguments(raw(True))
        if mode == "reorder":
            return ReplaceArguments({"order": {"b": 2, "a": 1}, "payload": {"value": 1}})
        if mode == "mutate":
            context.effective_arguments["payload"]["value"] = 2
        return Allow()

    @function_tool(needs_approval=True, run_before_approval=True, before=before)
    def risky(payload: dict[str, Any], order: dict[str, int]) -> str:
        effects.append(payload["value"])
        return "accepted"

    responses.respond(function_call("risky", raw(0), "bound-input"))
    responses.respond(assistant_output("done"))
    async with timeout(20), provider(responses) as model:
        agent = Agent(instructions="Use risky.", tools=[risky], provider=model)
        state = None
        if restored:
            async with agent.session() as source:
                turn = await source.submit("begin")
                original = await anext(source.interactions())
                assert isinstance(original, ApprovalRequested)
                assert original.effective_arguments.payload["value"] == 1
                state = await source.suspend()
        async with agent.session(state=state) as session:
            turn = session.restored_turn if restored else await session.submit("begin")
            request = await anext(session.interactions())
            assert isinstance(request, ApprovalRequested)
            assert request.effective_arguments.payload["value"] == 1
            assert len(guards) == 1
            await request.approve()
            assert (await turn.get_result()).final_output == "done"
    assert len(guards) == 2
    allowed = mode in ("reorder", "allow", "mutate")
    assert effects == ([1] if allowed else [])
    if not allowed:
        assert (
            tool_output(responses.requests[1], "bound-input")
            == "Tool input policy changed approved arguments"
        )


@pytest.mark.parametrize("required", [False, True])
async def test_approved_opaque_values_keep_supported_copy_behavior(responses, required):
    from typing import Annotated

    from pydantic import AfterValidator

    class SlotValue:
        __slots__ = ("value",)

        def __init__(self, value):
            self.value = value

    effects = []

    @function_tool(needs_approval=True if required else lambda _: False)
    def opaque(value: Annotated[Any, AfterValidator(SlotValue)]) -> str:
        effects.append(value.value)
        return "accepted"

    responses.respond(function_call("opaque", {"value": 2}, "opaque-input"))
    responses.respond(assistant_output("done"))
    async with timeout(20), provider(responses) as model:
        async with Agent(
            instructions="Use opaque.", tools=[opaque], provider=model
        ).session() as session:
            turn = await session.submit("begin")
            if required:
                request = await anext(session.interactions())
                assert isinstance(request, ApprovalRequested)
                assert request.effective_arguments.value.value == 2
                await request.approve()
            assert (await turn.get_result()).final_output == "done"
    assert effects == [2]


async def test_replacement_cannot_change_pydantic_field_presence(responses):
    from pydantic import BaseModel

    class Options(BaseModel):
        enabled: bool = False

    effects = []

    @function_tool(
        needs_approval=True, before=lambda _: ReplaceArguments({"options": {"enabled": False}})
    )
    def configure(options: Options) -> str:
        effects.append(options.model_dump(exclude_unset=True))
        return "forbidden"

    responses.respond(function_call("configure", {"options": {}}, "field-presence"))
    responses.respond(assistant_output("done"))
    async with timeout(20), provider(responses) as model:
        async with Agent(
            instructions="Use configure.", tools=[configure], provider=model
        ).session() as session:
            turn = await session.submit("begin")
            request = await anext(session.interactions())
            assert request.effective_arguments.options.model_dump(exclude_unset=True) == {}
            await request.approve()
            assert (await turn.get_result()).final_output == "done"
    assert effects == []
    assert (
        tool_output(responses.requests[1], "field-presence")
        == "Tool input policy changed approved arguments"
    )


async def test_pending_predicate_cannot_publish_an_approval(responses):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    async def policy(_):
        entered.set()
        await release.wait()
        return True

    @function_tool(needs_approval=policy)
    def gated(value: int) -> str:
        effects.append(value)
        return "accepted"

    responses.respond(function_call("gated", {"value": 1}, "pending-policy"))
    responses.respond(assistant_output("done"))
    async with timeout(20), provider(responses) as model:
        async with Agent(
            instructions="Use gated.", tools=[gated], provider=model
        ).session() as session:
            turn = await session.submit("begin")
            await entered.wait()
            pending = asyncio.create_task(anext(session.interactions()))
            try:
                await asyncio.sleep(0.03)
                assert not pending.done()
                assert effects == []
                release.set()
                request = await pending
                assert isinstance(request, ApprovalRequested)
                await request.approve()
                assert (await turn.get_result()).final_output == "done"
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    assert effects == [1]


async def test_replacement_mapping_subclass_cannot_change_effect_semantics(responses):
    class ChangedGet(dict):
        def get(self, key, default=None):
            return 3 if key == "amount" else super().get(key, default)

    effects = []

    @function_tool(
        needs_approval=True, before=lambda _: ReplaceArguments({"payload": ChangedGet(amount=2)})
    )
    def mapped(payload: Any) -> str:
        effects.append(payload.get("amount"))
        return "forbidden"

    responses.respond(function_call("mapped", {"payload": {"amount": 2}}, "mapping-subclass"))
    responses.respond(assistant_output("done"))
    async with timeout(20), provider(responses) as model:
        async with Agent(
            instructions="Use mapped.", tools=[mapped], provider=model
        ).session() as session:
            turn = await session.submit("begin")
            request = await anext(session.interactions())
            assert request.effective_arguments.payload == {"amount": 2}
            await request.approve()
            assert (await turn.get_result()).final_output == "done"
    assert effects == []
    assert (
        tool_output(responses.requests[1], "mapping-subclass")
        == "Tool input policy changed approved arguments"
    )
