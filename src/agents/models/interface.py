from __future__ import annotations

import abc
import asyncio
import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..agent_output import AgentOutputSchema
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..tool import Tool

if TYPE_CHECKING:
    from ..model_settings import ModelSettings


class ModelTracing(enum.Enum):
    DISABLED = 0
    """Tracing is disabled entirely."""

    ENABLED = 1
    """Tracing is enabled, and all data is included."""

    ENABLED_WITHOUT_DATA = 2
    """Tracing is enabled, but inputs/outputs are not included."""

    def is_disabled(self) -> bool:
        return self == ModelTracing.DISABLED

    def include_data(self) -> bool:
        return self == ModelTracing.ENABLED


@dataclass
class ModelRetrySettings:
    """Settings for retrying model calls on failure.

    This class helps manage backoff and retry logic when API calls fail.
    """

    max_retries: int = 3
    """Maximum number of retries to attempt."""

    initial_backoff_seconds: float = 1.0
    """Initial backoff time in seconds before the first retry."""

    max_backoff_seconds: float = 30.0
    """Maximum backoff time in seconds between retries."""

    backoff_multiplier: float = 2.0
    """Multiplier for backoff time after each retry."""

    retryable_status_codes: list[int] = field(default_factory=lambda: [429, 500, 502, 503, 504])
    """HTTP status codes that should trigger a retry."""

    async def execute_with_retry(
        self,
        operation: Callable[[], Any],
        should_retry: Callable[[Exception], bool] | None = None
    ) -> Any:
        """Execute an operation with retry logic.

        Args:
            operation: Async function to execute
            should_retry: Optional function to determine if an exception should trigger a retry

        Returns:
            The result of the operation if successful

        Raises:
            The last exception encountered if all retries fail
        """
        last_exception = None
        backoff = self.initial_backoff_seconds

        for attempt in range(self.max_retries + 1):
            try:
                return await operation()
            except Exception as e:
                last_exception = e

                # Check if we should retry
                if attempt >= self.max_retries:
                    break

                should_retry_exception = True
                if should_retry is not None:
                    should_retry_exception = should_retry(e)

                if not should_retry_exception:
                    break

                # Wait before retrying
                await asyncio.sleep(backoff)
                backoff = min(backoff * self.backoff_multiplier, self.max_backoff_seconds)

        if last_exception:
            raise last_exception

        # This should never happen, but just in case
        raise RuntimeError("Retry logic failed in an unexpected way")


class Model(abc.ABC):
    """The base interface for calling an LLM."""

    @abc.abstractmethod
    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
    ) -> ModelResponse:
        """Get a response from the model.

        Args:
            system_instructions: The system instructions to use.
            input: The input items to the model, in OpenAI Responses format.
            model_settings: The model settings to use.
            tools: The tools available to the model.
            output_schema: The output schema to use.
            handoffs: The handoffs available to the model.
            tracing: Tracing configuration.

        Returns:
            The full model response.
        """
        pass

    @abc.abstractmethod
    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchema | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """Stream a response from the model.

        Args:
            system_instructions: The system instructions to use.
            input: The input items to the model, in OpenAI Responses format.
            model_settings: The model settings to use.
            tools: The tools available to the model.
            output_schema: The output schema to use.
            handoffs: The handoffs available to the model.
            tracing: Tracing configuration.

        Returns:
            An iterator of response stream events, in OpenAI Responses format.
        """
        pass


class ModelProvider(abc.ABC):
    """The base interface for a model provider.

    Model provider is responsible for looking up Models by name.
    """

    @abc.abstractmethod
    def get_model(self, model_name: str | None) -> Model:
        """Get a model by name.

        Args:
            model_name: The name of the model to get.

        Returns:
            The model.
        """
