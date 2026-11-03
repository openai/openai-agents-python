import pytest

from agents._subagent_projection import project_subagent_item


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        (
            "spawn_agent",
            {
                "type": "create_subagent_call",
                "agent_id": "sender",
                "subagent_id": "child",
            },
        ),
        (
            "send_input",
            {
                "type": "send_subagent_input_call",
                "sender_agent_id": "sender",
                "recipient_agent_id": "child",
            },
        ),
        (
            "wait",
            {"type": "wait_for_subagents_call", "sender_agent_id": "sender"},
        ),
        *[
            (
                tool,
                {
                    "type": item_type,
                    "sender_agent_id": "sender",
                    "recipient_agent_id": "child",
                },
            )
            for tool, item_type in [
                ("resume_agent", "resume_subagent_call"),
                ("interrupt_agent", "interrupt_subagent_call"),
                ("close_agent", "close_subagent_call"),
            ]
        ],
    ],
)
def test_collaborator_call_preserves_identity_in_public_contract(tool, expected):
    native = {
        "type": "CollabAgentToolCall",
        "id": "call",
        "tool": tool,
        "status": "completed",
        "sender_thread_id": "sender",
        "receiver_thread_ids": ["child"],
        "prompt": "Investigate the failure",
        "model": "model",
        "reasoning_effort": "high",
    }
    assert project_subagent_item(native, True, "turn", "root") == {
        "id": "call",
        "turn_id": "turn",
        "status": "completed",
        **expected,
    }


@pytest.mark.parametrize(
    ("kind", "item_type", "identities"),
    [
        (
            "started",
            "create_subagent_call",
            {"agent_id": "parent", "subagent_id": "child"},
        ),
        (
            "interacted",
            "send_subagent_input_call",
            {"sender_agent_id": "parent", "recipient_agent_id": "child"},
        ),
        (
            "interrupted",
            "interrupt_subagent_call",
            {"sender_agent_id": "parent", "recipient_agent_id": "child"},
        ),
    ],
)
def test_activity_lifecycle_uses_observed_parent_and_operation(kind, item_type, identities):
    native = {
        "type": "SubAgentActivity",
        "id": "activity",
        "kind": kind,
        "agent_thread_id": "child",
        "agent_path": "/parent/child",
    }
    for done, status in [(False, "in_progress"), (True, "completed")]:
        assert project_subagent_item(native, done, "turn", "parent") == {
            "id": "activity",
            "turn_id": "turn",
            "type": item_type,
            "status": status,
            **identities,
        }


@pytest.mark.parametrize(
    ("native_status", "status"),
    [
        ("in_progress", "in_progress"),
        ("failed", "failed"),
        ("interrupted", "incomplete"),
    ],
)
def test_unsuccessful_spawn_keeps_call_without_inventing_child(native_status, status):
    native = {
        "type": "CollabAgentToolCall",
        "id": "spawn",
        "tool": "spawn_agent",
        "status": native_status,
        "sender_thread_id": "parent",
        "receiver_thread_ids": [],
    }
    assert project_subagent_item(native, native_status != "in_progress", "turn", "root") == {
        "id": "spawn",
        "turn_id": "turn",
        "type": "create_subagent_call",
        "status": status,
        "agent_id": "parent",
        "subagent_id": None,
    }


def test_targetless_send_failure_cannot_be_a_public_recipient_call():
    native = {
        "type": "CollabAgentToolCall",
        "id": "send",
        "tool": "send_input",
        "status": "failed",
        "sender_thread_id": "parent",
        "receiver_thread_ids": [],
    }
    assert project_subagent_item(native, True, "turn", "root") is None
