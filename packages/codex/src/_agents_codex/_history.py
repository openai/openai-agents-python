"""Project canonical rollout records without synthesizing model output."""

import json as json
from dataclasses import dataclass as dataclass
from typing import Any as Any, cast as cast

from pydantic import JsonValue as JsonValue

from ._events import FunctionCallItem as FunctionCallItem, SessionItem as SessionItem
from ._projection import (
    EventProjector as EventProjector,
    _execution_item as _execution_item,
    _usage as _usage,
    project_execution_completion as project_execution_completion,
    project_hosted_response_item,
)
from ._public import preserve_module_identity as _preserve_module_identity
from ._web_projection import project_web_search_item as project_web_search_item
from .history import (
    ReceivedTokenUsage as ReceivedTokenUsage,
    RecordedResponse as RecordedResponse,
    RecordedTurn as RecordedTurn,
)
from .recovery import UncertainCall as UncertainCall, _uncertain_call as _uncertain_call
from .response_facts import RecordedResponseFact, _response_fact_record
from .turn_receipt import _project_turn_receipt as _project_turn_receipt


@dataclass
class _HistoryItem:
    value: dict[str, Any]
    native: bool
    recorded_id: bool
    output: "_HistoryItem | None" = None
    original_input: JsonValue | None = None
    user_alias: bool = False


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
    if kind in {"function_call", "custom_tool_call", "computer_call"}:
        item_id = item_id or raw.get("call_id")
    elif kind in {"function_call_output", "custom_tool_call_output", "computer_call_output"}:
        item_id = item_id or (f"{raw['call_id']}:output" if raw.get("call_id") else None)
    if not item_id:
        return None
    common = {"id": item_id, "turn_id": turn_id, "type": kind}
    if kind == "message" and raw.get("role") in {"user", "assistant"}:
        role = raw["role"]
        accepted = (
            {"output_text"} if role == "assistant" else {"input_text", "input_image", "input_file"}
        )
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
    if kind in {"function_call", "custom_tool_call", "computer_call"}:
        name = "computer" if kind == "computer_call" else raw["name"]
        if raw.get("namespace"):
            name = f"{raw['namespace']}.{name}"
        return {
            **common,
            "type": "function_call",
            "call_id": raw["call_id"],
            "name": name,
            "arguments": raw if kind == "computer_call" else raw.get("arguments", raw.get("input")),
            "status": "in_progress",
        }
    if kind in {
        "function_call_output",
        "custom_tool_call_output",
        "computer_call_output",
    } and raw.get("call_id"):
        return {
            **common,
            "type": "function_call_output",
            "call_id": raw["call_id"],
            "status": "completed",
            "output": (
                json.dumps(
                    {
                        "output": raw["output"],
                        "acknowledged_safety_checks": raw.get("acknowledged_safety_checks", []),
                    },
                    allow_nan=False,
                )
                if kind == "computer_call_output"
                else raw["output"]
            ),
            "error": None,
        }
    if kind == "web_search_call":
        return project_web_search_item({**raw, "type": "WebSearch"}, True, turn_id)
    return project_hosted_response_item(raw, turn_id)


def _received_usage(raw: dict[str, Any] | None) -> ReceivedTokenUsage | None:
    if raw is None:
        return None
    usage: dict[str, Any] = {
        key: raw[key]
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if raw.get(key) is not None
    }
    input_details = {
        public: raw[native]
        for native, public in (
            ("cached_input_tokens", "cached_tokens"),
            ("cache_write_input_tokens", "cache_write_tokens"),
        )
        if raw.get(native) is not None
    }
    if input_details:
        usage["input_tokens_details"] = input_details
    if raw.get("reasoning_output_tokens") is not None:
        usage["output_tokens_details"] = {"reasoning_tokens": raw["reasoning_output_tokens"]}
    return cast(ReceivedTokenUsage, usage)


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
    responses: list[tuple[str, RecordedResponse]] = []
    response_facts: list[RecordedResponseFact] = []
    current_turn: str | None = None
    aborted_turn: str | None = None
    turn_order: list[str] = []
    pending_user: _HistoryItem | None = None

    def remember(
        item: dict[str, Any],
        *,
        native: bool,
        recorded_id: bool = True,
        original_input: JsonValue | None = None,
    ) -> _HistoryItem:
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
                occurrence.original_input = original_input
            latest_calls[call_key] = occurrence
        elif is_output and call is not None:
            call.output = occurrence
        return occurrence

    for record in history:
        kind, payload = record["type"], record["payload"]
        if kind in {"compacted", "session_meta"}:
            pending_user = None
        if kind == "turn_context":
            if payload.get("turn_id") != current_turn:
                pending_user = None
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
        elif kind == "response_fact_record":
            fact = _response_fact_record(payload)
            if fact is not None:
                response_facts.append(fact)
        elif kind == "response_observation_record":
            response: RecordedResponse = {
                "id": payload["id"],
                "thread_id": payload["thread_id"],
                "turn_id": payload["turn_id"],
                "response_id": payload.get("response_id"),
                "status": payload["status"],
                "usage_source": "received",
                "usage": _received_usage(payload.get("usage")),
            }
            if payload.get("incomplete_reason") is not None:
                response["incomplete_reason"] = payload["incomplete_reason"]
            if isinstance(payload.get("upstream_request_id"), str):
                response["upstream_request_id"] = payload["upstream_request_id"]
            responses.append((kind, response))
        elif kind == "token_usage_record":
            usage[payload["turn_id"]] = _usage(payload["turn_token_usage"])
            if payload.get("thread_id") is not None:
                responses.append(
                    (
                        kind,
                        {
                            "id": payload.get("response_observation_id"),
                            "thread_id": payload["thread_id"],
                            "turn_id": payload["turn_id"],
                            "response_id": payload.get("response_id"),
                            "usage_source": "native_normalized",
                            "usage": _usage(payload["usage"]),
                        },
                    )
                )
        elif kind == "response_item":
            turn_id = current_turn
            reconciliation = None
            if payload.get("type") in {
                "function_call_output",
                "custom_tool_call_output",
                "computer_call_output",
            }:
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
                    occurrence = remember(
                        item,
                        native=False,
                        recorded_id=bool(payload.get("id")),
                        original_input=(
                            (
                                payload
                                if payload["type"] == "computer_call"
                                else payload.get("input")
                            )
                            if uncertain_calls is not None
                            and payload["type"] in {"custom_tool_call", "computer_call"}
                            else None
                        ),
                    )
                    metadata = payload.get("internal_chat_message_metadata_passthrough")
                    recorded_metadata = record.get("metadata")
                    input_order = (
                        recorded_metadata.get("user_input_order")
                        if isinstance(recorded_metadata, dict)
                        else None
                    )
                    kinds = (
                        metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
                    )
                    if (
                        item["type"] == "message"
                        and item["role"] == "user"
                        and not occurrence.native
                        and isinstance(metadata, dict)
                        and metadata.get("turn_id") == turn_id
                        and type(input_order) is int
                        and input_order >= 0
                        and isinstance(kinds, list)
                        and len(kinds) > 0
                        and len(kinds) == len(payload["content"])
                        and all(
                            value
                            in {"user.text", "user.image", "user.file", "images.preparation_error"}
                            for value in kinds
                        )
                    ):
                        pending_user = occurrence
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
                    responses = [entry for entry in responses if entry[1]["turn_id"] not in removed]
                    items = [item for item in items if item.value["turn_id"] not in removed]
                    identities = {
                        key: item for key, item in identities.items() if key[0] not in removed
                    }
                    latest_calls = {
                        key: item for key, item in latest_calls.items() if key[0] not in removed
                    }
                current_turn = None
                aborted_turn = None
                pending_user = None
                projector = EventProjector(session_id, source_id=subagent_id)
                continue
            turn_id = payload.get("turn_id") or payload.get("turnId") or current_turn
            if not turn_id:
                continue
            if pending_user is not None and pending_user.value["turn_id"] != turn_id:
                pending_user = None
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
            if native_item.get("type") == "ContextCompaction":
                pending_user = None
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
                    occurrence = remember(item, native=True)
                    if native_item.get("type") == "UserMessage":
                        # Only the first native event for an input reaches this
                        # projector branch. Pair by acceptance provenance and
                        # record order, never by equal message content.
                        if pending_user is not None and occurrence is not pending_user:
                            pending_user.user_alias = True
                        pending_user = None
            if event_kind in {"task_started", "turn_started"}:
                pending_user = None
            if event_kind in {"task_complete", "turn_complete", "turn_aborted"}:
                current_turn = None
                pending_user = None
                if event_kind == "turn_aborted":
                    aborted_turn = turn_id
    for turn_id, turn in turns.items():
        if turn_id in usage:
            turn["usage"] = usage[turn_id]
    projected_fact_ids: set[str] = set()
    for fact in response_facts:
        fact_turn = turns.get(fact["turn_id"])
        if fact_turn is not None and fact["id"] not in projected_fact_ids:
            fact_turn.setdefault("response_facts", []).append(fact)
            projected_fact_ids.add(fact["id"])
    observed_ids = {
        response["id"] for kind, response in responses if kind == "response_observation_record"
    }
    projected_ids: set[str] = set()
    accounted_observations = {
        (response["thread_id"], response["turn_id"], response["id"])
        for kind, response in responses
        if kind == "token_usage_record" and response["id"] is not None
    }
    for kind, response in responses:
        observation_id = response["id"]
        if observation_id is not None:
            if kind == "token_usage_record" and observation_id in observed_ids:
                continue
            if observation_id in projected_ids:
                continue
            projected_ids.add(observation_id)
        response_turn = turns.get(response["turn_id"])
        if response_turn is not None:
            response_turn.setdefault("responses", []).append(response)
            # Native accounting fills omitted cache writes with zero. Suppress
            # only this optional aggregate when an exact contributor proves
            # that its received count is unknown.
            if (
                kind == "response_observation_record"
                and (response["thread_id"], response["turn_id"], observation_id)
                in accounted_observations
                and (response.get("usage") or {})
                .get("input_tokens_details", {})
                .get("cache_write_tokens")
                is None
                and response_turn["usage"] is not None
            ):
                response_turn["usage"]["input_tokens_details"].pop("cache_write_tokens", None)
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
                    original_input=occurrence.original_input,
                )
            )
    return list(turns.values()), cast(
        list[SessionItem], [item.value for item in items if not item.user_alias]
    )


_preserve_module_identity(globals(), "agents._history")
