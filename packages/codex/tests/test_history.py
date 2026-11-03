"""Historical projections preserve recorded identities and recorded ordering."""

import copy

import pytest

from agents._history import project_history


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


def test_specialized_native_call_replaces_generic_response_record():
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
    assert len(items) == 1
    assert items[0]["type"] == "command_execution"
    assert items[0]["output"] == "README.md"
    assert items[0]["exit_code"] == 0


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
