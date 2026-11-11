"""Public native attempts retain original errors, cleanup and measured usage."""

from __future__ import annotations

import asyncio
import threading

import pytest

from agents import (
    Agent,
    ModelRequest,
    ModelResponse,
    ModelRetrySettings,
    NativeError,
    NativeProvider,
    RetryAdvice,
    RetryBackoffSettings,
    RetryDecision,
    retry_policies,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


def response(text: str, tokens: int = 5) -> ModelResponse:
    return ModelResponse(
        [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens},
    )


def settings(policy, *, retries=1, attempt_ms=None):
    result = {
        "retry": ModelRetrySettings(
            max_retries=retries,
            policy=policy,
            backoff=RetryBackoffSettings(initial_delay_ms=0, max_delay_ms=0, jitter=False),
        )
    }
    if attempt_ms is not None:
        result["attempt_timeout_ms"] = attempt_ms
    return result


async def test_policy_retry_preserves_exact_error_and_counts_each_attempt_once():
    requests, decisions, measured = [], [], []
    original = RuntimeError("original producer error")

    class Model:
        def complete(self, request: ModelRequest):
            requests.append(request)
            if len(requests) == 1:
                measured.append(request.report_usage({"input_tokens": 3, "total_tokens": 3}))
                raise original
            return response("recovered", 7)

    def policy(context):
        decisions.append(context)
        assert context.error is original
        return RetryDecision(True, delay_ms=0)

    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        result = await Agent(
            model="custom-model",
            instructions="Answer.",
            provider=provider,
            model_settings=settings(policy),
        ).run("go")
    assert result.final_output == "recovered"
    assert len(requests) == 2 and measured == [True] and len(decisions) == 1
    assert requests[0].attempt_id != requests[1].attempt_id
    assert [request.attempt for request in requests] == [1, 2]
    assert [request.disable_provider_retries for request in requests] == [False, True]
    assert result.run_usage.requests == 2
    assert result.run_usage.input_tokens == 10
    assert result.run_usage.missing_usage_requests == 0
    assert requests[0].report_usage({"input_tokens": 99}) is False


async def test_composed_retry_policy_dispatches_each_predicate_in_its_executor():
    loop_thread = threading.get_ident()
    predicate_threads = []
    attempts = 0

    class Model:
        def complete(self, _request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("retry this attempt")
            return response("recovered")

    def sync_policy(_context):
        predicate_threads.append(("sync", threading.get_ident()))
        return True

    async def async_policy(_context):
        predicate_threads.append(("async", threading.get_ident()))
        return True

    policy = retry_policies.any(retry_policies.all(sync_policy, async_policy))
    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        result = await Agent(
            model="custom-model",
            instructions="Answer.",
            provider=provider,
            model_settings=settings(policy),
        ).run("go")
    assert result.final_output == "recovered"
    assert [kind for kind, _ in predicate_threads] == ["sync", "async"]
    assert predicate_threads[0][1] != loop_thread
    assert predicate_threads[1][1] == loop_thread


async def test_retry_update_uses_the_active_handoff_agent(responses):
    def source_policy(_context):
        return False

    def target_policy(_context):
        return False

    target = Agent(name="target", instructions="Target.", model_settings=settings(target_policy))
    provider = NativeProvider(
        base_url=responses.url, api_key="synthetic-retry-handoff-key", supports_websockets=False
    )
    source = Agent(
        instructions="Source.",
        handoffs=[target],
        provider=provider,
        model_settings=settings(source_policy),
    )
    responses.respond(function_call("transfer_to_target", {}, "handoff-target"))
    responses.respond(assistant_output("target active"))
    async with timeout(20), source.session() as session:
        assert (await (await session.submit("Switch to target")).get_result()).final_output == (
            "target active"
        )
        assert await session.get_active_agent() is target
        await session.update(model_settings=settings(target_policy, retries=2))
        with pytest.raises(ValueError, match="different retry policy callable"):
            await session.update(model_settings=settings(source_policy))


@pytest.mark.parametrize("retry", [False, True], ids=["declined", "exhausted"])
async def test_declined_or_exhausted_retry_raises_the_exact_provider_exception(retry):
    original = RuntimeError("retained provider error")
    requests, decisions = [], []

    class Model:
        async def complete(self, request):
            requests.append(request)
            raise original

    def policy(context):
        decisions.append(context)
        assert context.error is original
        return retry

    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        with pytest.raises(RuntimeError) as caught:
            await Agent(
                model="custom-model",
                instructions="Answer.",
                provider=provider,
                model_settings=settings(policy),
            ).run("go")
    assert caught.value is original
    assert len(requests) == (2 if retry else 1)
    assert len(decisions) == 1


async def test_policy_failure_keeps_precedence_over_the_provider_exception():
    original, policy_error = RuntimeError("producer failed"), ValueError("policy failed")
    requests = []

    class Model:
        def complete(self, request):
            requests.append(request)
            raise original

    def policy(_context):
        raise policy_error

    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        with pytest.raises(ValueError) as caught:
            await Agent(
                model="custom-model",
                instructions="Answer.",
                provider=provider,
                model_settings=settings(policy),
            ).run("go")
    assert caught.value is policy_error and len(requests) == 1


async def test_attempt_timeout_joins_cleanup_and_measurement_before_retry():
    requests, order, reports, aborts = [], [], [], []

    class Model:
        async def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    aborts.append(error)
                    raise
                finally:
                    reports.append(request.report_usage({"input_tokens": 2, "total_tokens": 2}))
                    order.append("released")
            assert order == ["released", "advice", "policy"]
            return response("retry after cleanup", 4)

        def get_retry_advice(self, request):
            assert request.error is aborts[0]
            assert request.request is requests[0]
            assert order == ["released"]
            order.append("advice")
            return RetryAdvice(suggested=True, retry_after_ms=0)

    def policy(context):
        assert context.normalized.is_timeout and context.error is None
        assert order == ["released", "advice"]
        order.append("policy")
        # The delay exceeds the expired attempt's own clock; the next attempt
        # must still receive its full independent timeout after this backoff.
        return RetryDecision(True, delay_ms=750)

    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        result = await Agent(
            model="custom-model",
            instructions="Answer.",
            provider=provider,
            model_settings=settings(policy, attempt_ms=500),
        ).run("go")
    assert result.final_output == "retry after cleanup"
    assert reports == [True] and len(requests) == 2
    assert result.run_usage.requests == 2 and result.run_usage.input_tokens == 6
    assert result.run_usage.missing_usage_requests == 0


async def test_failed_cleanup_vetoes_retry_even_with_an_affirmative_policy():
    requests, decisions = [], []

    class Model:
        async def complete(self, request):
            requests.append(request)
            try:
                await asyncio.Event().wait()
            finally:
                raise RuntimeError("distinct cleanup failure")

    def policy(context):
        decisions.append(context)
        return True

    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        with pytest.raises(NativeError, match="cleanup"):
            await Agent(
                model="custom-model",
                instructions="Answer.",
                provider=provider,
                model_settings=settings(policy, attempt_ms=500),
            ).run("go")
    assert len(requests) == 1 and decisions == []


@pytest.mark.parametrize("suggested", [True, False])
async def test_provider_advice_controls_default_retry_without_a_policy(suggested):
    original = RuntimeError("advice-only producer failure")
    requests, advice = [], []

    class Model:
        def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                raise original
            return response("advice recovered", 7)

        def get_retry_advice(self, request):
            advice.append(request)
            assert request.error is original and request.request is requests[0]
            return RetryAdvice(suggested=suggested, replay_safety="safe", retry_after_ms=0)

    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        agent = Agent(
            model="custom-model",
            instructions="Answer.",
            provider=provider,
            model_settings=settings(None),
        )
        if suggested:
            result = await agent.run("go")
            assert result.final_output == "advice recovered"
            assert result.run_usage.requests == 2
            assert result.run_usage.missing_usage_requests == 1
            assert result.run_usage.input_tokens == 7
        else:
            with pytest.raises(RuntimeError) as caught:
                await agent.run("go")
            assert caught.value is original
    assert len(requests) == (2 if suggested else 1)
    assert len(advice) == 1


async def test_whole_invocation_deadline_bounds_repeated_preparation_timeouts():
    preparations, released, requests, decisions = [], [], [], []

    class Model:
        def complete(self, request):
            requests.append(request)
            return response("unexpected producer")

    async def prepare(turn, request, *, preparation_index, recovery_attempt):
        preparations.append(preparation_index)
        try:
            await asyncio.Event().wait()
        finally:
            released.append(preparation_index)

    def policy(context):
        decisions.append(context)
        assert context.normalized.is_timeout
        return True

    controls = settings(policy, retries=3, attempt_ms=250)
    controls["invocation_timeout_ms"] = 375
    async with timeout(15), NativeProvider(model_backend=Model()) as provider:
        with pytest.raises(NativeError) as caught:
            await Agent(
                model="custom-model",
                instructions="Answer.",
                provider=provider,
                model_settings=controls,
            ).run("go", prepare_request=prepare)
    assert caught.value.code == "model_invocation_deadline_exceeded"
    assert 1 <= len(preparations) <= 2
    assert preparations == released and requests == []
    assert len(decisions) <= 1


async def test_manual_restore_retains_prior_usage_once_and_adds_a_fresh_native_owner():
    from agents import FunctionTool, InteractionRequired
    from agents.state import SessionState
    from tests.native_fixtures import function_call
    from tests.test_native_manual_tools import LookupArgs, next_request

    requests = []
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )

    class Model:
        def complete(self, request):
            requests.append(request)
            if len(requests) == 1:
                return ModelResponse(
                    [function_call("lookup", {"key": "A"}, "manual-usage")["item"]],
                    {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
                )
            return response("restored measurement", 7)

    async with timeout(25), NativeProvider(model_backend=Model()) as provider:
        agent = Agent(
            model="custom-model",
            instructions="Read A.",
            tools=[lookup],
            provider=provider,
            model_settings=settings(None),
        )
        async with agent.session() as source:
            original = await source.submit("go")
            await next_request(source.interactions(), lookup)
            saved = SessionState.from_json((await source.suspend()).to_json())
            with pytest.raises(InteractionRequired):
                await original.get_result()
        async with agent.session(state=saved) as restored:
            handle = restored.restored_turn
            assert handle is not None and handle.turn_id == original.turn_id
            pending = await next_request(restored.interactions(), lookup)
            await pending.respond("accepted-A")
            result = await handle.get_result()
    assert result.final_output == "restored measurement" and len(requests) == 2
    assert requests[0].attempt_id != requests[1].attempt_id
    assert result.run_usage.requests == 2
    assert result.run_usage.input_tokens == 12
    assert result.run_usage.missing_usage_requests == 0
