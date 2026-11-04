"""Historical projections preserve recorded identities and recorded ordering."""

import copy

import pytest

from agents._history import project_history
from agents._projection import project_execution_completion


def event(kind, turn="t1", **fields):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **fields}}


def response(kind, **fields):
    return {"type": "response_item", "payload": {"type": kind, **fields}}


def test_recorded_items_and_usage_survive_inherited_turn_history():
    history = [
        event("task_started", "parent-turn", started_at=100),
        response(
            "message",
            id="parent-message",
            role="assistant",
            content=[{"type": "output_text", "text": "Inherited"}],
        ),
        event("task_complete", "parent-turn", completed_at=110),
        event("task_started", started_at=120),
        event(
            "item_completed",
            item={
                "type": "UserMessage",
                "id": "prompt",
                "content": [{"type": "text", "text": "Inspect this"}],
            },
        ),
        response(
            "message",
            id="answer",
            role="assistant",
            content=[{"type": "output_text", "text": "Recorded"}],
        ),
        event(
            "item_completed",
            item={
                "type": "AgentMessage",
                "id": "answer",
                "content": [{"type": "Text", "text": "Recorded"}],
                "phase": "final_answer",
            },
        ),
        {
            "type": "token_usage_record",
            "payload": {
                "turn_id": "t1",
                "turn_token_usage": {
                    "input_tokens": 12,
                    "output_tokens": 4,
                    "total_tokens": 16,
                },
            },
        },
        event(
            "task_complete",
            completed_at=130,
            last_agent_message="A summary without an item identity",
        ),
    ]
    before = copy.deepcopy(history)
    turns, items = project_history(history, "root", "child")
    assert [turn["id"] for turn in turns] == ["parent-turn", "t1"]
    assert [(turn["started_at"], turn["completed_at"]) for turn in turns] == [
        (100, 110),
        (120, 130),
    ]
    assert all(turn["session_id"] == "root" and turn["subagent_id"] == "child" for turn in turns)
    assert turns[1]["usage"]["total_tokens"] == 16
    assert [item["id"] for item in items] == ["parent-message", "prompt", "answer"]
    assert items[1]["content"] == [{"type": "input_text", "text": "Inspect this"}]
    assert items[2]["phase"] == "final_answer"
    assert items[2]["content"] == [{"type": "output_text", "text": "Recorded"}]
    assert history == before


@pytest.mark.parametrize("terminal, expected", [(False, "in_progress"), (True, "incomplete")])
def test_legacy_tool_calls_pair_only_with_actual_outputs(terminal, expected):
    history = [
        event("task_started"),
        response(
            "function_call",
            id="model-call",
            call_id="lookup",
            name="lookup",
            namespace="catalog",
            arguments='{"key": 3}',
        ),
        response("function_call_output", id="tool-result", call_id="lookup", output="found"),
        response(
            "function_call",
            id="pending-call",
            call_id="pending",
            name="update",
            arguments="{}",
        ),
        response(
            "message",
            role="user",
            content=[{"type": "input_text", "text": "No stable identity"}],
        ),
    ]
    if terminal:
        history.append(event("turn_aborted"))
    turns, items = project_history(history, "root")
    assert [item["id"] for item in items] == [
        "model-call",
        "tool-result",
        "pending-call",
    ]
    assert items[0]["name"] == "catalog.lookup"
    assert items[0]["status"] == "completed"
    assert items[1]["output"] == "found"
    assert items[2]["status"] == expected
    assert turns[0]["created_at"] is turns[0]["started_at"] is turns[0]["completed_at"] is None
    assert turns[0]["status"] == ("cancelled" if terminal else "in_progress")


def test_native_tool_records_override_matching_raw_calls_without_duplication():
    native = {
        "type": "DynamicToolCall",
        "id": "call",
        "namespace": None,
        "tool": "lookup",
        "arguments": {"key": 2},
        "status": "completed",
        "success": True,
        "content_items": [],
    }
    history = [
        event("task_started", started_at=1),
        response(
            "function_call",
            id="model-call",
            call_id="call",
            name="lookup",
            arguments='{"key":2}',
        ),
        event("item_completed", item=native),
        response("function_call_output", call_id="call", output=[]),
        event("task_complete", completed_at=2),
    ]
    _, items = project_history(history, "root")
    assert [item["type"] for item in items] == ["function_call", "function_call_output"]
    assert items[0]["arguments"] == {"key": 2}
    assert items[0]["call_id"] == items[1]["call_id"] == "call"
    assert items[1]["output"] == []


def test_specialized_native_call_keeps_generic_response_record():
    history = [
        event("task_started"),
        response(
            "function_call",
            call_id="shell",
            name="exec_command",
            arguments='{"cmd":"ls"}',
        ),
        event(
            "item_completed",
            item={
                "type": "CommandExecution",
                "id": "shell",
                "command": ["ls"],
                "status": "completed",
                "aggregated_output": "README.md",
                "exit_code": 0,
            },
        ),
        event("task_complete"),
    ]
    _, items = project_history(history, "root")
    call, execution = items
    assert call == {
        "type": "function_call",
        "id": "shell:0",
        "turn_id": "t1",
        "call_id": "shell",
        "name": "exec_command",
        "arguments": '{"cmd":"ls"}',
        "status": "incomplete",
    }
    assert execution["type"] == "command_execution"
    assert execution["id"] == execution["call_id"] == call["call_id"]
    assert execution["output"] == "README.md"
    assert execution["exit_code"] == 0


def test_late_command_completion_keeps_its_recorded_thread_and_turn():
    native = event(
        "item_completed",
        "interrupted",
        thread_id="origin",
        item={
            "type": "CommandExecution",
            "id": "shell",
            "command": ["sh", "-c", "echo 'two words'"],
            "cwd": "file:///executor/work",
            "status": "failed",
            "exit_code": 137,
        },
    )
    message = native["payload"]
    observed = project_execution_completion(message, thread_id="origin", turn_id="interrupted")
    assert observed is not None
    assert observed["type"] == "command_execution"
    assert observed["argv"] == ["sh", "-c", "echo 'two words'"]
    assert observed["cwd"] == "file:///executor/work"
    assert observed["call_id"] == observed["id"] == "shell"
    assert observed["native_status"] == "failed"
    assert observed["exit_code"] == 137
    assert project_execution_completion(message, thread_id="child", turn_id="interrupted") is None
    assert project_execution_completion(message, thread_id="origin", turn_id="next") is None
    turns, items = project_history(
        [
            event("task_started", "interrupted"),
            event("turn_aborted", "interrupted"),
            event("task_started", "next"),
            native,
            response(
                "message",
                id="next-message",
                role="assistant",
                content=[{"type": "output_text", "text": "Next"}],
            ),
            event("task_complete", "next"),
        ],
        "root",
        "child",
    )
    assert [turn["status"] for turn in turns] == ["cancelled", "completed"]
    assert items[0] == observed
    assert items[1]["turn_id"] == "next"


def test_canonical_execution_replaces_legacy_receipt_and_keeps_model_records():
    legacy = event(
        "mcp_tool_call_end",
        call_id="fetch",
        invocation={"server": "catalog", "tool": "lookup", "arguments": {"key": "legacy"}},
        result={"Err": "legacy result"},
    )
    canonical = event(
        "item_completed",
        thread_id="root",
        item={
            "type": "McpToolCall",
            "id": "fetch",
            "server": "catalog",
            "tool": "lookup",
            "arguments": {"key": "effective"},
            "status": "failed",
            "error": {"message": "native failure"},
        },
    )
    raw_call = response(
        "function_call", id="provider-id", call_id="fetch", name="lookup", arguments='{"key":"raw"}'
    )
    raw_output = response("function_call_output", call_id="fetch", output="model-facing result")
    _, items = project_history(
        [event("task_started"), legacy, canonical, raw_call, raw_output, event("task_complete")],
        "root",
    )
    assert items == [
        project_execution_completion(canonical["payload"], thread_id="root", turn_id="t1"),
        {
            "type": "function_call",
            "id": "provider-id",
            "turn_id": "t1",
            "call_id": "fetch",
            "name": "lookup",
            "arguments": '{"key":"raw"}',
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fetch:output",
            "turn_id": "t1",
            "call_id": "fetch",
            "status": "completed",
            "output": "model-facing result",
            "error": None,
        },
    ]
    _, old = project_history([event("task_started"), legacy], "root", "child")
    assert old[0]["type"] == "mcp_call"
    assert old[0]["thread_id"] is None
    assert old[0]["native_status"] is None
    assert old[0]["error"] == {"message": "legacy result"}


def test_reconciliation_binds_to_the_original_turn_even_when_the_call_id_is_reused():
    def call(turn):
        return [
            event("task_started", turn),
            response("function_call", call_id="shared", name="write", arguments="{}"),
        ]

    def reconciliation(original_turn):
        return response(
            "function_call_output",
            call_id="shared",
            output="application-confirmed no effect",
            internal_chat_message_metadata_passthrough={
                "turn_id": original_turn,
                "content_item_kinds": ["sdk_reconciliation.not_applied"],
            },
        )

    _, items = project_history(
        [
            *call("original"),
            event("turn_aborted", "original"),
            *call("next"),
            reconciliation("unknown"),
            reconciliation("original"),
            response("function_call_output", call_id="shared", output="actual result"),
            event("task_complete", "next"),
        ],
        "root",
    )
    assert [(item["type"], item["turn_id"], item["status"]) for item in items] == [
        ("function_call", "original", "failed"),
        ("function_call", "next", "completed"),
        ("function_call_output", "original", "failed"),
        ("function_call_output", "next", "completed"),
    ]
    assert items[2]["type"] == "function_call_output"
    assert items[2]["reconciliation"] == "not_applied"
    assert items[2]["output"] == "application-confirmed no effect"
    assert "reconciliation" not in items[3]


def test_latest_sampled_context_receipt_survives_inherited_turn_completion():
    def receipt(model):
        return {
            "thread_id": "source",
            "model": model,
            "reasoning_effort": None,
            "instruction_revision": None,
            "developer_instructions_sha256": None,
            "instruction_files": [],
            "user_config_applied": False,
            "codex_revision": "revision",
            "codex_source_tree": "tree",
            "approval_policy": "never",
            "executors": [],
        }

    def context(turn, model):
        return {
            "type": "turn_context",
            "payload": {
                "turn_id": turn,
                "sdk_receipt": {"version": 1, **receipt(model)},
            },
        }

    history = [
        event("task_started"),
        context("t1", "first"),
        context("t1", "last"),
        {"type": "turn_context", "payload": {"turn_id": "t1"}},
        {"type": "turn_context", "payload": {"turn_id": "t1", "sdk_receipt": None}},
        event("task_complete"),
        context("t2", "context-only"),
    ]
    turns, _ = project_history(history, "fork", "child")
    assert turns[0]["status"] == "completed"
    assert turns[0]["receipt"] == {"turn_id": "t1", **receipt("last")}
    assert turns[1]["status"] == "in_progress"
    assert turns[1]["receipt"]["thread_id"] == "source"
    other_turn = turns[1]

    history.append(
        {
            "type": "turn_context",
            "payload": {"turn_id": "t1", "sdk_receipt": {"version": 2, "turn_id": "t2"}},
        }
    )
    turns, _ = project_history(history, "fork", "child")
    assert turns[0]["status"] == "completed"
    assert "receipt" not in turns[0]
    assert turns[1] == other_turn

    history.append(context("t1", "resampled"))
    turns, _ = project_history(history, "fork", "child")
    assert turns[0]["receipt"] == {"turn_id": "t1", **receipt("resampled")}


def test_child_call_attribution_and_rollback_remove_only_discarded_turns():
    history = [
        event("task_started", "kept"),
        event(
            "item_completed",
            "kept",
            item={
                "type": "SubAgentActivity",
                "id": "spawn",
                "kind": "started",
                "agent_thread_id": "leaf",
            },
        ),
        event("task_complete", "kept"),
        event("task_started", "discarded"),
        response(
            "message",
            id="discarded-message",
            role="assistant",
            content=[{"type": "output_text", "text": "Discard"}],
        ),
        event("task_complete", "discarded"),
        {
            "type": "event_msg",
            "payload": {"type": "thread_rolled_back", "num_turns": 1},
        },
        event("task_started", "next"),
        response(
            "message",
            id="next-message",
            role="assistant",
            content=[{"type": "output_text", "text": "Keep"}],
        ),
        event("task_complete", "next"),
    ]
    turns, items = project_history(history, "root", "child")
    assert [turn["id"] for turn in turns] == ["kept", "next"]
    assert [item["id"] for item in items] == ["spawn", "next-message"]
    assert items[0]["agent_id"] == "child"
    assert items[0]["subagent_id"] == "leaf"
