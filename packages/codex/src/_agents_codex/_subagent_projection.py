"""Project native subagent calls as Managed Agents items."""

from typing import Any as Any

from ._public import preserve_module_identity as _preserve_module_identity


def project_subagent_item(
    native: dict[str, Any],
    done: bool,
    turn_id: str | None,
    session_id: str,
) -> dict[str, Any] | None:
    kind = native["type"]
    if kind == "SubAgentActivity":
        tool = {
            "started": "spawn_agent",
            "interacted": "send_input",
            "interrupted": "interrupt_agent",
        }.get(native["kind"])
        if tool is None:
            return None
        sender = session_id
        receivers = [native["agent_thread_id"]]
        status = "completed" if done else "in_progress"
    elif kind == "CollabAgentToolCall":
        tool = native["tool"]
        sender = native["sender_thread_id"]
        receivers = native.get("receiver_thread_ids", [])
        status = native["status"]
        if status == "interrupted":
            status = "incomplete"
    else:
        return None

    common = {"id": native["id"], "turn_id": turn_id, "status": status}
    if tool == "spawn_agent":
        return {
            **common,
            "type": "create_subagent_call",
            "agent_id": sender,
            "subagent_id": receivers[0] if receivers else None,
        }
    if tool == "wait":
        return {
            **common,
            "type": "wait_for_subagents_call",
            "sender_agent_id": sender,
        }
    item_type = {
        "send_input": "send_subagent_input_call",
        "resume_agent": "resume_subagent_call",
        "interrupt_agent": "interrupt_subagent_call",
        "close_agent": "close_subagent_call",
    }.get(tool)
    if item_type is None or len(receivers) != 1:
        return None
    return {
        **common,
        "type": item_type,
        "sender_agent_id": sender,
        "recipient_agent_id": receivers[0],
    }


_preserve_module_identity(globals(), "agents._subagent_projection")
