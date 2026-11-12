"""Current declaration bounds at restored native opening."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from _agents_codex.session import NativeError, Session


class RestoredControls:
    def __init__(self, reject=None):
        self.controls = {
            "attempt_timeout_ms": 60000,
            "retry": {
                "configured": True,
                "has_policy": True,
                "max_retries": 99,
                "backoff": {"initial_delay_ms": 5000, "max_delay_ms": 60000, "jitter": True},
            },
            "temperature": 0.25,
        }
        self.calls = 0
        self.reject = reject

    async def _update_model_settings(self, encoded):
        import json

        self.calls += 1
        for key, value in json.loads(encoded).get("request_controls", {}).items():
            if isinstance(value, dict) and isinstance(self.controls.get(key), dict):
                self.controls[key].update(deepcopy(value))
            else:
                self.controls[key] = deepcopy(value)
        return self.calls != self.reject


async def opening(thread, settings, *, restored=True):
    async def publish(operation):
        return await operation

    owner = SimpleNamespace(_native=thread, _publish_startup=publish)
    await Session._publish_opening_configuration(
        owner,
        settings=settings,
        fresh=False,
        restored=restored,
        validate_provider_model=False,
        instructions="",
        instruction_receipts=(),
        mcp_config="[]",
        mcp_tools=(),
        allowed_mcp=(),
        generation=0,
        profile_roster=[],
    )


@pytest.mark.parametrize(
    "current",
    [
        {},
        {"retry": None},
        {"retry": {"configured": True, "max_retries": 0}},
        {"retry": {"configured": True, "max_retries": 1, "has_policy": True}},
        {"attempt_timeout_ms": 200},
        {"attempt_timeout_ms": None, "retry": {"configured": True, "max_retries": 0}},
    ],
)
async def test_restore_replaces_saved_retry_authority_with_current_controls(current):
    thread = RestoredControls()
    await opening(thread, {"request_controls": current})
    assert thread.controls["attempt_timeout_ms"] == current.get("attempt_timeout_ms")
    assert thread.controls["retry"] == current.get("retry")
    assert thread.controls["temperature"] == 0.25


async def test_atomic_first_turn_keeps_its_already_captured_controls():
    thread = RestoredControls()
    original = deepcopy(thread.controls)
    await opening(thread, {}, restored=False)
    assert thread.controls == original
    assert thread.calls == 0


@pytest.mark.parametrize("reject", [1, 2])
async def test_restore_requires_both_native_update_acknowledgements(reject):
    thread = RestoredControls(reject=reject)
    with pytest.raises(NativeError, match="acknowledgement"):
        await opening(thread, {"request_controls": {"retry": {"max_retries": 0}}})
    assert thread.calls == reject
