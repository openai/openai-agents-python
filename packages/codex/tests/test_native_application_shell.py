"""Application-managed Shell over the real native Responses transport."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agents import (
    Agent,
    Allow,
    LocalStorage,
    NativeError,
    ReplaceArguments,
    SessionState,
    ShellCall,
    ShellCommandOutput,
    ShellExit,
    ShellOutput,
    ShellSkill,
    ToolContext,
    ToolOutput,
    shell_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_manual_tools import next_request, provider


def shell_call(**fields: Any) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "id": "sh_native_application",
            "type": "shell_call",
            "call_id": "shell-local",
            "action": {"commands": ["printf sample"], "max_output_length": 5, "timeout_ms": 250},
            **fields,
        },
    }


@pytest.mark.parametrize("approved", [True, False])
async def test_pending_shell_approval_rebinds_current_callback_after_portable_restore(
    responses: ResponsesFixture, approved: bool
) -> None:
    effects: list[tuple[str, ShellCall]] = []

    def original(_context: ToolContext[str], _call: ShellCall) -> ShellOutput:
        raise AssertionError("The source callback must not execute")

    def current(context: ToolContext[str], call: ShellCall) -> ShellOutput:
        effects.append((context.context, call))
        return ShellOutput(
            max_output_length=3,
            output=[ShellCommandOutput(stdout="abcdef", stderr="", outcome=ShellExit(exit_code=0))],
        )

    source_tool = shell_tool(original, needs_approval=True)
    current_tool = shell_tool(current, needs_approval=True)
    agent = Agent(
        instructions="Use the application Shell.", tools=[source_tool], provider=provider(responses)
    )
    responses.respond(shell_call())
    responses.respond(assistant_output("done"))
    async with timeout(30), agent.session(context="source") as source:
        await source.submit("Execute the configured command.")
        await next_request(source.interactions(), source_tool)
        assert effects == [] and len(responses.requests) == 1
        saved = SessionState.from_json((await source.suspend()).to_json())
    restored_agent = replace(agent, tools=[current_tool])
    async with timeout(30), restored_agent.session(context="current", state=saved) as restored:
        request = await next_request(restored.interactions(), current_tool)
        assert request.call_id == "shell-local"
        assert effects == [] and len(responses.requests) == 1
        if approved:
            await request.approve()
        else:
            await request.reject("Application denied execution")
        assert restored.restored_turn is not None
        assert (await restored.restored_turn.get_result()).final_output == "done"
    assert len(effects) == int(approved)
    receipts = [
        item for item in responses.requests[1]["input"] if item["type"] == "shell_call_output"
    ]
    assert len(receipts) == 1 and receipts[0]["call_id"] == "shell-local"
    if approved:
        assert effects[0][0] == "current"
        assert effects[0][1].action.commands == ["printf sample"]
        assert effects[0][1].action.timeout_ms == 250
        assert effects[0][1].action.max_output_length == 5
        assert receipts[0]["max_output_length"] == 3
        assert receipts[0]["output"][0]["stdout"] == "abc"
    else:
        assert receipts[0]["output"][0]["stderr"] == "Application denied execution"


@pytest.mark.parametrize("changed", ["declaration", "action", "effective_action"])
async def test_pending_shell_restore_rejects_changed_declaration_or_saved_action(
    responses: ResponsesFixture, changed: str
) -> None:
    import json

    from agents import function_tool

    effects: list[str] = []

    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        effects.append("shell")
        return ShellOutput(output=[])

    tool = shell_tool(execute, needs_approval=True)
    agent = Agent(
        instructions="Use the application Shell.", tools=[tool], provider=provider(responses)
    )
    responses.respond(shell_call())
    async with timeout(30), agent.session() as source:
        await source.submit("Execute the configured command.")
        await next_request(source.interactions(), tool)
        raw = json.loads((await source.suspend()).to_json())
    if changed == "declaration":

        @function_tool(name="shell", needs_approval=True)
        def ordinary() -> str:
            effects.append("ordinary")
            return "unreachable"

        agent = replace(agent, tools=[ordinary])
    else:
        pending = raw["native_continuation"]["pending_manual"][0]
        field = "arguments" if changed == "action" else "effective_arguments"
        value = json.loads(pending.get(field) or pending["arguments"])
        value["action"]["commands"] = ["changed command"]
        pending[field] = json.dumps(value)
    state = SessionState.from_json(json.dumps(raw))
    with pytest.raises((RuntimeError, ValueError)):
        async with timeout(30), agent.session(state=state):
            pytest.fail("A changed Shell grant reached a current callback")
    assert effects == [] and len(responses.requests) == 1


@pytest.mark.parametrize("approved", [True, False])
async def test_application_shell_gates_execution_and_replays_typed_output(
    responses: ResponsesFixture, approved: bool
) -> None:
    effects: list[tuple[str, ShellCall]] = []

    def execute(context: ToolContext[str], call: ShellCall) -> ShellOutput:
        effects.append((context.context, call))
        return ShellOutput(
            max_output_length=3,
            output=[
                ShellCommandOutput(
                    stdout="é🙂long", stderr="details", outcome=ShellExit(exit_code=0)
                )
            ],
        )

    skill: ShellSkill = {
        "name": "example",
        "description": "Synthetic test skill",
        "path": "/workspace/SKILL.md",
    }
    tool = shell_tool(execute, skills=[skill], needs_approval=True, before=lambda _: Allow())
    frame = shell_call()
    responses.respond(frame)
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        model="gpt-5",
        provider=provider(responses),
        tools=[tool],
    )

    async with timeout(30), agent.session(context="application-context") as session:
        turn = await session.submit("run the test command")
        request = await next_request(session.interactions(), tool)
        assert request.call_id == "shell-local"
        assert effects == [] and len(responses.requests) == 1
        if approved:
            await request.approve()
        else:
            await request.reject("deny")
        assert (await turn.get_result()).final_output == "done"
        state = SessionState.from_json((await session.save_state()).to_json())

    shell_specs = [spec for spec in responses.requests[0]["tools"] if spec["type"] == "shell"]
    assert shell_specs == [{"type": "shell", "environment": {"type": "local", "skills": [skill]}}]
    assert not any(
        spec.get("name") == "shell"
        for spec in responses.requests[0]["tools"]
        if spec["type"] == "function"
    )

    output = next(
        item for item in responses.requests[1]["input"] if item["type"] == "shell_call_output"
    )
    assert output["call_id"] == "shell-local"
    if approved:
        assert len(effects) == 1
        context, call = effects[0]
        assert context == "application-context"
        assert call.call_id == "shell-local" and call.status is None
        assert call.action.commands == ["printf sample"] and call.action.timeout_ms == 250
        assert output["max_output_length"] == 3
        assert output["output"] == [
            {"stdout": "é🙂l", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
        ]
    else:
        assert effects == []
        assert output["output"] == [
            {"stdout": "", "stderr": "deny", "outcome": {"type": "exit", "exit_code": 1}}
        ]

    responses.respond(assistant_output("replayed"))
    async with timeout(30), agent.session(context="application-context", state=state) as restored:
        turn = await restored.submit("continue")
        assert (await turn.get_result()).final_output == "replayed"
    assert len(effects) == int(approved)
    replayed = responses.requests[2]["input"]
    assert next(item for item in replayed if item["type"] == "shell_call_output") == output


async def test_application_shell_keeps_acknowledged_local_receipt(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    calls: list[str] = []

    def execute(_context: ToolContext[None], call: ShellCall) -> ShellOutput:
        calls.append(call.call_id)
        return ShellOutput(
            max_output_length=5,
            output=[ShellCommandOutput(stdout="saved", stderr="", outcome=ShellExit(exit_code=0))],
        )

    responses.respond(shell_call())
    responses.respond(assistant_output("done"))
    responses.respond(assistant_output("continued"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        model="gpt-5",
        provider=provider(responses),
        tools=[shell_tool(execute)],
    )
    storage = LocalStorage(tmp_path / "profile")

    async with timeout(30), agent.session(storage=storage) as session:
        thread_id = session.id
        turn = await session.submit("run the test command")
        assert (await turn.get_result()).final_output == "done"
    async with timeout(30), agent.session(storage=storage, resume=thread_id) as restored:
        turn = await restored.submit("continue")
        assert (await turn.get_result()).final_output == "continued"

    assert calls == ["shell-local"]
    assert len(responses.requests) == 3
    initial = [
        item for item in responses.requests[1]["input"] if item["type"] == "shell_call_output"
    ]
    replayed = [
        item for item in responses.requests[2]["input"] if item["type"] == "shell_call_output"
    ]
    assert len(initial) == 1
    assert initial[0]["call_id"] == "shell-local"
    assert initial[0]["max_output_length"] == 5
    assert initial[0]["output"] == [
        {"stdout": "saved", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
    ]
    assert len(replayed) == 1
    assert replayed[0]["call_id"] == initial[0]["call_id"]
    assert replayed[0]["max_output_length"] == initial[0]["max_output_length"]
    assert replayed[0]["output"] == initial[0]["output"]


@pytest.mark.parametrize(
    "receipt_case",
    [
        "paired",
        "typed-auto",
        "typed-reference",
        "missing-reference-id",
        "blank-reference-id",
        "numeric-reference-id",
        "unrelated",
        "duplicate-output",
        "duplicate-call",
        "empty-environment",
        "unknown-environment",
        "numeric-container",
        "empty-container",
        "blank-container",
    ],
)
async def test_application_shell_accepts_only_paired_provider_container_receipts(
    responses: ResponsesFixture, receipt_case: str
) -> None:
    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        pytest.fail("The provider's container call reached the application callback")

    item = {
        "id": "sh_hosted_output",
        "type": "shell_call_output",
        "call_id": "shell-other" if receipt_case == "unrelated" else "shell-local",
        "status": "completed",
        "output": [{"stdout": "hosted", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}],
    }
    hosted_markers = {
        "typed-auto": {"environment": {"type": "container_auto"}},
        "typed-reference": {
            "environment": {"type": "container_reference", "container_id": "cntr-hosted"}
        },
        "missing-reference-id": {"environment": {"type": "container_reference"}},
        "blank-reference-id": {"environment": {"type": "container_reference", "container_id": " "}},
        "numeric-reference-id": {"environment": {"type": "container_reference", "container_id": 0}},
        "empty-environment": {"environment": {}},
        "unknown-environment": {"environment": {"type": "unknown"}},
        "numeric-container": {"container_id": 0},
        "empty-container": {"container_id": ""},
        "blank-container": {"container_id": " "},
    }
    frames = [
        shell_call(
            **hosted_markers.get(receipt_case, {"container_id": "cntr-hosted"}),
            internal_chat_message_metadata_passthrough={"turn_id": "provider-turn"},
        ),
        {"type": "response.output_item.done", "item": item},
    ]
    if receipt_case == "duplicate-call":
        frames.insert(1, shell_call(container_id="cntr-hosted", id="sh_duplicate_call"))
    if receipt_case == "duplicate-output":
        frames.append(
            {"type": "response.output_item.done", "item": {**item, "id": "sh_duplicate_output"}}
        )
    responses.respond(*frames, assistant_output("provider done"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        provider=provider(responses),
        tools=[shell_tool(execute)],
    )
    async with timeout(30):
        if receipt_case in {"paired", "typed-auto", "typed-reference"}:
            assert (
                await agent.run("run in the provider container")
            ).final_output == "provider done"
        else:
            with pytest.raises(
                NativeError, match="Model returned a receipt for an application shell call"
            ):
                await agent.run("run in the provider container")
    assert len(responses.requests) == 1


@pytest.mark.parametrize("raw_shape", [False, True])
async def test_application_shell_rejects_argument_replacement_before_execution(
    responses: ResponsesFixture,
    raw_shape: bool,
) -> None:
    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        pytest.fail("A rejected replacement reached the Shell executor")

    def before(context: ToolContext[None]) -> ReplaceArguments:
        if raw_shape:
            return ReplaceArguments(context.effective_arguments["call"].model_dump())
        return ReplaceArguments(dict(context.effective_arguments))

    responses.respond(shell_call())
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        provider=provider(responses),
        tools=[shell_tool(execute, before=cast(Any, before))],
    )
    async with timeout(15):
        with pytest.raises(TypeError, match="Shell before policies must return Allow or Deny"):
            await agent.run("Run the test command.")


def test_application_shell_rejects_generic_after_replacement() -> None:
    def execute(_context: ToolContext[None], _call: ShellCall) -> ShellOutput:
        return ShellOutput(output=[])

    with pytest.raises(TypeError, match="Shell tools require typed ShellOutput from the executor"):
        replace(shell_tool(execute), after=lambda *_: ToolOutput.text("unstructured"))


@pytest.mark.parametrize("selection", ["call", "output", "both", "neither"])
async def test_selective_compaction_preserves_application_shell_pairs(
    responses: ResponsesFixture, selection: str
) -> None:
    effects: list[str] = []

    def execute(context: ToolContext[None], call: ShellCall) -> ShellOutput:
        effects.append(call.call_id)
        return ShellOutput(
            output=[
                ShellCommandOutput(stdout="accepted", stderr="", outcome=ShellExit(exit_code=0))
            ]
        )

    responses.respond(shell_call())
    responses.respond(assistant_output("completed"))
    agent = Agent(
        instructions="Use the configured Shell tool.",
        model="gpt-5",
        provider=provider(responses),
        tools=[shell_tool(execute)],
    )
    async with timeout(30), agent.session() as session:
        assert (
            await (await session.submit("run the test command")).get_result()
        ).final_output == "completed"
        snapshot = await session.model_context()
        pair = [
            index
            for index, item in enumerate(snapshot.items)
            if item.get("type") in {"shell_call", "shell_call_output"}
        ]
        assert [snapshot.items[index]["type"] for index in pair] == [
            "shell_call",
            "shell_call_output",
        ]
        retain = {"call": pair[:1], "output": pair[1:], "both": pair, "neither": []}[selection]
        before = await session.save_state()
        request_count = len(responses.requests)
        if selection in {"call", "output"}:
            with pytest.raises(RuntimeError, match="retain both original tool call and result"):
                await session.compact(
                    summary="Reject a split Shell pair.", snapshot=snapshot, retain=retain
                )
            after = await session.save_state()
            assert after.history[: len(before.history)] == before.history
            assert not any(row["type"] == "compacted" for row in after.history)
        else:
            await session.compact(
                summary="Keep the selected complete window.", snapshot=snapshot, retain=retain
            )
            after = await session.save_state()
            (compacted,) = (row for row in after.history if row["type"] == "compacted")
            selected = [
                item["type"]
                for item in compacted["payload"]["replacement_history"]
                if item.get("type") in {"shell_call", "shell_call_output"}
            ]
            assert selected == (["shell_call", "shell_call_output"] if selection == "both" else [])
        assert effects == ["shell-local"]
        assert len(responses.requests) == request_count
