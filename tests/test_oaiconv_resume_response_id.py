"""Tests for OpenAIServerConversationTracker.hydrate_from_state response_id seeding."""

from typing import Any

from agents.items import ModelResponse
from agents.run_internal.oai_conversation import OpenAIServerConversationTracker
from agents.usage import Usage


def _make_response(response_id: str | None) -> ModelResponse:
    response = object.__new__(ModelResponse)
    response.output = []
    response.usage = Usage()
    response.response_id = response_id
    return response


def test_hydrate_from_state_uses_latest_non_none_response_id() -> None:
    """Resume should chain to the most recent response_id, not None when last response lacks id.

    A run might produce model responses across providers where some have no `response_id`
    (e.g., a non-Responses fallback). `track_server_items` skips updates when response_id is
    None, so live runs preserve the last known id. Resume hydration should match that
    behavior — falling back to the last id-bearing response instead of forgetting the chain.
    """
    tracker = OpenAIServerConversationTracker(
        conversation_id=None,
        previous_response_id=None,
        auto_previous_response_id=True,
    )

    responses: list[Any] = [
        _make_response("resp_first"),
        _make_response("resp_second"),
        _make_response(None),
    ]

    tracker.hydrate_from_state(
        original_input=[],
        generated_items=[],
        model_responses=responses,
    )

    assert tracker.previous_response_id == "resp_second"
