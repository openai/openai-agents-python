from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from .agent import Agent
from .items import TResponseInputItem
from .models.multi_provider import MultiProvider
from .models.openai_provider import OpenAIProvider
from .result import RunResult, RunResultStreaming
from .run import Runner
from .run_config import RunConfig
from .run_state import RunState


@dataclass(frozen=True)
class ResponsesWebSocketSession:
    """Helper that pins runs to a shared OpenAI websocket-capable provider."""

    provider: OpenAIProvider
    run_config: RunConfig

    def __post_init__(self) -> None:
        self._validate_provider_alignment()

    def _validate_provider_alignment(self) -> MultiProvider:
        model_provider = self.run_config.model_provider
        if not isinstance(model_provider, MultiProvider):
            raise TypeError(
                "ResponsesWebSocketSession.run_config.model_provider must be a MultiProvider."
            )
        if model_provider.openai_provider is not self.provider:
            raise ValueError(
                "ResponsesWebSocketSession provider and run_config.model_provider are not aligned."
            )
        return model_provider

    async def aclose(self) -> None:
        """Close cached provider model resources (including websocket connections)."""
        await self._validate_provider_alignment().aclose()

    def _prepare_runner_kwargs(self, method_name: str, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_provider_alignment()
        if "run_config" in kwargs:
            raise ValueError(
                f"Do not pass `run_config` to ResponsesWebSocketSession.{method_name}()."
            )
        runner_kwargs = dict(kwargs)
        runner_kwargs["run_config"] = self.run_config
        return runner_kwargs

    async def run(
        self,
        starting_agent: Agent[Any],
        input: str | list[TResponseInputItem] | RunState[Any],
        **kwargs: Any,
    ) -> RunResult:
        """Call ``Runner.run`` with the session's shared ``RunConfig``."""
        runner_kwargs = self._prepare_runner_kwargs("run", kwargs)
        return await Runner.run(starting_agent, input, **runner_kwargs)

    def run_streamed(
        self,
        starting_agent: Agent[Any],
        input: str | list[TResponseInputItem] | RunState[Any],
        **kwargs: Any,
    ) -> RunResultStreaming:
        """Call ``Runner.run_streamed`` with the session's shared ``RunConfig``."""
        runner_kwargs = self._prepare_runner_kwargs("run_streamed", kwargs)
        return Runner.run_streamed(starting_agent, input, **runner_kwargs)


@asynccontextmanager
async def responses_websocket_session(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    websocket_base_url: str | None = None,
    organization: str | None = None,
    project: str | None = None,
) -> AsyncIterator[ResponsesWebSocketSession]:
    """Create a shared OpenAI Responses websocket session for multiple Runner calls.

    The helper returns a session object that injects one shared ``RunConfig`` backed by a
    websocket-configured ``MultiProvider`` with one shared ``OpenAIProvider``. This preserves
    prefix-based model routing (for example ``openai/gpt-4.1``) while keeping websocket
    connections warm across turns and nested agent-as-tool runs that inherit the same
    ``run_config``.

    Drain or close streamed iterators before the context exits. Exiting the context while a
    websocket request is still in flight may force-close the shared connection.
    """
    model_provider = MultiProvider(
        openai_api_key=api_key,
        openai_base_url=base_url,
        openai_websocket_base_url=websocket_base_url,
        openai_organization=organization,
        openai_project=project,
        openai_use_responses=True,
        openai_use_responses_websocket=True,
    )
    provider = model_provider.openai_provider
    session = ResponsesWebSocketSession(
        provider=provider,
        run_config=RunConfig(model_provider=model_provider),
    )
    try:
        yield session
    finally:
        await session.aclose()


__all__ = ["ResponsesWebSocketSession", "responses_websocket_session"]
