from __future__ import annotations

from dataclasses import dataclass
from typing import Generic

from ..agent import Agent
from ..exceptions import UserError
from ..items import TResponseInputItem
from ..result import RunResult, RunResultStreaming
from ..run_config import RunConfig
from ..run_context import RunContextWrapper, TContext
from ..run_internal.agent_bindings import (
    AgentBindings,
    bind_execution_agent,
    bind_public_agent,
)
from ..run_state import RunState
from .runtime_agent_preparation import clone_capabilities, prepare_sandbox_agent
from .runtime_session_manager import SandboxRuntimeSessionManager
from .sandbox_agent import SandboxAgent
from .session.base_sandbox_session import BaseSandboxSession


@dataclass
class _SandboxPreparedAgent(Generic[TContext]):
    bindings: AgentBindings[TContext]
    input: str | list[TResponseInputItem]


class SandboxRuntime(Generic[TContext]):
    def __init__(
        self,
        *,
        starting_agent: Agent[TContext],
        run_config: RunConfig | None,
        run_state: RunState[TContext] | None,
    ) -> None:
        self._sandbox_config = run_config.sandbox if run_config is not None else None
        self._session_manager = SandboxRuntimeSessionManager(
            starting_agent=starting_agent,
            sandbox_config=self._sandbox_config,
            run_state=run_state,
        )
        self._prepared_agents: dict[int, Agent[TContext]] = {}
        self._prepared_sessions: dict[int, BaseSandboxSession] = {}

    @property
    def enabled(self) -> bool:
        return self._session_manager.enabled

    @property
    def current_session(self) -> BaseSandboxSession | None:
        return self._session_manager.current_session

    def apply_result_metadata(self, result: RunResult | RunResultStreaming) -> None:
        session = self.current_session
        result._sandbox_session = session
        if isinstance(result, RunResultStreaming):

            async def _cleanup_and_store() -> None:
                try:
                    payload = await self.cleanup()
                    result._sandbox_resume_state = payload
                finally:
                    result._sandbox_session = None

            result._sandbox_cleanup = _cleanup_and_store

    def assert_agent_supported(self, agent: Agent[TContext]) -> None:
        if isinstance(agent, SandboxAgent) and self._sandbox_config is None:
            raise UserError("SandboxAgent execution requires `RunConfig(sandbox=...)`")

    async def prepare_agent(
        self,
        *,
        current_agent: Agent[TContext],
        current_input: str | list[TResponseInputItem],
        context_wrapper: RunContextWrapper[TContext],
        is_resumed_state: bool,
    ) -> _SandboxPreparedAgent[TContext]:
        self.assert_agent_supported(current_agent)
        if not isinstance(current_agent, SandboxAgent):
            return _SandboxPreparedAgent(
                bindings=bind_public_agent(current_agent),
                input=current_input,
            )

        self._session_manager.acquire_agent(current_agent)
        prepared_agent = self._prepared_agents.get(id(current_agent))
        prepared_capabilities = clone_capabilities(current_agent.capabilities)
        session = await self._session_manager.ensure_session(
            agent=current_agent,
            capabilities=prepared_capabilities,
            is_resumed_state=is_resumed_state,
        )
        if prepared_agent is not None and self._prepared_sessions.get(id(current_agent)) is session:
            return _SandboxPreparedAgent(
                bindings=bind_execution_agent(
                    public_agent=current_agent,
                    execution_agent=prepared_agent,
                ),
                input=current_input,
            )

        prepared_agent = prepare_sandbox_agent(
            agent=current_agent,
            session=session,
            capabilities=prepared_capabilities,
        )
        self._prepared_agents[id(current_agent)] = prepared_agent
        self._prepared_sessions[id(current_agent)] = session
        return _SandboxPreparedAgent(
            bindings=bind_execution_agent(
                public_agent=current_agent,
                execution_agent=prepared_agent,
            ),
            input=current_input,
        )

    async def cleanup(self) -> dict[str, object] | None:
        try:
            return await self._session_manager.cleanup()
        finally:
            self._prepared_agents.clear()
            self._prepared_sessions.clear()
