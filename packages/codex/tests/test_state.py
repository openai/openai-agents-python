import json

import pytest
from pydantic import ValidationError

from agents.state import SessionState


def test_checkpoint_without_roles_preserves_native_tree_envelope():
    payload = {
        "format_version": 2,
        "codex_revision": "revision",
        "source_session_id": "session",
        "output_version": 1,
        "output_fingerprint": "0" * 64,
        "root_metadata": {},
        "history": [{"type": "message", "content": "remember this"}],
    }
    checkpoint = SessionState.from_json(json.dumps(payload))
    assert checkpoint.subagents == {}
    assert json.loads(checkpoint.to_json()) == payload


@pytest.mark.parametrize(
    "change",
    [
        {"format_version": 1},
        {"format_version": 3},
        {"root_metadata": None},
        {"unexpected": True},
        {"history": "invalid"},
    ],
)
def test_checkpoint_rejects_incompatible_envelopes(change):
    checkpoint = {
        "format_version": 2,
        "codex_revision": "revision",
        "source_session_id": "session",
        "output_version": 1,
        "output_fingerprint": "0" * 64,
        "root_metadata": {},
        "history": [],
        **change,
    }
    with pytest.raises(ValidationError):
        SessionState.from_json(json.dumps(checkpoint))
