"""Finite native-model scripts with exact application-error identity."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock

from ._public import preserve_module_identity as _preserve_module_identity
from .models import ModelRequest, ModelResponse, ModelRetryAdviceRequest
from .retry import RetryAdvice


@dataclass(frozen=True)
class ModelError:
    """Raise this exact application error and optionally offer captured advice."""

    error: Exception
    retry_advice: RetryAdvice | None = None


class ScriptedModel:
    """Consume a finite script through the native model interface.

    Core owns attempts and retries. Responses are captured on construction and
    detached on return; errors retain their exact identity. Advice is static for
    each step. Use NativeModel directly for dynamic behavior.

    Advice records retain their exact request/error until requested. Unrequested
    records remain owned by this fixture, bounded by its original finite script.
    Use synthetic inputs: errors can independently retain traceback frames.
    """

    def __init__(self, steps: Iterable[ModelResponse | ModelError]) -> None:
        self._lock = Lock()
        self._steps: deque[ModelResponse | ModelError] = deque()
        self._advice: dict[str, tuple[ModelRequest, ModelError]] = {}
        for step in steps:
            if isinstance(step, ModelResponse):
                self._steps.append(deepcopy(step))
            elif isinstance(step, ModelError) and isinstance(step.error, Exception):
                if step.retry_advice is not None and not isinstance(step.retry_advice, RetryAdvice):
                    raise TypeError("Expected static RetryAdvice or None")
                self._steps.append(step)
            else:
                raise TypeError("Expected ModelResponse or ModelError")

    @property
    def remaining_steps(self) -> int:
        """Count unreserved steps, excluding model calls already admitted."""
        with self._lock:
            return len(self._steps)

    def assert_complete(self) -> None:
        """Require all steps reserved without discarding unrequested advice."""
        if self.remaining_steps:
            raise AssertionError("Unconsumed scripted model steps")

    def complete(self, request: ModelRequest) -> ModelResponse:
        with self._lock:
            if not self._steps:
                raise AssertionError("Unexpected scripted model call")
            step = self._steps.popleft()
            if isinstance(step, ModelError) and step.retry_advice is not None:
                if not request.attempt_id or request.attempt_id in self._advice:
                    raise ValueError("Scripted advice requires a distinct native attempt")
                self._advice[request.attempt_id] = (request, step)
        if isinstance(step, ModelError):
            raise step.error
        return deepcopy(step)

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> RetryAdvice | None:
        with self._lock:
            occurrence = self._advice.get(request.attempt_id)
            if (
                occurrence is None
                or occurrence[0] is not request.request
                or occurrence[1].error is not request.error
            ):
                return None
            del self._advice[request.attempt_id]
            return occurrence[1].retry_advice


_preserve_module_identity(globals(), "agents.testing")
