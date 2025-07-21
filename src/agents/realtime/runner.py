"""Minimal realtime session implementation for voice agents."""

from __future__ import annotations

from ..run_context import TContext
from .agent import RealtimeAgent
from .config import (
    RealtimeRunConfig,
)
from .model import (
    RealtimeModel,
    RealtimeModelConfig,
)
from .openai_realtime import OpenAIRealtimeWebSocketModel
from .session import RealtimeSession


class RealtimeRunner:
    """A `RealtimeRunner` is the equivalent of `Runner` for realtime agents. It automatically
    handles multiple turns by maintaining a persistent connection with the underlying model
    layer.

    The session manages the local history copy, executes tools, runs guardrails and facilitates
    handoffs between agents.

    Since this code runs on your server, it uses WebSockets by default. You can optionally create
    your own custom model layer by implementing the `RealtimeModel` interface.
    """

    def __init__(
        self,
        starting_agent: RealtimeAgent,
        *,
        model: RealtimeModel | None = None,
        config: RealtimeRunConfig | None = None,
    ) -> None:
        """Initialize the realtime runner.

        Args:
            starting_agent: The agent to start the session with.
            context: The context to use for the session.
            model: The model to use. If not provided, will use a default OpenAI realtime model.
            config: Override parameters to use for the entire run.
        """
        self._starting_agent = starting_agent
        self._config = config
        self._model = model or OpenAIRealtimeWebSocketModel()

    async def run(
        self, *, context: TContext | None = None, model_config: RealtimeModelConfig | None = None
    ) -> RealtimeSession:
        """Start and returns a realtime session.

        Returns:
            RealtimeSession: A session object that allows bidirectional communication with the
            realtime model.

        Example:
            ```python
            runner = RealtimeRunner(agent)
            async with await runner.run() as session:
                await session.send_message("Hello")
                async for event in session:
                    print(event)
            ```
        """
        # Create and return the connection
        session = RealtimeSession(
            model=self._model,
            agent=self._starting_agent,
            context=context,
            model_config=model_config,
            run_config=self._config,
        )

        return session
