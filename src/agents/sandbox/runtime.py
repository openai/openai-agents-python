from __future__ import annotations

import logging
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Generic, cast

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
from ..tracing import custom_span, get_current_trace
from .capabilities import Capability
from .capabilities.memory import Memory
from .memory.manager import SandboxMemoryGenerationManager, get_or_create_memory_generation_manager
from .memory.rollouts import (
    RolloutTerminalMetadata,
    build_rollout_payload,
)
from .runtime_agent_preparation import (
    clone_capabilities,
    prepare_sandbox_agent,
    prepare_sandbox_input,
)
from .runtime_session_manager import SandboxRuntimeSessionManager
from .sandbox_agent import SandboxAgent
from .session.base_sandbox_session import BaseSandboxSession
from .types import User

logger = logging.getLogger(__name__)


@dataclass
class _SandboxPreparedAgent(Generic[TContext]):
    bindings: AgentBindings[TContext]
    input: str | list[TResponseInputItem]


def _supports_trace_spans() -> bool:
    current_trace = get_current_trace()
    return current_trace is not None and current_trace.export() is not None


def _stream_memory_input_override(
    result: RunResultStreaming,
) -> list[TResponseInputItem] | None:
    if (
        result._conversation_id is not None
        or result._previous_response_id is not None
        or result._auto_previous_response_id
    ):
        return None
    return result._original_input_for_persistence


class SandboxRuntime(Generic[TContext]):
    def __init__(
        self,
        *,
        starting_agent: Agent[TContext],
        run_config: RunConfig | None,
        rollout_id: str | None = None,
        run_state: RunState[TContext] | None,
    ) -> None:
        self._sandbox_config = run_config.sandbox if run_config is not None else None
        self._run_config_model = run_config.model if run_config is not None else None
        # The runner resolves this before constructing the runtime. It can be None only when
        # sandbox is disabled or tests instantiate the runtime directly.
        self._rollout_id = rollout_id
        self._active_memory_capability: Memory | None = None
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
                    try:
                        await self.enqueue_memory_result(
                            result,
                            input_override=_stream_memory_input_override(result),
                        )
                    except Exception as error:
                        logger.warning(
                            "Failed to enqueue sandbox memory after streamed run: %s", error
                        )
                    payload = await self.cleanup()
                    result._sandbox_resume_state = payload
                finally:
                    result._sandbox_session = None

            result._sandbox_cleanup = _cleanup_and_store

    def assert_agent_supported(self, agent: Agent[TContext]) -> None:
        if isinstance(agent, SandboxAgent) and self._sandbox_config is None:
            raise UserError("SandboxAgent execution requires `RunConfig(sandbox=...)`")

    async def enqueue_memory_result(
        self,
        result: RunResult | RunResultStreaming,
        *,
        exception: BaseException | None = None,
        input_override: str | list[TResponseInputItem] | None = None,
    ) -> None:
        manager = self._memory_generation_manager()
        if manager is None or self._rollout_id is None:
            return
        await manager.enqueue_result(
            result,
            exception=exception,
            input_override=input_override,
            rollout_id=self._rollout_id,
        )

    async def enqueue_memory_payload(
        self,
        *,
        input: str | list[TResponseInputItem],
        new_items: list[Any],
        final_output: object,
        interruptions: list[Any],
        terminal_metadata: RolloutTerminalMetadata,
    ) -> None:
        manager = self._memory_generation_manager()
        if manager is None or self._rollout_id is None:
            return
        payload = build_rollout_payload(
            input=input,
            new_items=new_items,
            final_output=final_output,
            interruptions=interruptions,
            terminal_metadata=terminal_metadata,
        )
        await manager.enqueue_rollout_payload(
            payload,
            rollout_id=self._rollout_id,
        )

    def _memory_generation_manager(self) -> SandboxMemoryGenerationManager | None:
        session = self.current_session
        if (
            session is None
            or self._active_memory_capability is None
            or self._active_memory_capability.generate is None
        ):
            return None
        return get_or_create_memory_generation_manager(
            session=session,
            memory=self._active_memory_capability,
        )

    def _set_active_memory_capability(self, agent: Agent[TContext]) -> None:
        self._active_memory_capability = _get_memory_capability(agent)

    async def prepare_agent(
        self,
        *,
        current_agent: Agent[TContext],
        current_input: str | list[TResponseInputItem],
        context_wrapper: RunContextWrapper[TContext],
        is_resumed_state: bool,
    ) -> _SandboxPreparedAgent[TContext]:
        self.assert_agent_supported(current_agent)
        self._set_active_memory_capability(current_agent)
        if not isinstance(current_agent, SandboxAgent):
            return _SandboxPreparedAgent(
                bindings=bind_public_agent(current_agent),
                input=current_input,
            )

        span_cm = (
            custom_span(
                "sandbox.prepare_agent",
                data={"agent_name": current_agent.name},
            )
            if _supports_trace_spans()
            else nullcontext(None)
        )
        with span_cm:
            self._session_manager.acquire_agent(current_agent)
            prepared_agent = self._prepared_agents.get(id(current_agent))
            prepared_capabilities = clone_capabilities(current_agent.capabilities)
            session = await self._session_manager.ensure_session(
                agent=current_agent,
                capabilities=prepared_capabilities,
                is_resumed_state=is_resumed_state,
            )
            if (
                prepared_agent is not None
                and self._prepared_sessions.get(id(current_agent)) is session
            ):
                # Reuse the cached execution agent's bound capability instances so context
                # processing can depend on live session state and preserve per-run state.
                _bind_capability_run_as(
                    cast(SandboxAgent[TContext], prepared_agent).capabilities,
                    _coerce_run_as_user(current_agent.run_as),
                )
                prepared_input = prepare_sandbox_input(
                    cast(SandboxAgent[TContext], prepared_agent).capabilities,
                    current_input,
                )
                return _SandboxPreparedAgent(
                    bindings=bind_execution_agent(
                        public_agent=current_agent,
                        execution_agent=prepared_agent,
                    ),
                    input=prepared_input,
                )

            # Bind before context processing: capabilities may inspect self.session while
            # transforming input.
            run_as = _coerce_run_as_user(current_agent.run_as)
            for capability in prepared_capabilities:
                capability.bind(session)
            _bind_capability_run_as(prepared_capabilities, run_as)
            prepared_input = prepare_sandbox_input(prepared_capabilities, current_input)
            prepared_agent = prepare_sandbox_agent(
                agent=current_agent,
                session=session,
                capabilities=prepared_capabilities,
                run_config_model=self._run_config_model,
            )
            self._prepared_agents[id(current_agent)] = prepared_agent
            self._prepared_sessions[id(current_agent)] = session
            return _SandboxPreparedAgent(
                bindings=bind_execution_agent(
                    public_agent=current_agent,
                    execution_agent=prepared_agent,
                ),
                input=prepared_input,
            )

    async def cleanup(self) -> dict[str, object] | None:
        should_trace_cleanup = self.current_session is not None or bool(self._prepared_sessions)
        span_cm = (
            custom_span("sandbox.cleanup", data={})
            if should_trace_cleanup and _supports_trace_spans()
            else nullcontext(None)
        )
        with span_cm:
            try:
                return await self._session_manager.cleanup()
            finally:
                self._prepared_agents.clear()
                self._prepared_sessions.clear()


def _get_memory_capability(agent: Agent[TContext]) -> Memory | None:
    if not isinstance(agent, SandboxAgent):
        return None
    for capability in agent.capabilities:
        if isinstance(capability, Memory):
            return capability
    return None


def _coerce_run_as_user(run_as: User | str | None) -> User | None:
    if run_as is None:
        return None
    if isinstance(run_as, User):
        return run_as
    return User(name=run_as)


def _bind_capability_run_as(capabilities: Sequence[Capability], user: User | None) -> None:
    for capability in capabilities:
        capability.bind_run_as(user)
