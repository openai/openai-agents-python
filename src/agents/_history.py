"""Project canonical rollout records without synthesizing model output."""

from dataclasses import dataclass
from typing import Any, cast

from ._events import SessionItem
from ._projection import EventProjector, _usage
from ._web_projection import project_web_search_item
from .history import RecordedTurn


@dataclass
class _HistoryItem:
    value: dict[str, Any]
    native: bool
    recorded_id: bool
    output: "_HistoryItem | None" = None


def _response_item(raw: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    kind = raw["type"]
    item_id = raw.get("id")
    if kind in {"function_call", "custom_tool_call"}:
        item_id = item_id or raw.get("call_id")
    elif kind in {"function_call_output", "custom_tool_call_output"}:
        item_id = item_id or (f"{raw['call_id']}:output" if raw.get("call_id") else None)
    if not item_id:
        return None
    common = {"id": item_id, "turn_id": turn_id, "type": kind}
    if kind == "message" and raw.get("role") in {"user", "assistant"}:
        role = raw["role"]
        accepted = {"output_text"} if role == "assistant" else {"input_text", "input_image"}
        content = [part for part in raw["content"] if part["type"] in accepted]
        return {
            **common,
            "role": role,
            "status": "completed",
            "phase": raw.get("phase") if role == "assistant" else None,
            "content": content,
        }
    if kind == "reasoning":
        return {**common, "summary": raw["summary"], "status": None}
    if kind in {"function_call", "custom_tool_call"}:
        name = raw["name"]
        if raw.get("namespace"):
            name = f"{raw['namespace']}.{name}"
        return {
            **common,
            "type": "function_call",
            "call_id": raw["call_id"],
            "name": name,
            "arguments": raw.get("arguments", raw.get("input")),
            "status": "in_progress",
        }
    if kind in {"function_call_output", "custom_tool_call_output"} and raw.get("call_id"):
        return {
            **common,
            "type": "function_call_output",
            "call_id": raw["call_id"],
            "status": "completed",
            "output": raw["output"],
            "error": None,
        }
    if kind == "web_search_call":
        return project_web_search_item({**raw, "type": "WebSearch"}, True, turn_id)
    return None


def project_history(
    history: list[dict[str, Any]], session_id: str, subagent_id: str | None = None
) -> tuple[list[RecordedTurn], list[SessionItem]]:
    """Prefer native item events over raw records, retaining first-seen order."""
    projector = EventProjector(session_id, source_id=subagent_id)
    turns: dict[str, RecordedTurn] = {}
    items: list[_HistoryItem] = []
    identities: dict[tuple[str, str, str], _HistoryItem] = {}
    latest_calls: dict[tuple[str, str], _HistoryItem] = {}
    usage: dict[str, Any] = {}
    current_turn: str | None = None
    aborted_turn: str | None = None
    turn_order: list[str] = []

    def remember(item: dict[str, Any], *, native: bool, recorded_id: bool = True) -> None:
        is_output = item["type"] == "function_call_output"
        identity = (
            item["turn_id"],
            "output" if is_output else "item",
            item["id"],
        )
        occurrence = identities.get(identity)
        call_key = (item["turn_id"], item.get("call_id", item["id"]))
        call = latest_calls.get(call_key)
        if is_output and call is not None:
            if call.output is None:
                occurrence = None
            elif occurrence is call.output or native != call.output.native:
                occurrence = call.output
            else:
                occurrence = None
        elif native and call is not None and not call.native:
            # Native tool events use the call ID as their item ID. Enrich the
            # latest matching raw call without changing its first-seen position.
            occurrence = call
        elif not native and not recorded_id and call is not None and call.output is not None:
            # With no distinct item ID, a completed call/output pair marks the
            # boundary between occurrences that reuse the same call ID.
            occurrence = None
        if occurrence is None:
            occurrence = _HistoryItem(item, native, recorded_id)
            items.append(occurrence)
        elif native or not occurrence.native:
            occurrence.value = item
            occurrence.native = native
            occurrence.recorded_id = recorded_id
        identities[identity] = occurrence
        if item["type"] == "function_call":
            latest_calls[call_key] = occurrence
        elif is_output and call is not None:
            call.output = occurrence

    for record in history:
        kind, payload = record["type"], record["payload"]
        if kind == "turn_context":
            current_turn = payload.get("turn_id") or current_turn
            aborted_turn = None
            if current_turn and current_turn not in turn_order:
                turn_order.append(current_turn)
        elif kind == "token_usage_record":
            usage[payload["turn_id"]] = _usage(payload["turn_token_usage"])
        elif kind == "response_item":
            turn_id = current_turn
            if turn_id is None and aborted_turn is not None:
                metadata = payload.get("internal_chat_message_metadata_passthrough")
                if (
                    payload.get("type") in {"function_call_output", "custom_tool_call_output"}
                    and isinstance(metadata, dict)
                    and metadata.get("turn_id") == aborted_turn
                    and (aborted_turn, payload.get("call_id")) in latest_calls
                ):
                    turn_id = aborted_turn
            if turn_id:
                item = _response_item(payload, turn_id)
                if item is not None:
                    remember(item, native=False, recorded_id=bool(payload.get("id")))
        elif kind == "event_msg":
            event_kind = payload["type"]
            if event_kind == "thread_rolled_back":
                count = payload["num_turns"]
                if count:
                    removed = set(turn_order[-count:])
                    del turn_order[-count:]
                    for old in removed:
                        turns.pop(old, None)
                        usage.pop(old, None)
                    items = [item for item in items if item.value["turn_id"] not in removed]
                    identities = {
                        key: item for key, item in identities.items() if key[0] not in removed
                    }
                    latest_calls = {
                        key: item for key, item in latest_calls.items() if key[0] not in removed
                    }
                current_turn = None
                aborted_turn = None
                projector = EventProjector(session_id, source_id=subagent_id)
                continue
            turn_id = payload.get("turn_id") or payload.get("turnId") or current_turn
            if not turn_id:
                continue
            if turn_id not in turn_order:
                turn_order.append(turn_id)
            current_turn = turn_id
            aborted_turn = None
            # The live projector can create a fallback final message. History
            # must contain only recorded items, with their original identities.
            message = {**payload, "turn_id": turn_id}
            message.pop("last_agent_message", None)
            native_item = payload.get("item", {})
            for event in projector.project({"id": turn_id, "msg": message}):
                if (
                    event["type"] == "agent.session.turn.in_progress"
                    or event["type"] == "agent.session.turn.completed"
                    or event["type"] == "agent.session.turn.failed"
                    or event["type"] == "agent.session.turn.cancelled"
                ):
                    turn = cast(RecordedTurn, event["turn"])
                    previous = turns.get(turn_id)
                    started = payload.get("started_at")
                    if started is None and previous:
                        started = previous["started_at"]
                    turn["subagent_id"] = subagent_id
                    turn["created_at"] = started
                    turn["started_at"] = started
                    turn["completed_at"] = payload.get("completed_at")
                    turns[turn_id] = turn
                elif (
                    event["type"] == "agent.session.turn.item.added"
                    or event["type"] == "agent.session.turn.item.done"
                ):
                    item = cast(dict[str, Any], event["item"])
                    if (
                        item["type"] == "function_call_output"
                        and native_item.get("content_items") is None
                    ):
                        continue
                    remember(item, native=True)
            if event_kind in {"task_complete", "turn_complete", "turn_aborted"}:
                current_turn = None
                if event_kind == "turn_aborted":
                    aborted_turn = turn_id
    for turn_id, turn in turns.items():
        if turn_id in usage:
            turn["usage"] = usage[turn_id]
    # Reserve recorded IDs before assigning distinct cursors to synthetic occurrences.
    cursor_ids = {occurrence.value["id"] for occurrence in items if occurrence.recorded_id}
    for index, occurrence in enumerate(items):
        item = occurrence.value
        if not occurrence.recorded_id:
            item_id = item["id"]
            while item_id in cursor_ids:
                item_id = f"{item_id}:{index}"
            item["id"] = item_id
            cursor_ids.add(item_id)
        if item["type"] == "function_call" and not occurrence.native:
            output = occurrence.output
            recorded_turn = turns.get(item["turn_id"])
            terminal = recorded_turn["status"] if recorded_turn is not None else None
            item["status"] = (
                output.value["status"]
                if output
                else "incomplete"
                if terminal and terminal != "in_progress"
                else "in_progress"
            )
    return list(turns.values()), cast(list[SessionItem], [item.value for item in items])
