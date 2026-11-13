"""Local recovery selection uses retained identities before native validation."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents import LocalStorage, ReconciledCall, Session


def row(kind, **payload):
    return {"type": kind, "payload": payload}


def started(turn, root):
    return row("event_msg", type="task_started", turn_id=turn, root_turn_id=root)


def activity(parent, turn, child, kind, call):
    return row(
        "event_msg",
        type="item_completed",
        thread_id=parent,
        turn_id=turn,
        item={
            "type": "SubAgentActivity",
            "id": call,
            "kind": kind,
            "agent_thread_id": child,
            "agent_path": f"/root/{child}",
        },
    )


@pytest.mark.parametrize(
    "origin", ["automatic", "supplied", "retained", "older-retained", "root-retained"]
)
@pytest.mark.parametrize("followup", [False, True])
@pytest.mark.parametrize("nested", [False, True])
async def test_local_recovery_keeps_receipt_lineage_separate_from_automatic_sibling(
    tmp_path, origin, followup, nested
):
    root = [started("root-turn", "root-turn")]
    if followup:
        root = [
            started("spawn-turn", "spawn-turn"),
            activity("root", "spawn-turn", "child", "started", "spawn"),
            row("event_msg", type="task_complete", turn_id="spawn-turn"),
            *root,
            row(
                "response_item",
                type="function_call",
                namespace="collaboration",
                name="followup_task",
                call_id="follow",
                arguments="{}",
            ),
            row("response_item", type="function_call_output", call_id="follow", output="accepted"),
            activity("root", "root-turn", "child", "interacted", "follow"),
        ]
    else:
        root.append(activity("root", "root-turn", "child", "started", "spawn"))
    root.append(activity("root", "root-turn", "sibling", "started", "sibling-spawn"))
    child = [started("child-turn", "root-turn")]
    if origin == "root-retained":
        root.extend(
            [
                row(
                    "response_item",
                    type="function_call",
                    name="record",
                    call_id="root-effect",
                    arguments="{}",
                ),
                row(
                    "response_item",
                    type="function_call_output",
                    call_id="root-effect",
                    output="confirmed",
                    internal_chat_message_metadata_passthrough={
                        "turn_id": "root-turn",
                        "content_item_kinds": ["sdk_reconciliation.applied"],
                    },
                ),
            ]
        )
    elif origin != "automatic":
        receipt_turn = "older" if origin == "older-retained" else "child-turn"
        receipt_history = [
            started(receipt_turn, "root-turn"),
            row(
                "response_item",
                type="function_call",
                name="record",
                call_id="effect",
                arguments="{}",
            ),
        ]
        if origin != "supplied":
            receipt_history.append(
                row(
                    "response_item",
                    type="function_call_output",
                    call_id="effect",
                    output="confirmed",
                    internal_chat_message_metadata_passthrough={
                        "turn_id": receipt_turn,
                        "content_item_kinds": ["sdk_reconciliation.applied"],
                    },
                )
            )
        if origin == "older-retained":
            receipt_history.append(row("event_msg", type="task_complete", turn_id=receipt_turn))
            child = [*receipt_history, *child]
        else:
            child = receipt_history
    effect_thread = "leaf" if nested else "child"
    histories = {
        "root": root,
        effect_thread: child,
        "sibling": [started("sibling-turn", "root-turn")],
    }
    if nested:
        histories["child"] = [
            started("parent-turn", "root-turn"),
            activity("child", "parent-turn", "leaf", "started", "spawn-leaf"),
        ]
    descendants = [
        {"id": child, "parent_agent_id": "root", "status": "open", "runtime_loaded": False}
        for child in ("child", "sibling")
    ]
    if nested:
        descendants.append(
            {"id": "leaf", "parent_agent_id": "child", "status": "open", "runtime_loaded": False}
        )
    native = SimpleNamespace(
        call=AsyncMock(return_value=json.dumps(descendants)),
        read_thread_history=AsyncMock(
            side_effect=lambda child, **_: json.dumps(histories[child or "root"])
        ),
        _reconcile_local_calls=AsyncMock(),
    )

    async def publish(operation):
        return await operation

    owner = SimpleNamespace(
        id="root", _native=native, _storage=LocalStorage(tmp_path), _publish_startup=publish
    )
    receipts = (
        [
            ReconciledCall(
                thread_id=effect_thread,
                turn_id="child-turn",
                call_id="effect",
                outcome="applied",
                result="confirmed",
            )
        ]
        if origin == "supplied"
        else []
    )
    await Session._reconcile_local_history(owner, receipts)
    request = json.loads(native._reconcile_local_calls.call_args.args[0])
    automatic = origin in ("automatic", "older-retained", "root-retained")
    assert request["histories"][effect_thread]["interrupted"] == ["child-turn"]
    assert request["histories"][effect_thread]["automatic_turns"] == (
        ["child-turn"] if automatic else []
    )
    if nested:
        assert request["histories"]["child"]["automatic_turns"] == (
            ["parent-turn"] if automatic else []
        )
    assert request["histories"]["sibling"]["automatic_turns"] == ["sibling-turn"]
    assert request["automatic_root_turns"] == (
        ["root-turn"] if (automatic or followup) and origin != "root-retained" else []
    )
