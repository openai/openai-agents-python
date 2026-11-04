"""Project canonical rollout records without synthesizing model output."""

from dataclasses import dataclass
from typing import Any, cast

from ._events import FunctionCallItem, SessionItem
from ._projection import EventProjector, _execution_item, _usage, project_execution_completion
from ._web_projection import project_web_search_item
from .history import RecordedTurn
from .recovery import UncertainCall, _uncertain_call
from .turn_receipt import _project_turn_receipt


@dataclass
class _HistoryItem:
    value: dict[str, Any]
    native: bool
    recorded_id: bool
    output: "_HistoryItem | None" = None
    custom_input: str | None = None


_EXECUTION_TYPES = {"command_execution", "mcp_call", "patch_execution"}


def _legacy_execution(message: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    # These legacy end events have no native thread ID. Never borrow the
    # reader's ID for an inherited record.
    if message["type"] == "patch_apply_end":
        native = {
            "type": "FileChange",
            "id": message.get("call_id"),
            "changes": message.get("changes", {}),
            "stdout": message.get("stdout"),
            "stderr": message.get("stderr"),
            "status": message.get("status"),
        }
    elif message["type"] == "mcp_tool_call_end":
        invocation = message["invocation"]
        result = message["result"].get("Ok")
        error = message["result"].get("Err")
        native = {
            "type": "McpToolCall",
            "id": message.get("call_id"),
            "server": invocation["server"],
            "tool": invocation["tool"],
            "arguments": invocation.get("arguments"),
            "result": result,
            "error": {"message": error} if error is not None else None,
            "status": (
                "completed"
                if isinstance(result, dict) and result.get("isError") is not True
                else "failed"
            ),
        }
    else:
        return None
    item = _execution_item(native, None, turn_id)
    if item is not None and message["type"] == "mcp_tool_call_end":
        item["native_status"] = None
    return item


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
    history: list[dict[str, Any]],
    session_id: str,
    subagent_id: str | None = None,
    *,
    uncertain_calls: list[UncertainCall] | None = None,
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

    def remember(
        item: dict[str, Any],
        *,
        native: bool,
        recorded_id: bool = True,
        custom_input: str | None = None,
    ) -> None:
        kind = item["type"]
        is_output = kind == "function_call_output"
        is_execution = kind in _EXECUTION_TYPES
        # Execution records describe what ran. Keep them separate from the model's call and output.
        identity = (
            item["turn_id"],
            kind if is_output or is_execution else "item",
            item["id"],
        )
        occurrence = identities.get(identity)
        call_key = (item["turn_id"], item.get("call_id", item["id"]))
        call = None if is_execution else latest_calls.get(call_key)
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
        if kind == "function_call":
            if not native:
                occurrence.custom_input = custom_input
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
            receipt = _project_turn_receipt(payload)
            if receipt is not None:
                receipt_turn_id = receipt["turn_id"]
                turn: RecordedTurn | None = turns.get(receipt_turn_id)
                if turn is None:
                    turn = {
                        "id": receipt_turn_id,
                        "object": "agent.session.turn",
                        "session_id": session_id,
                        "subagent_id": subagent_id,
                        "status": "in_progress",
                        "created_at": None,
                        "started_at": None,
                        "completed_at": None,
                        "error": None,
                        "usage": None,
                    }
                    turns[receipt_turn_id] = turn
                turn["receipt"] = receipt
            elif payload.get("sdk_receipt") is not None:
                # A newer sample supersedes the earlier receipt even if we cannot read it.
                receipt_turn_id = payload.get("turn_id")
                if isinstance(receipt_turn_id, str) and receipt_turn_id in turns:
                    turns[receipt_turn_id].pop("receipt", None)
        elif kind == "token_usage_record":
            usage[payload["turn_id"]] = _usage(payload["turn_token_usage"])
        elif kind == "response_item":
            turn_id = current_turn
            reconciliation = None
            if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                metadata = payload.get("internal_chat_message_metadata_passthrough")
                if isinstance(metadata, dict):
                    original_turn = metadata.get("turn_id")
                    call_id = payload.get("call_id")
                    kinds = metadata.get("content_item_kinds")
                    if kinds == ["sdk_reconciliation.applied"]:
                        reconciliation = "applied"
                    elif kinds == ["sdk_reconciliation.not_applied"]:
                        reconciliation = "not_applied"
                    if reconciliation is not None:
                        if (original_turn, call_id) not in latest_calls:
                            continue
                        turn_id = original_turn
                    elif (
                        turn_id is None
                        and aborted_turn is not None
                        and original_turn == aborted_turn
                        and (aborted_turn, call_id) in latest_calls
                    ):
                        turn_id = aborted_turn
            if turn_id:
                item = _response_item(payload, turn_id)
                if item is not None:
                    if reconciliation is not None:
                        item["reconciliation"] = reconciliation
                        if reconciliation == "not_applied":
                            item["status"] = "failed"
                    remember(
                        item,
                        native=False,
                        recorded_id=bool(payload.get("id")),
                        custom_input=(
                            payload.get("input")
                            if uncertain_calls is not None and payload["type"] == "custom_tool_call"
                            else None
                        ),
                    )
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
            # The live projector can create a fallback final message. History
            # must contain only recorded items, with their original identities.
            message = {**payload, "turn_id": turn_id}
            execution = cast(
                dict[str, Any] | None,
                project_execution_completion(
                    message, thread_id=message.get("thread_id"), turn_id=turn_id
                ),
            ) or _legacy_execution(message, turn_id)
            if execution is not None:
                # Background commands may complete after their owning turn
                # has finished. An execution receipt cannot reopen that turn
                # or change the owner of subsequent raw response items.
                remember(execution, native=event_kind == "item_completed")
                continue
            current_turn = turn_id
            aborted_turn = None
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
                    if previous is not None and "receipt" in previous:
                        turn["receipt"] = previous["receipt"]
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
    # Reserve recorded model IDs first. An execution row keeps its native ID in call_id
    # when that ID is already used by another row in this snapshot.
    cursor_ids = {
        occurrence.value["id"]
        for occurrence in items
        if occurrence.recorded_id and occurrence.value["type"] not in _EXECUTION_TYPES
    }
    for occurrence in items:
        item = occurrence.value
        if item["type"] in _EXECUTION_TYPES:
            item_id = item["id"]
            occurrence.recorded_id = item_id not in cursor_ids
            cursor_ids.add(item_id)
    for index, occurrence in enumerate(items):
        item = occurrence.value
        if not occurrence.recorded_id:
            item_id = item["id"]
            while item_id in cursor_ids:
                item_id = f"{item_id}:{index}"
            item["id"] = item_id
            cursor_ids.add(item_id)
        if item["type"] == "function_call":
            output = occurrence.output
            if not occurrence.native:
                recorded_turn = turns.get(item["turn_id"])
                terminal = recorded_turn["status"] if recorded_turn is not None else None
                item["status"] = (
                    output.value["status"]
                    if output
                    else "incomplete"
                    if terminal and terminal != "in_progress"
                    else "in_progress"
                )
            elif (
                item["status"] in ("in_progress", "incomplete")
                and output is not None
                and "reconciliation" in output.value
            ):
                item["status"] = output.value["status"]
        if (
            uncertain_calls is not None
            and item["type"] == "function_call"
            and item["status"] in ("in_progress", "incomplete")
        ):
            uncertain_calls.append(
                _uncertain_call(
                    subagent_id if subagent_id is not None else session_id,
                    cast(FunctionCallItem, item),
                    custom_input=occurrence.custom_input,
                )
            )
    return list(turns.values()), cast(list[SessionItem], [item.value for item in items])
