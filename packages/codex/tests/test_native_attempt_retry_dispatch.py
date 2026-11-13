"""Native callback decisions retain exact provider owners without a host retry loop."""

from __future__ import annotations

import inspect

import pytest

from _agents_codex._model_retry import _ModelFailure, _normalized_error, _resolve_retry
from agents import ModelRequest, RetryAdvice, RetryDecision


def context(*, policy=True, original="attempt-2"):
    return {
        "attempt_id": "attempt-2",
        "original_error_attempt_id": original,
        "attempt": 1,
        "request_attempt": 2,
        "max_retries": 0,
        "policy_enabled": policy,
        "model": "fixture",
        "thread_id": "root",
        "turn_id": "turn",
        "transport": "model_producer",
        "normalized": {"message": "native failure", "is_timeout": original is None},
        "provider_advice": {},
        "response_started": False,
        "retry_unsafe_output": False,
        "local_effects": False,
        "stateful": False,
        "previous_response_id": None,
        "conversation_id": None,
        "usage": None,
    }


async def invoke(callback, argument):
    value = callback(argument)
    return await value if inspect.isawaitable(value) else value


def failed(backend=None):
    error = RuntimeError("exact provider exception")
    request = ModelRequest(
        {}, {"application": True}, "session", "turn", attempt_id="attempt-2", attempt=2
    )
    return _ModelFailure("attempt-2", request, backend, error)


async def test_policy_receives_exact_error_and_independent_request_ordinal():
    failure = failed()
    seen = []

    def policy(value):
        seen.append(value)
        return False

    result = await _resolve_retry(context(), failure, policy, invoke)
    assert result["decision"]["retry"] is False
    assert seen[0].error is failure.error
    assert (seen[0].attempt, seen[0].request_attempt, seen[0].max_retries) == (1, 2, 0)
    assert failure.selected_error("attempt-2") is failure.error


async def test_disabled_or_exhausted_policy_retains_advice_and_exact_failed_request():
    observed = []

    class Backend:
        @property
        def get_retry_advice(self):
            observed.append("capture")
            return self.advice

        def advice(self, value):
            observed.append(value)
            return RetryAdvice(suggested=True, retry_after_ms=123)

    failure = failed(Backend())

    def forbidden(_context):
        pytest.fail("A cleared policy must not be invoked")

    result = await _resolve_retry(context(policy=False), failure, forbidden, invoke)
    assert result["decision"] is None
    assert result["provider_advice"]["retry_after_ms"] == 123
    assert observed[0] == "capture"
    assert len(observed) == 2
    assert observed[1].request is failure.request
    assert observed[1].error is failure.error
    assert observed[1].attempt == 1 and observed[1].request_attempt == 2


async def test_native_timeout_does_not_substitute_the_producer_abort_exception():
    failure = failed()
    seen = []
    await _resolve_retry(
        context(original=None),
        failure,
        lambda value: seen.append(value) or RetryDecision(False),
        invoke,
    )
    assert seen[0].error is None
    assert seen[0].normalized.is_timeout
    assert failure.selected_error(None) is None


async def test_foreign_error_owner_fails_before_application_entry():
    with pytest.raises(ValueError, match="unknown provider error owner"):
        await _resolve_retry(
            context(original="other-attempt"),
            failed(),
            lambda _value: pytest.fail("unowned policy"),
            invoke,
        )


async def test_policy_exception_keeps_identity_instead_of_provider_error():
    policy_error = ValueError("policy failure")

    def policy(_value):
        raise policy_error

    with pytest.raises(ValueError) as caught:
        await _resolve_retry(context(), failed(), policy, invoke)
    assert caught.value is policy_error


def test_normalized_error_preserves_cause_and_native_millisecond_retry_hint():
    from types import SimpleNamespace

    cause = ConnectionError("connection failed")
    cause.status_code = 429
    cause.code = "rate_limit"
    cause.request_id = "request-id"
    cause.response = SimpleNamespace(headers={"retry-after-ms": "125"})
    error = RuntimeError("bounded " + "x" * 3000)
    error.__cause__ = cause
    facts = _normalized_error(error)
    assert facts.status_code == 429 and facts.error_code == "rate_limit"
    assert facts.request_id == "request-id" and facts.retry_after_ms == 125
    assert facts.is_network_error and not facts.is_abort
    assert len(facts.message) == 2048


def test_opening_numeric_override_preserves_its_same_source_policy_and_null_clears():
    from _agents_codex.session import _capture_opening_agent, _native_model_settings
    from agents import Agent, ModelRetrySettings

    def policy(_context):
        return False

    agent = Agent(
        instructions="fixture",
        model_settings={"retry": ModelRetrySettings(max_retries=3, policy=policy)},
    )
    captured = _capture_opening_agent(
        agent,
        provider=None,
        model=None,
        model_settings={"retry": ModelRetrySettings(max_retries=1)},
        tool_selection=None,
    )
    retry = captured.model_settings["retry"]
    assert retry.max_retries == 1 and retry.policy is policy
    wire = _native_model_settings(None, captured.model_settings)
    assert wire["request_controls"]["retry"] == {
        "max_retries": 1,
        "configured": True,
        "has_policy": True,
    }
    cleared = _capture_opening_agent(
        agent, provider=None, model=None, model_settings={"retry": None}, tool_selection=None
    )
    assert cleared.model_settings["retry"] is None
    assert _native_model_settings(None, cleared.model_settings)["request_controls"]["retry"] is None


async def test_different_update_policy_rejects_before_any_native_publication():
    from types import SimpleNamespace

    from _agents_codex.session import Session
    from agents import Agent, ModelRetrySettings

    def original(_context):
        return False

    agent = Agent(
        instructions="fixture", model_settings={"retry": ModelRetrySettings(policy=original)}
    )

    async def refresh_profile():
        assert owner._update_generation == 1

    owner = SimpleNamespace(
        _update_generation=0,
        _check_idle_update=lambda: None,
        _opening_agent=agent,
        _refresh_profile_bindings=refresh_profile,
        _effective_agent=lambda: agent,
        _join_mutation=lambda pending: pending,
    )
    with pytest.raises(ValueError, match="different retry policy callable"):
        await Session.update(
            owner, model_settings={"retry": ModelRetrySettings(policy=lambda _context: True)}
        )
    assert owner._update_generation == 1


def test_error_diagnostics_cannot_replace_the_retained_provider_failure():
    class ProviderError(Exception):
        @property
        def status_code(self):
            raise RuntimeError("diagnostic getter failure")

        def __str__(self):
            raise RuntimeError("diagnostic formatter failure")

    error = ProviderError()
    facts = _normalized_error(error)
    assert facts.message == "Model producer failed"
    assert facts.status_code is None
    assert (
        _ModelFailure("attempt-2", failed().request, None, error).selected_error("attempt-2")
        is error
    )


def test_error_diagnostics_remain_valid_unicode_on_the_native_json_wire():
    import json
    from dataclasses import asdict

    error = RuntimeError("invalid \ud800 text")
    error.code = "bad \udfff code"
    facts = _normalized_error(error)
    encoded = json.dumps(asdict(facts), ensure_ascii=False).encode("utf-8")
    assert b"invalid ? text" in encoded and b"bad ? code" in encoded


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_producer_captures_its_complete_property_once_before_callback_entry(asynchronous):
    import asyncio
    import json
    from types import SimpleNamespace

    from _agents_codex.session import Session
    from agents import ModelResponse

    seen = []
    replies = []

    class Model:
        @property
        def complete(self):
            seen.append("capture")
            assert seen == ["capture"]
            return self.async_complete if asynchronous else self.sync_complete

        def sync_complete(self, request):
            seen.append(("body", request.attempt_id, request.attempt))
            return ModelResponse(
                [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "captured"}],
                    }
                ]
            )

        async def async_complete(self, request):
            await asyncio.sleep(0)
            return self.sync_complete(request)

    state = SimpleNamespace(stopping=False, failure=asyncio.get_running_loop().create_future())
    native = SimpleNamespace(
        _check_model_producer=lambda _request_id: True,
        _reply_model_producer=lambda *args, **kwargs: replies.append((args, kwargs)) or True,
    )
    owner = SimpleNamespace(
        _active=state,
        _profiles=None,
        _model_backend=Model(),
        _native=native,
        _catalog_for_message=lambda *args: None,
        _context=None,
        _include_raw_provider_events=False,
        id="session",
    )
    await Session._produce_model_response(
        owner,
        state,
        {
            "request_id": 1,
            "generation": 1,
            "turn_id": "turn",
            "attempt_id": "attempt-2",
            "attempt": 2,
            "data": {"model": "fixture"},
        },
    )
    assert seen == ["capture", ("body", "attempt-2", 2)]
    assert not state.failure.done()
    assert replies[0][1] == {"error": False}
    assert json.loads(replies[0][0][1])["output"][0]["content"][0]["text"] == "captured"


@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_preparation_revocation_stays_nonfatal_and_replies_to_original_native(
    cleanup_failure,
):
    import asyncio
    from types import SimpleNamespace

    from _agents_codex.session import Session, _Invocation, _invocation

    replies = []
    state = SimpleNamespace(
        stopping=False,
        failure=asyncio.get_running_loop().create_future(),
        prepare_request=lambda *args: {},
        handle=object(),
        preparation_index=0,
        tracing=None,
        cleanup_error=None,
    )
    state.retain_cleanup_error = lambda error: setattr(state, "cleanup_error", error)
    native = SimpleNamespace(
        _check_prepared_request=lambda _request_id: True,
        _python_callback_cancelled=lambda _receipt: True,
        _reply_prepared_request=lambda *args, **kwargs: replies.append((args, kwargs)) or False,
    )
    callback = SimpleNamespace(native=native, receipt={"kind": "preparation"}, cancelled=True)
    error = RuntimeError("cleanup failed") if cleanup_failure else asyncio.CancelledError()

    async def decide(*args, **kwargs):
        if cleanup_failure:
            try:
                raise asyncio.CancelledError()
            finally:
                raise error
        raise error

    owner = SimpleNamespace(
        _active=state,
        _native=object(),
        _catalog_for_message=lambda *args: None,
        _call_decision_callback=decide,
        active_turn=state.handle,
    )
    token = _invocation.set(_Invocation(owner, native_callback=(state, callback)))
    try:
        await Session._prepare_sampling_request(
            owner, state, {"request_id": 7, "generation": 1, "data": {}, "recovery_attempt": 0}
        )
    finally:
        _invocation.reset(token)
    assert not state.failure.done()
    assert state.cleanup_error is (error if cleanup_failure else None)
    assert replies == [((7, None), {"error": True})]


@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_physical_callback_release_reports_retained_cleanup_failure(cleanup_failure):
    import asyncio
    from types import SimpleNamespace

    from _agents_codex.session import Session

    released = []
    callbacks = {}
    native = SimpleNamespace(
        _enter_python_callback=lambda _receipt: True,
        _release_python_callback=lambda *args, **kwargs: released.append(kwargs) or True,
    )
    state = SimpleNamespace(
        callbacks=callbacks, cleanup_error=RuntimeError("cleanup") if cleanup_failure else None
    )
    state.add_callback = lambda task, owner: callbacks.__setitem__(task, owner)
    owner = SimpleNamespace(_native=native)

    async def body():
        await asyncio.sleep(0)

    task = Session._start_native_callback(
        owner, state, body(), {"kind": "preparation", "invocation": 1}
    )
    await task
    await asyncio.sleep(0)
    assert released == [{"cleanup_failed": True} if cleanup_failure else {}]
    assert callbacks[task] is None


@pytest.mark.parametrize("error_kind", ["cancelled", "api_abort", "cleanup"])
async def test_revoked_producer_retains_expected_abort_only_for_advice(error_kind):
    import asyncio
    from types import SimpleNamespace

    from _agents_codex.session import Session, _Invocation, _invocation

    class APIUserAbortError(Exception):
        pass

    error = (
        asyncio.CancelledError()
        if error_kind == "cancelled"
        else RuntimeError("cleanup failed")
        if error_kind == "cleanup"
        else APIUserAbortError("expected abort")
    )
    state = SimpleNamespace(
        stopping=False,
        failure=asyncio.get_running_loop().create_future(),
        cleanup_error=None,
        model_failure=None,
    )
    state.retain_cleanup_error = lambda value: setattr(state, "cleanup_error", value)
    native = SimpleNamespace(
        _check_model_producer=lambda _request_id: True,
        _reply_model_producer=lambda *args, **kwargs: False,
    )
    callback = SimpleNamespace(native=native, receipt={"kind": "preparation"}, cancelled=False)
    advice = []

    class Model:
        def complete(self, request):
            callback.cancelled = True
            if error_kind == "cleanup":
                try:
                    raise asyncio.CancelledError()
                finally:
                    raise error
            raise error

        def get_retry_advice(self, request):
            advice.append(request)
            return RetryAdvice(suggested=True)

    owner = SimpleNamespace(
        _active=state,
        _profiles=None,
        _model_backend=Model(),
        _native=object(),
        _catalog_for_message=lambda *args: None,
        _context=None,
        _include_raw_provider_events=False,
        id="session",
    )
    token = _invocation.set(_Invocation(owner, native_callback=(state, callback)))
    try:
        await Session._produce_model_response(
            owner,
            state,
            {
                "request_id": 1,
                "generation": 1,
                "turn_id": "turn",
                "attempt_id": "attempt-2",
                "attempt": 2,
                "data": {"model": "fixture"},
            },
        )
    finally:
        _invocation.reset(token)
    assert not state.failure.done()
    if error_kind == "cleanup":
        assert isinstance(error.__context__, asyncio.CancelledError)
        assert state.cleanup_error is error and state.model_failure is None
        return
    assert state.cleanup_error is None
    assert state.model_failure.error is error
    seen = []
    await _resolve_retry(
        context(original=None),
        state.model_failure,
        lambda value: seen.append(value) or False,
        invoke,
    )
    assert advice[0].error is error
    assert advice[0].request is state.model_failure.request
    assert seen[0].error is None and seen[0].normalized.is_timeout


@pytest.mark.parametrize("evidence", [None, False, True])
async def test_provider_response_start_evidence_preserves_unknown_and_native_facts(evidence):
    class Backend:
        def get_retry_advice(self, _request):
            return RetryAdvice(response_started=evidence)

    seen = []

    def policy(value):
        seen.append(value)
        return RetryDecision(
            retry=True,
            approve_unsafe_replay=(
                not value.response_started and value.provider_advice.response_started is False
            ),
        )

    native = context()
    native["local_effects"] = True
    native["retry_unsafe_output"] = True
    reply = await _resolve_retry(native, failed(Backend()), policy, invoke)
    assert seen[-1].response_started is (evidence is True)
    assert seen[-1].provider_advice.response_started is evidence
    assert reply["decision"]["approve_unsafe_replay"] is (evidence is False)
    assert seen[-1].local_effects is True
    assert seen[-1].retry_unsafe_output is True
    assert "response_started" not in reply["provider_advice"]
    native["response_started"] = True
    await _resolve_retry(native, failed(Backend()), policy, invoke)
    assert seen[-1].response_started is True


async def test_provider_normalized_advice_is_a_detached_policy_view():
    source = {
        "status_code": None,
        "retry_after_ms": None,
        "is_abort": False,
        "is_network_error": False,
        "is_timeout": False,
    }

    class Backend:
        def get_retry_advice(self, _request):
            advice = RetryAdvice(retry_after_ms=7, normalized=source)
            source["status_code"] = 429
            return advice

    admitted = context(original=None)
    admitted["normalized"].update(
        is_abort=True, is_network_error=True, status_code=503, request_id="native-id"
    )
    seen = []
    reply = await _resolve_retry(
        admitted, failed(Backend()), lambda value: seen.append(value) or True, invoke
    )
    normalized = seen[0].normalized
    assert normalized.status_code is None and normalized.retry_after_ms is None
    assert normalized.is_abort is True
    assert normalized.is_network_error is False and normalized.is_timeout is False
    assert normalized.request_id == "native-id"
    assert admitted["normalized"]["is_timeout"] is True
    assert admitted["normalized"]["status_code"] == 503
    assert "normalized" not in reply["provider_advice"]
    with pytest.raises(TypeError):
        seen[0].provider_advice.normalized["is_abort"] = False


@pytest.mark.parametrize(
    "normalized", [{"is_timeout": 1}, {"surprise": True}, {"retry_after_ms": float("nan")}]
)
def test_provider_normalized_advice_rejects_invalid_fields(normalized):
    with pytest.raises((TypeError, ValueError)):
        RetryAdvice(normalized=normalized)


def test_provider_normalized_advice_preserves_dataclass_copy_and_pickle():
    import json
    import pickle
    from copy import copy, deepcopy
    from dataclasses import asdict

    original = RetryAdvice(normalized={"status_code": 429, "is_timeout": False})
    for captured in (copy(original), deepcopy(original), pickle.loads(pickle.dumps(original))):
        assert captured == original
        assert captured.normalized == {"status_code": 429, "is_timeout": False}
        with pytest.raises(TypeError):
            captured.normalized["is_timeout"] = True
    assert json.loads(json.dumps(asdict(original)))["normalized"] == {
        "status_code": 429,
        "is_timeout": False,
    }
