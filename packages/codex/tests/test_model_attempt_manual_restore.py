from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import _agents_codex.session as implementation
from _agents_codex.state import SessionState
from _agents_codex.usage import RunUsage, TokenUsage, _UsageCollector


def accepted(controls):
    return SimpleNamespace(
        root_metadata={"model": "fixture"},
        source_session_id="source",
        native_continuation=None,
        history=[
            {
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_id": "source",
                    "thread_settings": {
                        "model": "fixture",
                        "request_controls": controls,
                    },
                },
            }
        ],
    )


@pytest.mark.parametrize(
    "controls",
    [
        {},
        {"attempt_timeout_ms": None},
        {"attempt_timeout_ms": 1},
        {"attempt_timeout_ms": 4_294_967_295},
        {"retry": None},
        {"retry": {}},
        {"retry": {"configured": False, "has_policy": False, "max_retries": None}},
        {
            "retry": {
                "configured": True,
                "has_policy": True,
                "max_retries": 0,
                "backoff": {
                    "initial_delay_ms": 0,
                    "max_delay_ms": 5,
                    "multiplier": 1.5,
                    "jitter": False,
                },
            }
        },
        {"retry": {"configured": True, "backoff": None}, "attempt_timeout_ms": None},
    ],
)
def test_accepted_retry_controls_preserve_presence_null_and_values(controls):
    model, restored = implementation._manual_restore_model_settings(
        accepted(controls), authorized_model=None, trusted_default="fixture"
    )
    assert model == "fixture"
    assert restored["request_controls"] == controls
    if isinstance(controls.get("retry"), dict):
        assert restored["request_controls"]["retry"] is not controls["retry"]
    json.dumps(restored, allow_nan=False)


@pytest.mark.parametrize(
    "controls",
    [
        {"attempt_timeout_ms": True},
        {"attempt_timeout_ms": 0},
        {"attempt_timeout_ms": -1},
        {"attempt_timeout_ms": 4_294_967_296},
        {"retry": True},
        {"retry": {"configured": 1}},
        {"retry": {"has_policy": None}},
        {"retry": {"max_retries": True}},
        {"retry": {"max_retries": -1}},
        {"retry": {"max_retries": 4_294_967_296}},
        {"retry": {"backoff": {"initial_delay_ms": -1}}},
        {"retry": {"backoff": {"multiplier": float("inf")}}},
        {"retry": {"backoff": {"jitter": 1}}},
        {"retry": {"policy": "forged serialized callback"}},
    ],
)
def test_saved_retry_controls_do_not_bypass_accepted_shape(controls):
    with pytest.raises(ValueError):
        implementation._manual_restore_model_settings(
            accepted(controls), authorized_model=None, trusted_default="fixture"
        )


def snapshot(owner="native-owner", requests=3, missing=1):
    return {
        "owner_attempt_id": owner,
        "usage": {
            "requests": requests,
            "requests_without_token_usage": missing,
            "token_usage": {
                "input_tokens": 8,
                "cached_input_tokens": 1,
                "cache_write_input_tokens": 0,
                "output_tokens": 6,
                "reasoning_output_tokens": 2,
                "total_tokens": 14,
            },
        },
        "terminal_attempt_id": None,
    }


class CapturedState:
    def __init__(self, value):
        self.value = value
        self.format_version = value["format_version"]

    @classmethod
    def from_json(cls, raw):
        return cls(json.loads(raw))

    def to_json(self):
        return json.dumps(self.value)


async def capture(
    monkeypatch,
    *,
    output_mode="native",
    historical=None,
    joined_snapshot=None,
    live_snapshot=None,
    report_at_seal=True,
):
    monkeypatch.setattr(SessionState, "_from_native_capture", CapturedState.from_json)
    monkeypatch.setattr(implementation, "_check_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        implementation, "_redact_checkpoint_executor_receipts", lambda payload: None
    )
    full = _UsageCollector(historical)
    model = _UsageCollector()
    if live_snapshot is not None:
        full.attach(model)
        model.replace_model_attempt(live_snapshot)
    child = _UsageCollector()
    if report_at_seal:
        child.report(TokenUsage(3, 0, 0, 1, 0, 4))
        full.attach(child)
    active = SimpleNamespace(
        operation_refs=None,
        operation_mcp=None,
        operation_slots=None,
        output=SimpleNamespace(mode=output_mode, schema={"type": "string"}, fingerprint="schema"),
        output_override=False,
        budget=None,
        original_input=None,
        tracing=None,
        run_usage=full,
        model_run_usage=model,
        stopping=False,
        restored_pending=False,
        terminal=asyncio.get_running_loop().create_future(),
        failure=asyncio.get_running_loop().create_future(),
    )
    calls = []

    async def seal(schema=None, *, include_model_attempt=False):
        calls.append((schema, include_model_attempt))
        if report_at_seal:
            full.report(TokenUsage(7, 3, 0, 5, 2, 12))
            full.report(None)
        payload = {
            "format_version": 3,
            "source_session_id": "source",
            "history": [],
            "descendants": [],
        }
        return json.dumps(
            {"checkpoint": payload, "model_attempt": joined_snapshot}
            if include_model_attempt
            else payload
        )

    async def ready(_active):
        return True

    async def executor_digest():
        return None

    owner = SimpleNamespace(
        _executor=None,
        _native_agents=None,
        _project_guidance=None,
        _strict_executor_writes=False,
        _codex_login_binding=None,
        _current_remembered_approvals=lambda: (),
        _profiles=None,
        _portable_profile_workspaces={},
        _capture_produced_checkpoint_prefix=False,
        _produced_prefix=implementation._ProducedPrefix(),
        _default_tools=None,
        _repair_turns=[],
        _repair_items=[],
        _application_tool_outputs=[],
        _native=SimpleNamespace(_seal_manual=seal, _export_mcp_approval_choices=lambda: "{}"),
        _manual_ready=ready,
        _executor_policy_digest=executor_digest,
        _checkpoint_guardrail_records=lambda _active: [],
        _reader=SimpleNamespace(done=lambda: False),
        _instructions="",
        _sealing=False,
        _observations=SimpleNamespace(notify=lambda: None),
        _model_api_key="private",
    )
    raw = await implementation.Session._capture_checkpoint(owner, active, None, False)
    return json.loads(raw), calls, active


@pytest.mark.asyncio
@pytest.mark.parametrize("output_mode", ["native", "prompted"])
async def test_seal_joins_model_usage_without_replacing_historical_reports_or_children(
    monkeypatch,
    output_mode,
):
    historical = RunUsage(10, 2, 1, 4, 1, 14, 2, 1)
    live = snapshot(requests=1, missing=0)
    live["usage"]["token_usage"]["input_tokens"] = 5
    state, calls, active = await capture(
        monkeypatch,
        output_mode=output_mode,
        historical=historical,
        joined_snapshot=snapshot(),
        live_snapshot=live,
    )
    assert state["run_usage"] == {
        "input_tokens": 28,
        "cached_input_tokens": 6,
        "cache_write_input_tokens": 1,
        "output_tokens": 16,
        "reasoning_output_tokens": 5,
        "total_tokens": 44,
        "requests": 8,
        "missing_usage_requests": 3,
    }
    assert calls == [(json.dumps({"type": "string"}) if output_mode == "native" else None, True)]
    late = snapshot(missing=2)
    late["usage"]["token_usage"] = dict.fromkeys(late["usage"]["token_usage"], 0)
    active.model_run_usage.replace_model_attempt(late)
    assert active.run_usage.snapshot() == RunUsage(**state["run_usage"])
    assert active.failure.result().state.value["run_usage"] == state["run_usage"]
    assert state["agent_options"] == {}


@pytest.mark.asyncio
async def test_restore_offset_survives_no_new_generation_and_new_generation_once(
    monkeypatch,
):
    historical = RunUsage(10, 2, 1, 4, 1, 14, 2, 1)
    held, _, _ = await capture(
        monkeypatch,
        historical=historical,
        joined_snapshot=None,
        report_at_seal=False,
    )
    assert held["run_usage"] == {
        name: getattr(historical, name) for name in RunUsage.__dataclass_fields__
    }
    restored, _, _ = await capture(
        monkeypatch,
        historical=RunUsage(**held["run_usage"]),
        joined_snapshot=snapshot(owner="fresh-owner"),
        report_at_seal=False,
    )
    assert restored["run_usage"] == {
        "input_tokens": 18,
        "cached_input_tokens": 3,
        "cache_write_input_tokens": 1,
        "output_tokens": 10,
        "reasoning_output_tokens": 3,
        "total_tokens": 28,
        "requests": 5,
        "missing_usage_requests": 2,
    }
