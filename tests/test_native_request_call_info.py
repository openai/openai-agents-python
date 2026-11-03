"""Exercise Python request construction, not native prompt production."""

from __future__ import annotations

import asyncio
import gc
from types import SimpleNamespace
from typing import Any
from weakref import ref

import pytest
from pydantic import BaseModel

from agents import (
    ActionRequested,
    ApprovalRequest,
    ElicitationRequest,
    FunctionTool,
    Interaction,
    InteractionExpiredError,
    Session,
)
from agents.manual import _CapturedManual, _question
from agents.turn import _root_turn


@pytest.mark.parametrize(
    ("message", "request_type"),
    [
        pytest.param(
            {
                "type": "exec_approval_request",
                "turn_id": "turn",
                "call_id": "shared",
                "approval_id": "subprocess",
                "kind": "command",
                "command": ["echo", "ok"],
            },
            ApprovalRequest,
            id="execve-is-not-an-originating-call",
        ),
        pytest.param(
            {
                "type": "apply_patch_approval_request",
                "turn_id": "turn",
                "call_id": "shared",
                "changes": {},
            },
            ApprovalRequest,
            id="patch-can-originate-in-exec",
        ),
        pytest.param(
            {
                "type": "elicitation_request",
                "id": "mcp_tool_call_approval_shared",
                "server_name": "server",
                "request": {
                    "mode": "form",
                    "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_title": "Display only"},
                },
            },
            ApprovalRequest,
            id="core-mcp-approval-omits-raw-tool",
        ),
        pytest.param(
            {
                "type": "elicitation_request",
                "id": "shared",
                "server_name": "server",
                "request": {
                    "mode": "form",
                    "_meta": {"tool_name": "not-an-origin", "callId": "shared", "role": "root"},
                    "requested_schema": {"type": "object", "properties": {}},
                },
            },
            ElicitationRequest,
            id="server-cannot-assert-call-origin",
        ),
        pytest.param(
            {
                "type": "elicitation_request",
                "id": "shared",
                "server_name": "server",
                "request": {
                    "mode": "url",
                    "url": "https://example.invalid/",
                    "elicitation_id": "shared",
                },
            },
            ElicitationRequest,
            id="verification-id-is-not-call-id",
        ),
    ],
)
async def test_optional_call_does_not_fabricate_or_confer_authority(
    message: dict[str, Any], request_type: type[ApprovalRequest] | type[ElicitationRequest]
) -> None:
    # Only the Python owner and issuer are needed. Never open a native session.
    owner = object.__new__(Session)
    owner._id = "session"
    owner._owner = object()
    # This construction-only fixture has no tracked native URL responder.
    owner._native = SimpleNamespace(_check_elicitation_pending=lambda _server, _id: False)
    owner_ref = ref(owner)
    turn = _root_turn(owner, "turn", "submission", asyncio.get_running_loop().create_future())
    request: Interaction = _question(owner, message, turn)
    assert isinstance(request, request_type)
    if message.get("request", {}).get("mode") == "url":
        assert (request.session_id, request.turn_id, request.submission_id) == (
            owner.id,
            None,
            None,
        )
    else:
        assert (request.session_id, request.turn_id, request.submission_id) == (
            owner.id,
            turn.turn_id,
            turn.submission_id,
        )
    assert request.call is None
    del turn, owner
    gc.collect()
    assert owner_ref() is None
    assert request.call is None
    with pytest.raises(InteractionExpiredError, match="expired|answered"):
        if isinstance(request, ApprovalRequest):
            await request.respond("allow_once")
        else:
            assert isinstance(request, ElicitationRequest)
            await request.decline()


class Input(BaseModel):
    key: str


async def test_manual_identity_is_preserved_when_native_queue_ids_match() -> None:
    owner = object.__new__(Session)
    owner._id = "session"
    owner._owner = object()
    turn = _root_turn(owner, "turn", "submission", asyncio.get_running_loop().create_future())
    tool: FunctionTool[Input, str, Any] = FunctionTool(
        name="lookup", description="", input_type=Input, output_type=str
    )
    captured = tool._capture(None)
    assert isinstance(captured, _CapturedManual)
    manual: Interaction = captured.request(
        object(), 0, 0, owner, turn, "shared", Input(key="a"), None
    )
    native: Interaction = _question(
        owner,
        {
            "type": "exec_approval_request",
            "turn_id": "turn",
            "call_id": "shared",
            "kind": "command",
            "command": ["echo", "ok"],
        },
        turn,
    )
    assert isinstance(manual, ActionRequested)
    assert tool.match(manual) is manual and tool.match(native) is None
    assert manual.call.tool == tool.ref
    assert manual.call.model is None  # This synthetic SDK request has no live native model.
    assert (manual.call.session_id, manual.call.turn_id, manual.call.call_id) == (
        owner.id,
        turn.turn_id,
        "shared",
    )
    assert manual.call.child_id is manual.call.child_incarnation is None
    assert native.call is None
