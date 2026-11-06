"""Safe metadata bridge and existing passive-buffer behavior, without model calls."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from _agents_codex.observation import _ObservationBuffer, _request_observation
from agents.observation import (
    EventGap,
    EventStream,
    ExecutionItemCompleted,
    ModelRequestDispatching,
    ModelResponseLinked,
)


def payload(effort: Any = None) -> dict[str, Any]:
    return {
        "type": "model_request_dispatching",
        "request_id": "request",
        "thread_id": "thread",
        "turn_id": "turn",
        "model": "resolved-model",
        "reasoning_effort": effort,
        "reasoning_summary": None,
        "service_tier": None,
        "wire": "responses",
        "transport": "http_sse",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ({"kind": "text", "value": "future-effort"}, "future-effort"),
        ({"kind": "text", "value": "18446744073709551616"}, "18446744073709551616"),
        ({"kind": "integer", "decimal": "9007199254740993"}, 9007199254740993),
        ({"kind": "integer", "decimal": "18446744073709551615"}, 18446744073709551615),
        ({"kind": "integer", "decimal": "0"}, 0),
    ],
)
def test_exact_private_scalar_bridge(raw: Any, expected: Any) -> None:
    event = _request_observation(payload(raw), session_id="observer", submission_id="submission")
    assert isinstance(event, ModelRequestDispatching)
    assert event.reasoning_effort == expected and type(event.reasoning_effort) is type(expected)
    assert event.reasoning_summary is None and event.service_tier is None
    with pytest.raises(FrozenInstanceError):
        event.model = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "raw",
    [
        9007199254740993,
        "9007199254740993",
        True,
        {"kind": "integer", "decimal": "01"},
        {"kind": "integer", "decimal": "-1"},
        {"kind": "integer", "decimal": "18446744073709551616"},
        {"kind": "integer", "decimal": "١"},
        {"kind": "integer", "decimal": "1", "body": "private"},
        {"kind": "text", "value": 1},
        {"kind": "unknown", "value": "x"},
    ],
)
def test_malformed_scalar_is_dropped_without_failure(raw: Any) -> None:
    assert (
        _request_observation(payload(raw), session_id="observer", submission_id="submission")
        is None
    )


def test_unknown_record_fields_and_wrong_types_never_enter_public_events() -> None:
    for value in [
        {**payload(), "body": "private"},
        {**payload(), "transport": "unknown"},
        {**payload(), "service_tier": {}},
        {**payload(), "turn_id": None},
    ]:
        assert (
            _request_observation(value, session_id="observer", submission_id="submission") is None
        )
    link = _request_observation(
        {
            "type": "model_response_linked",
            "request_id": "request",
            "thread_id": "thread",
            "turn_id": "turn",
            "response_observation_id": "occurrence",
        },
        session_id="observer",
        submission_id="submission",
    )
    assert isinstance(link, ModelResponseLinked)
    assert link.request_id == "request" and link.response_observation_id == "occurrence"


async def test_reader_closure_and_buffer_overflow_do_not_change_metadata() -> None:
    buffer = _ObservationBuffer("observer")
    event = _request_observation(payload(), session_id="observer", submission_id="submission")
    assert event is not None
    reader = EventStream(buffer, after=0)
    await reader.aclose()
    for _ in range(300):
        buffer.append(event)
    buffer.finish("closed")
    retained = [value async for value in EventStream(buffer, after=0)]
    assert isinstance(retained[0], EventGap)
    assert any(isinstance(value, ModelRequestDispatching) for value in retained)
    assert event.sequence == 0


async def test_host_routes_exact_submission_and_drops_retired_metadata() -> None:
    import asyncio
    import weakref
    from types import SimpleNamespace
    from typing import cast

    from _agents_codex.session import _Submission
    from agents.session import Session

    session = object.__new__(Session)
    session._id = "thread"
    session._observations = _ObservationBuffer("thread")
    state = _Submission(cast(Any, None))
    state.issued = cast(Any, SimpleNamespace(turn_id="turn", submission_id="original"))
    session._request_observation_routes = weakref.WeakValueDictionary()
    assert state.issued is not None
    session._register_request_observation(state, state.issued)
    event = {
        "id": "turn",
        "msg": {
            "type": "_sdk_request_metadata",
            "submission_id": "original",
            "observation": payload(),
        },
    }
    # No Session._active exists: attribution uses the immutable submitted route.
    session._observe_request_metadata(event)
    state.completion.set_result(None)
    await asyncio.sleep(0)
    assert "original" not in session._request_observation_routes
    later = _Submission(cast(Any, None))
    later.issued = cast(Any, SimpleNamespace(turn_id="turn", submission_id="later"))
    assert later.issued is not None
    session._register_request_observation(later, later.issued)
    session._observe_request_metadata(event)
    session._observe_request_metadata({**event, "msg": {**event["msg"], "submission_id": "later"}})
    later.completion.set_result(None)
    session._observations.finish("closed")
    seen = [item async for item in EventStream(session._observations, after=0)]
    observations = [item for item in seen if isinstance(item, ModelRequestDispatching)]
    assert [event.submission_id for event in observations] == ["original", "later"]


@pytest.mark.parametrize("include_details", [False, True])
async def test_execution_filter_preserves_request_dispatch_and_response_links(
    include_details: bool,
) -> None:
    buffer = _ObservationBuffer("observer")
    dispatch = _request_observation(payload(), session_id="observer", submission_id="submission")
    link = _request_observation(
        {
            "type": "model_response_linked",
            "request_id": "request",
            "thread_id": "thread",
            "turn_id": "turn",
            "response_observation_id": "occurrence",
        },
        session_id="observer",
        submission_id="submission",
    )
    assert dispatch is not None and link is not None
    buffer.append(dispatch)
    buffer.append(
        ExecutionItemCompleted(
            session_id="observer",
            turn_id="turn",
            submission_id="submission",
            item={
                "type": "command_execution",
                "id": "execution",
                "call_id": "call",
                "turn_id": "turn",
                "command": "synthetic-command",
                "cwd": None,
                "status": "completed",
                "output": "synthetic-output",
                "exit_code": 0,
                "duration_ms": 1,
            },
        )
    )
    buffer.append(link)
    buffer.finish("closed")
    seen = [
        event
        async for event in EventStream(buffer, after=0, _include_execution_details=include_details)
    ]
    assert isinstance(seen[0], ModelRequestDispatching)
    assert seen[0].request_id == "request" and seen[0].submission_id == "submission"
    assert isinstance(seen[1], ExecutionItemCompleted)
    assert seen[1].item.get("call_id") == "call"
    assert ("command" in seen[1].item) is include_details
    assert ("output" in seen[1].item) is include_details
    assert isinstance(seen[2], ModelResponseLinked)
    assert seen[2].request_id == "request" and seen[2].response_observation_id == "occurrence"
    assert [event.sequence for event in seen[:3]] == [1, 2, 3]
