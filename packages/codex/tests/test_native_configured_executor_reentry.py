"""Configured reentry retains only the restored parent's current executor selection."""

import json

import pytest

from agents import ApprovalRequested, Environment, PendingRequests, SessionState, ToolRef
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import fixture


@pytest.mark.parametrize("selection", ["retained", "excluded", "narrowed"])
async def test_configured_reentry_respects_current_parent_executor_selection(
    tmp_path, monkeypatch, native_executor_binary, selection
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust

    async with timeout(45), fixture() as values:
        parent, _child, context, _root_model, child_model, effects, _guards, sibling = values
        retained = selection == "retained"
        if retained:
            child_model.replies.pop()
            child_model.respond(
                function_call(
                    "exec_command",
                    {"cmd": "printf retained-executor", "login": False},
                    "exec-after-restore",
                )
            )
            child_model.respond(assistant_output("child complete"))
        async with ExecutorPeer(
            tmp_path / "executor", synthetic_process_output="retained-executor"
        ).serve() as executor:
            trust(executor, tmp_path, monkeypatch)
            environment = Environment(
                url=executor.url,
                headers={"Authorization": "Bearer " + executor.token},
                cwd=str(executor.root),
                tools=["exec_command"],
                write_policy="legacy",
            )
            async with parent.session(context=context, environment=environment) as source:
                selected = None
                if selection != "narrowed":
                    selected = [
                        *source._bindings,
                        *([ToolRef("exec_command", "functions")] if retained else []),
                    ]
                turn = await source.submit(
                    "Restore the parent's executor selection.", tools=selected
                )
                boundary = await turn.wait_for_request_or_result()
                assert isinstance(boundary, PendingRequests)
                await sibling.wait()
                saved = await source.suspend()
            if selection == "narrowed":
                saved = replace_saved_operation(saved)
            async with parent.session(
                context=context, environment=environment, state=saved
            ) as restored:
                assert restored.restored_turn is not None
                pending = await restored.restored_turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                [request] = pending.requests
                assert isinstance(request, ApprovalRequested)
                [child] = restored._configured_children
                options = child.session._native_options
                assert options is not None
                allowed = json.loads(options)["tool_policy"]["allowed_tools"]
                assert ("exec_command" in {tool["name"] for tool in allowed}) == retained
                await restored.respond(request.reply(True))
                assert (await restored.restored_turn.get_result()).final_output == "parent complete"
            advertised = str(child_model.requests[-1].get("tools", []))
            assert ("exec_command" in advertised) == retained
            assert len(executor.synthetic_process_starts) == int(retained)
            assert effects == ["accepted-sibling", "child-effect"]


def replace_saved_operation(saved: SessionState) -> SessionState:
    # The child's old availability cannot override a narrowed parent operation.
    value = json.loads(saved.to_json())
    value["operation_tools"] = [
        {"name": "nested_child", "namespace": None},
        {"name": "accepted_sibling", "namespace": None},
    ]
    return SessionState.from_json(json.dumps(value))
