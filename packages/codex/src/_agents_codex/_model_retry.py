"""Exact failed-attempt retention and bounded facts for the existing native exchange."""

from __future__ import annotations

import asyncio
import math
import sys
from dataclasses import dataclass, field
from typing import Any

from .models import ModelRequest
from .retry import NormalizedError


@dataclass(frozen=True, slots=True)
class _ModelFailure:
    attempt_id: str
    request: ModelRequest = field(repr=False)
    backend: Any = field(repr=False)
    error: BaseException = field(repr=False)
    stream: bool | None = None

    def selected_error(self, attempt_id: str | None) -> BaseException | None:
        if attempt_id is None:
            return None
        if attempt_id != self.attempt_id:
            raise ValueError("Native retry selected an unknown provider error owner")
        return self.error


def _is_model_abort(error: BaseException) -> bool:
    # Cleanup may replace cancellation while retaining it in __context__. Only
    # the actual raised error can classify a revoked callback as a healthy abort.
    return isinstance(error, asyncio.CancelledError) or type(error).__name__ in {
        "AbortError",
        "CancelledError",
        "APIUserAbortError",
    }


def _normalized_error(error: BaseException) -> NormalizedError:
    """Capture bounded diagnostics without replacing the retained original exception."""

    def read(value: Any, name: str) -> Any:
        try:
            return getattr(value, name, None)
        except BaseException:
            return None

    def text(value: str, limit: int) -> str:
        return value[:limit].encode("utf-8", "replace").decode("utf-8")

    # A borrowed OpenAI client has already imported its error types. Native
    # declarations and custom producers can work without that optional client.
    openai = sys.modules.get("openai")
    timeout_types = (
        TimeoutError,
        asyncio.TimeoutError,
        getattr(openai, "APITimeoutError", TimeoutError),
    )
    network_types = (
        ConnectionError,
        *timeout_types,
        getattr(openai, "APIConnectionError", ConnectionError),
    )
    status = code = request_id = retry_after = None
    abort = network = timeout = False
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        candidate_status = read(current, "status_code")
        if status is None and type(candidate_status) is int and 100 <= candidate_status <= 599:
            status = candidate_status
        candidate_code = read(current, "code")
        if code is None and isinstance(candidate_code, str):
            code = text(candidate_code, 256)
        candidate_id = read(current, "request_id")
        if request_id is None and isinstance(candidate_id, str):
            request_id = text(candidate_id, 256)
        headers = read(read(current, "response"), "headers")
        if retry_after is None and headers is not None:
            for key, scale in (("retry-after-ms", 1.0), ("retry-after", 1000.0)):
                try:
                    raw = headers.get(key)
                except BaseException:
                    continue
                if raw is None:
                    continue
                try:
                    delay = float(raw) * scale
                except (ValueError, TypeError, OverflowError):
                    continue
                if math.isfinite(delay) and delay >= 0:
                    retry_after = delay
                    break
        # asyncio timeouts retain their internal cancellation as a cause.
        # Preserve real aborts while avoiding a false veto below a timeout.
        if not (timeout and isinstance(current, asyncio.CancelledError)):
            abort |= _is_model_abort(current)
        timeout |= isinstance(current, timeout_types)
        network |= isinstance(current, network_types)
        following = read(current, "__cause__") or read(current, "__context__")
        current = following if isinstance(following, BaseException) else None
    try:
        message = text(str(error), 2048)
    except BaseException:
        message = "Model producer failed"
    return NormalizedError(
        message=message,
        status_code=status,
        error_code=code,
        request_id=request_id,
        retry_after_ms=retry_after,
        is_abort=abort,
        is_network_error=network,
        is_timeout=timeout,
    )


async def _resolve_retry(
    data: dict[str, Any], failure: _ModelFailure | None, policy: Any, invoke: Any
) -> dict[str, Any]:
    from dataclasses import asdict

    from .models import ModelRetryAdviceRequest
    from .retry import RetryAdvice, RetryDecision, RetryPolicyContext
    from .usage import TokenUsage, _UsageCollector

    values = raw_usage = context = advice = result = advisor = None
    try:
        values = dict(data)
        original_id = values.pop("original_error_attempt_id")
        policy_enabled = values.pop("policy_enabled")
        if failure is not None and failure.attempt_id != values["attempt_id"]:
            raise ValueError("Native retry has no matching failed request")
        if original_id is not None and failure is None:
            raise ValueError("Native retry has no retained original provider error")
        values["error"] = failure.selected_error(original_id) if failure is not None else None
        values["normalized"] = NormalizedError(**values["normalized"])
        values["provider_advice"] = RetryAdvice(**values["provider_advice"])
        if values["transport"] == "model_producer":
            values["stream"] = failure.stream if failure is not None else None
        elif values["transport"] in {"responses_json", "responses_sse", "responses_ws"}:
            values["stream"] = values["transport"] != "responses_json"
        raw_usage = values["usage"]
        if raw_usage is not None:
            values["usage"] = TokenUsage(
                **{key: raw_usage.get(key, 0) for key in TokenUsage.__dataclass_fields__}
            )
            _UsageCollector.capture(values["usage"])
        context = RetryPolicyContext(**values)
        advice = None
        if failure is not None:
            advisor = getattr(failure.backend, "get_retry_advice", None)
            if advisor is not None:
                if not callable(advisor):
                    raise TypeError("NativeModel.get_retry_advice must be callable")
                advice = await invoke(
                    advisor,
                    ModelRetryAdviceRequest(
                        failure.request,
                        failure.error,
                        context.attempt_id,
                        context.attempt,
                        context.request_attempt,
                        stream=context.stream,
                    ),
                )
                if advice is not None and not isinstance(advice, RetryAdvice):
                    raise TypeError("NativeModel.get_retry_advice must return RetryAdvice or None")
                if advice is not None:
                    from dataclasses import replace

                    context = replace(context, provider_advice=advice)
        decision = None
        if policy_enabled:
            if not callable(policy):
                raise ValueError("Native retry policy has no captured application owner")
            result = await invoke(policy, context)
            if type(result) is bool:
                result = RetryDecision(result)
            if not isinstance(result, RetryDecision):
                raise TypeError("Retry policy must return a bool or RetryDecision")
            decision = {
                key: getattr(result, key)
                for key in (
                    "retry",
                    "delay_ms",
                    "reason",
                    "approve_unsafe_replay",
                )
            }
        return {
            "decision": decision,
            "provider_advice": asdict(advice) if advice is not None else None,
        }
    finally:
        # Policy/advice failures must not retain a failed request through traceback locals.
        del data, failure, policy, invoke, values, raw_usage, context, advice, result, advisor
