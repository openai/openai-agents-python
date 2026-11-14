"""Native managed denials keep authority while formatting default messages."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, InteractionExpiredError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import next_request, provider

DEFAULT = "Tool execution was rejected"


@pytest.mark.parametrize("mode", ["text", "empty", "none", "nonstring", "throw"])
async def test_default_managed_denial_uses_owned_formatter(responses, mode):
    effects = []
    seen = []
    context = object()

    @function_tool(needs_approval=True, error_function=None)
    def risky(value: str) -> str:
        effects.append(value)
        return value

    async def formatter(error):
        seen.append(error)
        assert error.context is context
        assert error.phase == "execution" and error.reason == "approval_rejected"
        assert isinstance(error.error, PermissionError)
        assert error.default_message == DEFAULT
        assert error.call.call_id == "denied"
        if mode == "throw":
            raise ValueError("private formatter failure")
        return {"text": "application denial", "empty": "", "none": None, "nonstring": 1}[mode]

    responses.respond(function_call("risky", {"value": "secret"}, "denied"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use risky.", tools=[risky], provider=provider(responses))
    async with (
        timeout(20),
        agent.session(context=context, tool_error_formatter=formatter) as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), risky)
        await request.reject()
        with pytest.raises(InteractionExpiredError):
            await request.reject()
        assert (await turn.get_result()).final_output == "done"
    assert len(seen) == 1 and effects == []
    expected = "application denial" if mode == "text" else "" if mode == "empty" else DEFAULT
    assert tool_output(responses.requests[1], "denied") == expected
    assert "private formatter failure" not in str(responses.requests[1])


@pytest.mark.parametrize("reason", ["explicit denial", DEFAULT, ""])
async def test_explicit_managed_denial_skips_formatter(responses, reason):
    seen = []

    @function_tool(needs_approval=True)
    def risky() -> str:
        raise AssertionError("Denied body ran")

    def formatter(error):
        seen.append(error)
        return "wrong"

    responses.respond(function_call("risky", {}, "explicit"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(instructions="Use risky.", tools=[risky], provider=provider(responses)).session(
            tool_error_formatter=formatter
        ) as session,
    ):
        turn = await session.submit("begin")
        await (await next_request(session.interactions(), risky)).reject(reason)
        await turn.get_result()
    assert seen == []
    assert tool_output(responses.requests[1], "explicit") == reason


@pytest.mark.parametrize("explicit", [False, True])
async def test_remembered_default_denial_formats_each_exact_call_and_preserves_omission(
    responses, explicit
):
    seen = []

    @function_tool(needs_approval=True, approval_key="fixture.risky")
    def risky() -> str:
        raise AssertionError("Denied body ran")

    def formatter(error):
        seen.append(error.call.call_id)
        return "deny-" + error.call.call_id

    agent = Agent(instructions="Use risky.", tools=[risky], provider=provider(responses))
    async with timeout(25):
        async with agent.session(tool_error_formatter=formatter) as session:
            for i in range(2):
                responses.respond(function_call("risky", {}, f"remembered-{i}"))
                responses.respond(assistant_output("done"))
                turn = await session.submit("begin")
                if i == 0:
                    request = await next_request(session.interactions(), risky)
                    if explicit:
                        await request.reject(DEFAULT, always=True)
                    else:
                        await request.reject(always=True)
                await turn.get_result()
                assert tool_output(responses.requests[-1], f"remembered-{i}") == (
                    DEFAULT if explicit else f"deny-remembered-{i}"
                )
            saved = await session.save_state()
            assert saved.remembered_approvals[0].reason == (DEFAULT if explicit else None)
        responses.respond(function_call("risky", {}, "restored"))
        responses.respond(assistant_output("done"))
        async with agent.session(
            state=saved, verify_remembered_approvals=lambda _: True, tool_error_formatter=formatter
        ) as session:
            await (await session.submit("resume")).get_result()
            assert tool_output(responses.requests[-1], "restored") == (
                DEFAULT if explicit else "deny-restored"
            )
    assert seen == ([] if explicit else ["remembered-0", "remembered-1", "restored"])


@pytest.mark.parametrize("late_return", [False, True])
async def test_cancelled_managed_denial_joins_formatter_and_suppresses_late_reply(
    responses, late_return
):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    @function_tool(needs_approval=True)
    def risky() -> str:
        raise AssertionError("Denied body ran")

    async def formatter(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if late_return:
                return "late denial"
            raise
        finally:
            cleaned.set()

    responses.respond(function_call("risky", {}, "cancelled"))
    async with (
        timeout(20),
        Agent(instructions="Use risky.", tools=[risky], provider=provider(responses)).session(
            tool_error_formatter=formatter
        ) as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), risky)
        denial = asyncio.create_task(request.reject())
        await entered.wait()
        await turn.cancel()
        await cleaned.wait()
        result = await asyncio.gather(denial, return_exceptions=True)
        assert isinstance(result[0], (asyncio.CancelledError, InteractionExpiredError))
        with pytest.raises(InteractionExpiredError):
            await request.approve()
        assert len(responses.requests) == 1


async def test_approved_managed_call_does_not_format_a_rejection(responses):
    effects = []
    seen = []

    @function_tool(needs_approval=True)
    def risky() -> str:
        effects.append("ran")
        return "accepted"

    def formatter(error):
        seen.append(error)
        return "wrong"

    responses.respond(function_call("risky", {}, "approved"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(instructions="Use risky.", tools=[risky], provider=provider(responses)).session(
            tool_error_formatter=formatter
        ) as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), risky)
        await request.approve()
        await turn.get_result()
    assert seen == [] and effects == ["ran"]
    assert tool_output(responses.requests[1], "approved") == "accepted"


async def test_approval_formatter_cannot_interrupt_its_own_session(responses):
    seen = []

    @function_tool(needs_approval=True)
    def risky() -> str:
        raise AssertionError("Denied body ran")

    async def formatter(_):
        with pytest.raises(RuntimeError, match="A tool callback cannot interrupt its own Session"):
            await asyncio.wait_for(turn.cancel(), timeout=0.3)
        seen.append("refused")
        return "controlled denial"

    responses.respond(function_call("risky", {}, "self-cancel"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(instructions="Use risky.", tools=[risky], provider=provider(responses)).session(
            tool_error_formatter=formatter
        ) as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), risky)
        await request.reject()
        assert (await turn.get_result()).final_output == "done"
    assert seen == ["refused"]
    assert tool_output(responses.requests[1], "self-cancel") == "controlled denial"


@pytest.mark.parametrize("kind", ["shell", "apply_patch"])
async def test_typed_managed_denial_formats_without_effects(responses, kind):
    from agents import apply_patch_tool, shell_tool
    from tests.test_native_application_shell import shell_call
    from tests.test_native_apply_patch import Editor, patch_call

    effects = []
    editor = Editor()
    seen = []
    observations = []

    def execute(context, call):
        effects.append(call)
        raise AssertionError("Denied Shell ran")

    if kind == "shell":
        tool = shell_tool(execute, needs_approval=True)
        frame = shell_call()
        frame["item"]["action"]["max_output_length"] = 200
        call_id = "shell-local"
    else:
        tool = apply_patch_tool(editor, needs_approval=True)
        frame = patch_call({"type": "delete_file", "path": "owned"})
        call_id = "native-patch"

    def formatter(error):
        assert error.reason == "approval_rejected"
        assert error.call.call_id == call_id
        assert error.call.tool.name == kind
        seen.append(error)
        return "typed denial"

    responses.respond(frame)
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use the typed tool.",
            model="gpt-5",
            tools=[tool],
            provider=provider(responses),
        ).session(tool_error_formatter=formatter, on_tool_lifecycle=observations.append) as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), tool)
        assert observations == []
        await request.reject()
        assert (await turn.get_result()).final_output == "done"
    assert len(seen) == 1 and effects == [] and editor.operations == []
    assert observations == []
    outputs = [
        item for item in responses.requests[1]["input"] if item["type"] == kind + "_call_output"
    ]
    assert len(outputs) == 1 and outputs[0]["call_id"] == call_id
    if kind == "shell":
        assert outputs[0]["output"] == [
            {"stdout": "", "stderr": "typed denial", "outcome": {"type": "exit", "exit_code": 1}}
        ]
    else:
        assert outputs[0]["status"] == "failed" and outputs[0]["output"] == "typed denial"


@pytest.mark.parametrize("kind", ["shell", "apply_patch"])
async def test_approved_typed_tool_delivers_lifecycle_around_effects(responses, kind):
    from agents import ShellOutput, apply_patch_tool, shell_tool
    from tests.test_native_application_shell import shell_call
    from tests.test_native_apply_patch import Editor, patch_call

    seen = []

    class ObservedEditor(Editor):
        def delete_file(self, operation):
            assert seen == ["start"]
            seen.append("body")
            return super().delete_file(operation)

    def execute(_context, _call):
        assert seen == ["start"]
        seen.append("body")
        return ShellOutput(output=[])

    if kind == "shell":
        tool = shell_tool(execute, needs_approval=True)
        frame = shell_call()
        call_id = "shell-local"
    else:
        tool = apply_patch_tool(ObservedEditor(), needs_approval=True)
        frame = patch_call({"type": "delete_file", "path": "owned"})
        call_id = "native-patch"

    def observe(event):
        assert event.call_id == call_id
        seen.append(event.kind)

    responses.respond(frame)
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use the typed tool.",
        model="gpt-5",
        tools=[tool],
        provider=provider(responses),
    )
    async with timeout(20), agent.session(on_tool_lifecycle=observe) as session:
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), tool)
        assert seen == []
        await request.approve()
        assert (await turn.get_result()).final_output == "done"
    assert seen == ["start", "body", "end"]
    outputs = [
        item for item in responses.requests[1]["input"] if item["type"] == kind + "_call_output"
    ]
    assert len(outputs) == 1 and outputs[0]["call_id"] == call_id
    if kind == "shell":
        assert outputs[0]["output"] == []
    else:
        assert outputs[0]["status"] == "completed"
        assert outputs[0]["output"] == "exact editor output"


async def test_custom_text_managed_denial_preserves_raw_input_and_typed_output(responses):
    from tests.test_native_custom_string_tools import RAW_INPUT, custom_call

    effects = []
    seen = []

    @function_tool(needs_approval=True)
    def raw_editor(source: str) -> str:
        effects.append(source)
        return source

    tool = raw_editor.as_custom()

    def formatter(error):
        assert error.reason == "approval_rejected"
        assert error.call.call_id == "raw" and error.call.tool.name == "raw_editor"
        seen.append(error)
        return "custom denial"

    responses.respond(custom_call())
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use raw_editor.",
            model="gpt-5",
            tools=[tool],
            provider=provider(responses),
        ).session(tool_error_formatter=formatter) as session,
    ):
        turn = await session.submit("begin")
        request = await next_request(session.interactions(), tool)
        assert request.effective_arguments.source == RAW_INPUT
        await request.reject()
        assert (await turn.get_result()).final_output == "done"
    assert len(seen) == 1 and effects == []
    assert tool_output(responses.requests[1], "raw", "custom_tool_call_output") == "custom denial"
