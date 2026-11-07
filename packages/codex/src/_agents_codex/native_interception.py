"""Tool-local decisions for calls that still execute in Codex."""

from __future__ import annotations

import asyncio as asyncio
import json as json
from collections.abc import Callable as Callable, Coroutine as Coroutine
from copy import deepcopy as deepcopy
from dataclasses import dataclass as dataclass, field as field, replace as replace
from functools import partial as partial
from types import MappingProxyType as MappingProxyType
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Literal as Literal,
    TypeAlias as TypeAlias,
)

from ._compat import cancel_once as cancel_once
from ._lifecycle import _join as _join
from ._public import preserve_module_identity as _preserve_module_identity

if TYPE_CHECKING:
    from .executor import Environment
    from .mcp import _MCP
    from .native_agents import NativeAgents
    from .session import Session, _CallbackState, _Submission
    from .telemetry import SpanContext
    from .turn import Turn


@dataclass(frozen=True)
class StopTurn:
    """Stop the exact owning turn from an admitted before policy or step hook.

    Return this from an Environment approval or arguments transform, or from
    an MCP approval, or a root or child before_step or before_complete callback.
    A completion decision aborts instead of accepting the proposed answer. A step decision
    prevents its pending model request and joins owned cleanup; earlier effects
    are not rolled back. The SDK never includes the reason in native history,
    observations, or the public failure. False is a local denial for direct
    Environment calls and for MCP calls. Generated-code Environment approval
    False also stops and joins its owning turn.
    """

    reason: str = field(repr=False)


@dataclass(frozen=True)
class Allow:
    """Use the admitted input without modification."""


@dataclass(frozen=True)
class Deny:
    """Return a local function-tool failure without invoking the callback."""

    reason: str = field(repr=False)


@dataclass(frozen=True)
class ReplaceArguments:
    """Use this input for the selected callback or an admitted native MCP call.

    StdioMCP and HttpMCP document the model transport and ownership restrictions.
    """

    arguments: Any = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeToolCall:
    """Native call metadata supplied to collaboration approval before dispatch.

    Input is the original JSON string or freeform input.
    """

    thread_id: str
    turn_id: str
    call_id: str | None
    name: str
    namespace: str | None
    phase: Literal["before", "after"]
    route: Literal["direct", "extension", "programmatic_tool_calling", "mcp"]
    boundary: Literal["pre_dispatch", "executor_return", "nested_return", "cell_return"]
    tool_kind: Literal["function_json", "function", "freeform", "tool_search"]
    input: Any = field(repr=False)
    result: Any = field(repr=False)
    outer_call_id: str | None = None
    cell_id: str | None = None
    runtime_tool_call_id: str | None = None
    agent_role: str | None = None
    model: str | None = None


_Descriptor: TypeAlias = "Environment[Any] | NativeAgents | _MCP[Any]"


def _has_interceptors(session: Session[Any, Any]) -> bool:
    return (
        session._executor is not None
        and session._executor._has_callbacks
        or session._native_agents is not None
        and session._native_agents.approval is not None
        or any(server._has_before for server in session._mcp_tools)
    )


@dataclass(eq=False, repr=False)
class _CellPolicyOwner:
    owner_id: int
    thread_id: str
    turn_id: str
    outer_call_id: str
    cell_id: str
    turn: Turn[Any]
    executor: Environment[Any] | None
    context: Any
    agent: Any
    live: bool = True
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    cleanup_failed: bool = False

    def release_context(self) -> None:
        self.executor = None
        self.context = None
        self.agent = None

    def matches(self, value: dict[str, Any]) -> bool:
        return all(
            value.get(key) == getattr(self, key)
            for key in ("thread_id", "turn_id", "outer_call_id", "cell_id")
        )


class _NativeInterception:
    """One native decision channel for environment policy and collaboration approval."""

    def __init__(self, session: Session[Any, Any]) -> None:
        self.session = session
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.cell_owners: dict[int, _CellPolicyOwner] = {}
        self.cleanup_error: BaseException | None = None
        self.reader: asyncio.Task[None] | None = None
        self.closing = False

    def start(self) -> None:
        self.session._native._enable_native_interception(3600.0, "thread_tree")
        self.refresh()
        self.reader = asyncio.create_task(self._read())

    def refresh(self) -> None:
        from .executor import Environment
        from .mcp import _tool_names
        from .native_agents import _COLLABORATION_TOOLS

        rules: list[dict[str, Any]] = []
        for descriptor in (self.session._executor, self.session._native_agents):
            if descriptor is None:
                continue
            names: list[dict[str, Any]]
            if isinstance(descriptor, Environment):
                if not descriptor._has_callbacks:
                    continue
                names = [{"namespace": "functions", "name": name} for name in descriptor.tools]
                # An approval can wait for application input. Transform-only
                # declarations retain the native queue deadline. Combined
                # declarations time transforms separately after approval.
                duration = None if descriptor.approval is not None else descriptor.policy_timeout
                scope = "thread_tree" if self.session._native_agents is not None else "root"
            else:
                if descriptor.approval is None:
                    continue
                names = [
                    {"namespace": "collaboration", "name": name} for name in _COLLABORATION_TOOLS
                ]
                duration = None
                scope = "thread_tree"
            rules.extend(
                {
                    **name,
                    "timeout": duration,
                    "scope": scope,
                    "result": isinstance(descriptor, Environment)
                    and descriptor.transform_result is not None,
                }
                for name in names
            )
        for server in self.session._mcp_tools:
            if server._has_before:
                rules.extend(
                    {
                        **name,
                        "timeout": None if server.approval is not None else 30.0,
                        "scope": "thread_tree",
                    }
                    for name in _tool_names((server,))
                )
        self.session._native._configure_native_interception(json.dumps(rules))

    def _select(self, call: NativeToolCall) -> _Descriptor | None:
        from .executor import Environment
        from .mcp import _MCP, _tool_names
        from .native_agents import _COLLABORATION_TOOLS

        session = self.session
        selected: _Descriptor | None = None
        if (
            session._executor is not None
            and call.namespace in (None, "", "functions")
            and call.name in session._executor.tools
        ):
            selected = session._executor
        elif (
            session._native_agents is not None
            and call.namespace == "collaboration"
            and call.name in _COLLABORATION_TOOLS
        ):
            selected = session._native_agents
        else:
            selected = next(
                (
                    server
                    for server in session._mcp_tools
                    if server._has_before
                    and {"namespace": call.namespace, "name": call.name} in _tool_names((server,))
                ),
                None,
            )
        if selected is None:
            return None
        if isinstance(selected, Environment):
            if not selected._has_callbacks:
                return None
            if call.thread_id != session.id and session._native_agents is None:
                return None
        elif isinstance(selected, _MCP):
            if not selected._has_before:
                return None
        elif selected.approval is None:
            return None
        return selected

    def _capture_cell_owner(self, value: dict[str, Any]) -> _CellPolicyOwner | None:
        owner_id = value.get("owner_id")
        keys = ("thread_id", "turn_id", "outer_call_id", "cell_id")
        if (
            type(owner_id) is not int
            or not 0 < owner_id < 2**64
            or any(not isinstance(value.get(key), str) for key in keys)
            or value["thread_id"] != self.session.id
        ):
            return None
        existing = self.cell_owners.get(owner_id)
        if existing is not None:
            return existing if existing.live and existing.matches(value) else None
        state = self.session._active
        executor = self.session._executor
        if (
            self.closing
            or self.session._closing
            or state is None
            or state.handle is None
            or state.stopping
            or state.failure.done()
            or state.terminal.done()
            or state.handle.turn_id != value["turn_id"]
            or executor is None
            or not executor._has_callbacks
        ):
            return None
        # One lease may have been announced lazily before the initial return.
        # Different native leases must not alias the same actual cell.
        if any(owner.matches(value) for owner in self.cell_owners.values()):
            return None
        owner = _CellPolicyOwner(
            owner_id=owner_id,
            thread_id=value["thread_id"],
            turn_id=value["turn_id"],
            outer_call_id=value["outer_call_id"],
            cell_id=value["cell_id"],
            turn=state.handle,
            executor=executor,
            context=self.session._context,
            agent=self.session._agent,
        )
        self.cell_owners[owner_id] = owner
        return owner

    def _cell_owner(
        self, request: dict[str, Any], *, capture: bool = False
    ) -> _CellPolicyOwner | None:
        if not isinstance(request.get("runtime_tool_call_id"), str):
            return None
        owner_id = request.get("cell_owner_id")
        if owner_id is not None:
            if type(owner_id) is not int:
                return None
            owner = self.cell_owners.get(owner_id)
            if owner is None and capture and request["phase"] == "before":
                owner = self._capture_cell_owner({**request, "owner_id": owner_id})
            return owner if owner is not None and owner.live and owner.matches(request) else None
        if request["phase"] != "after" or request["boundary"] != "executor_return":
            return None
        owners = [
            owner for owner in self.cell_owners.values() if owner.live and owner.matches(request)
        ]
        return owners[0] if len(owners) == 1 else None

    async def _run_cell_policy(
        self, nonce: int, request: dict[str, Any], initial: _Submission | None
    ) -> None:
        native = self.session._native
        if self.closing or self.session._closing or not native._check_native_interception(nonce):
            return
        owner = self._cell_owner(request)
        if owner is None and request["phase"] == "before" and initial is not None:
            # The first native call may arrive before _submit publishes its
            # handle. This task waits; the sole channel reader keeps moving.
            await asyncio.wait((initial.accepted,))
            if (
                self.closing
                or self.session._closing
                or not native._check_native_interception(nonce)
            ):
                return
            # Initial-return acknowledgement may have bound the cell and let
            # the root finish while this task waited for acceptance.
            owner = self._cell_owner(request)
            if owner is None and self.session._active is initial:
                owner = self._cell_owner(request, capture=True)
        if owner is None:
            return
        task = asyncio.current_task()
        assert task is not None
        owner.tasks.add(task)
        try:
            await self.session._invoke_executor_policy(None, nonce, request, cell_owner=owner)
        finally:
            owner.tasks.discard(task)
            if not owner.live and not owner.tasks:
                owner.release_context()
                if not owner.cleanup_failed and self.cell_owners.get(owner.owner_id) is owner:
                    self.cell_owners.pop(owner.owner_id)

    def _retire_cell_owner(self, owner_id: Any) -> None:
        if type(owner_id) is not int:
            return
        owner = self.cell_owners.get(owner_id)
        if owner is not None:
            owner.live = False
            for task in tuple(owner.tasks):
                cancel_once(task)
            if not owner.tasks:
                owner.release_context()
                if not owner.cleanup_failed:
                    self.cell_owners.pop(owner_id, None)

    def owns_cell(self, owner: _CellPolicyOwner) -> bool:
        return not self.closing and owner.live and self.cell_owners.get(owner.owner_id) is owner

    def _start_task(self, nonce: int, task: asyncio.Task[None]) -> None:
        native = self.session._native
        self.tasks[nonce] = task

        def finished(_task: asyncio.Task[None]) -> None:
            native._resolve_native_interception(nonce, '{"action":"deny"}')
            native._release_native_interception(nonce)
            self.tasks.pop(nonce, None)

        task.add_done_callback(finished)

    async def _bind_cell_owner(self, nonce: int, value: dict[str, Any]) -> None:
        native = self.session._native
        if not isinstance(value, dict):
            return
        state = self.session._active
        if state is not None:
            await asyncio.wait((state.accepted,))
        if not native._check_native_interception(nonce) or not native._handoff_native_interception(
            nonce
        ):
            return
        if self._capture_cell_owner(value) is not None:
            native._resolve_native_interception(nonce, '{"action":"continue"}')

    def cell_cleanup_failed(self) -> None:
        from .session import NativeError

        if self.cleanup_error is None:
            self.cleanup_error = NativeError("Native cell policy cleanup failed")
        self.session._broken = True
        self.session._native._revoke_native_interception()

    async def join_cell_callbacks(self, turn: Turn[Any] | None = None) -> None:
        # Called only after actual non-normal native retirement. A normal root
        # result does not retire its admitted cells or their callback tasks.
        owners = [
            owner for owner in self.cell_owners.values() if turn is None or owner.turn is turn
        ]
        tasks = {task for owner in owners for task in owner.tasks}
        for owner in owners:
            self._retire_cell_owner(owner.owner_id)
        from .session import NativeError

        try:
            await _join(asyncio.gather(*tasks, return_exceptions=True))
        finally:
            if self.cleanup_error is not None:
                raise self.cleanup_error
            if any(owner.cleanup_failed for owner in owners):
                raise NativeError("Executor policy cleanup failed")

    async def _read(self) -> None:
        from .executor import Environment
        from .mcp import _MCP
        from .session import NativeError
        from .telemetry import _create_task, _retain_context

        native = self.session._native
        try:
            while not self.closing:
                notice = json.loads(await native._next_native_interception())
                if notice is None:
                    return
                if "cell_owner_retired" in notice:
                    self._retire_cell_owner(notice["cell_owner_retired"])
                    continue
                if "cell_owner" in notice:
                    nonce = notice["nonce"]
                    self._start_task(
                        nonce,
                        asyncio.create_task(self._bind_cell_owner(nonce, notice["cell_owner"])),
                    )
                    continue
                if "cancel" in notice:
                    task = self.tasks.get(notice["cancel"])
                    if task is not None:
                        cancel_once(task)
                    continue
                nonce = notice["nonce"]
                dispatched = False
                tracing = None
                try:
                    request = notice["request"]
                    name = request["tool_name"]
                    call = NativeToolCall(
                        **{
                            key: value
                            for key, value in request.items()
                            if key
                            not in (
                                "tool_name",
                                "instance",
                                "route",
                                "cell_owner_id",
                                "tracing_context",
                            )
                        },
                        name=name["name"],
                        namespace=name["namespace"],
                        route=(
                            "programmatic_tool_calling"
                            if request["route"] == "code_mode"
                            else request["route"]
                        ),
                    )
                    descriptor = self._select(call)
                    if descriptor is None:
                        native._resolve_native_interception(nonce, '{"action":"continue"}')
                        continue
                    if isinstance(descriptor, _MCP):
                        # Approval applies before effect. The separate MCP
                        # result owner still controls delivery after execution.
                        if call.phase != "before":
                            native._resolve_native_interception(nonce, '{"action":"continue"}')
                            continue
                    if not native._check_native_interception(nonce):
                        continue
                    if context := request.get("tracing_context"):
                        try:
                            tracing = _retain_context(context)
                        except RuntimeError:
                            if self.closing or not native._check_native_interception(nonce):
                                continue
                            raise
                        request["tracing_context"] = tracing._handle
                    if (
                        isinstance(descriptor, Environment)
                        and call.thread_id == self.session.id
                        and request["route"] == "code_mode"
                    ):
                        # Cell work is channel-owned even if its callback begins
                        # before the original root completes normally.
                        self._start_task(
                            nonce,
                            _create_task(
                                self._dispatch(
                                    nonce,
                                    tracing,
                                    partial(
                                        self._run_cell_policy, nonce, request, self.session._active
                                    ),
                                )
                            ),
                        )
                        dispatched = True
                        continue
                    state: _CallbackState | None
                    if call.thread_id == self.session.id:
                        state = self.session._active
                    else:
                        state = self.session._child_callback_state(
                            call.thread_id, request["instance"], call.turn_id
                        )
                    if state is None or state.stopping or state.failure.done():
                        continue
                    if isinstance(descriptor, Environment):
                        invoke = partial(
                            self.session._invoke_executor_policy, state, nonce, request
                        )
                    else:
                        invoke = partial(self._invoke, state, nonce, call, descriptor)
                    task = _create_task(self._dispatch(nonce, tracing, invoke))
                    self._start_task(nonce, task)
                    state.add_callback(task)
                    dispatched = True
                finally:
                    tracing = None
                    if not dispatched:
                        native._resolve_native_interception(nonce, '{"action":"deny"}')
                        native._release_native_interception(nonce)
        except asyncio.CancelledError:
            raise
        except BaseException:
            native._revoke_native_interception()
            self.session._broken = True
            state = self.session._active
            if state is not None and not state.failure.done():
                state.failure.set_result(NativeError("Native tool interception failed"))
        finally:
            native._finish_native_interception_reader()

    async def _dispatch(
        self,
        nonce: int,
        tracing: SpanContext | None,
        invoke: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        from .telemetry import _activate, _clear_current

        if self.closing or not self.session._native._check_native_interception(nonce):
            return
        with _clear_current(), _activate(tracing._handle if tracing is not None else None):
            await invoke()

    async def _invoke(
        self,
        state: _CallbackState,
        nonce: int,
        call: NativeToolCall,
        descriptor: NativeAgents | _MCP[Any],
    ) -> None:
        from ._function_tools import ToolContext
        from .mcp import _MCP, MCPCallContext, _tool_names
        from .session import (
            NativeError,
            _call_callback,
            _ChildCallbacks,
            _Invocation,
            _invocation,
            _Submission,
        )
        from .tools import ToolCallInfo, ToolRef

        session = self.session
        native = session._native
        scope = _Invocation(session)
        token = _invocation.set(scope)

        def admitted() -> bool:
            return (
                not self.closing
                and not session._closing
                and (isinstance(state, _ChildCallbacks) or session._active is state)
                and state.handle is not None
                and state.handle.turn_id == call.turn_id
                and not state.stopping
                and not state.failure.done()
                and native._check_native_interception(nonce)
            )

        reply = '{"action":"deny"}'
        try:
            if isinstance(state, _Submission):
                await asyncio.wait((state.accepted,))
            if (
                not admitted()
                or (
                    call.call_id is None
                    and (not isinstance(descriptor, _MCP) or call.runtime_tool_call_id is None)
                )
                or not native._handoff_native_interception(nonce)
            ):
                return
            assert state.handle is not None
            info = ToolCallInfo(
                tool=ToolRef(call.name, call.namespace),
                session_id=session.id,
                turn_id=call.turn_id,
                call_id=call.call_id,
                child_id=state.handle.child_id if isinstance(state, _ChildCallbacks) else None,
                child_incarnation=(
                    state.handle.instance_id if isinstance(state, _ChildCallbacks) else None
                ),
                role=call.agent_role,
                model=call.model,
                route="code" if call.route == "programmatic_tool_calling" else "direct",
                outer_call_id=call.outer_call_id,
                cell_id=call.cell_id,
                nested_call_id=call.runtime_tool_call_id,
            )
            if isinstance(descriptor, _MCP):
                assert call.namespace is not None
                # _select resolved exactly one descriptor and _tool_names
                # rejected projected collisions before admission.
                index = _tool_names((descriptor,)).index(
                    {"namespace": call.namespace, "name": call.name}
                )
                original_arguments = (
                    json.loads(call.input) if isinstance(call.input, str) else deepcopy(call.input)
                )
                if not isinstance(original_arguments, dict):
                    return
                effective_arguments = original_arguments
                submission = state.handle

                def call_context() -> MCPCallContext[Any]:
                    return MCPCallContext(
                        session._context,
                        submission,
                        info,
                        descriptor.name,
                        descriptor.tools[index],
                        original_arguments=MappingProxyType(deepcopy(original_arguments)),
                        effective_arguments=MappingProxyType(deepcopy(effective_arguments)),
                    )

                effective = call
                decision: dict[str, Any] = {"action": "continue"}
                if descriptor.before is not None:
                    loop = asyncio.get_running_loop()
                    task = asyncio.current_task()
                    assert task is not None
                    cutoff = loop.time() + 30.0

                    def expire(cancel_body: bool = True) -> None:
                        if admitted():
                            state.failure.set_result(NativeError("Native MCP before policy failed"))
                            if cancel_body:
                                cancel_once(task)

                    timer = loop.call_at(cutoff, expire)
                    try:
                        # Allow must leave approval observing the native input.
                        value = await _call_callback(
                            descriptor.before,
                            admitted,
                            call_context(),
                            replace(call, input=deepcopy(call.input)),
                        )
                    finally:
                        timer.cancel()
                        if loop.time() >= cutoff:
                            expire(cancel_body=False)
                    if not admitted():
                        return
                    if isinstance(value, StopTurn):
                        await session._stop_policy_turn(state, nonce)
                        return
                    if isinstance(value, ReplaceArguments):
                        # Freeze the value before approval. Never trust a retained
                        # callback reference to leave it unchanged while awaiting.
                        replacement = json.loads(json.dumps(value.arguments, allow_nan=False))
                        if not isinstance(replacement, dict):
                            return
                        decision = {"action": "replace", "value": replacement}
                        effective_arguments = replacement
                        effective = replace(
                            call,
                            input=(
                                json.dumps(replacement)
                                if isinstance(call.input, str)
                                else deepcopy(replacement)
                            ),
                        )
                    elif not isinstance(value, Allow):
                        return
                value = (
                    True
                    if descriptor.approval is None
                    else await _call_callback(
                        descriptor.approval, admitted, call_context(), effective
                    )
                )
                if admitted():
                    if isinstance(value, StopTurn):
                        await session._stop_policy_turn(state, nonce)
                    elif type(value) is bool and value:
                        reply = json.dumps(decision, allow_nan=False)
            else:
                approval = descriptor.approval
                assert approval is not None
                assert call.call_id is not None
                collab_context = ToolContext(
                    session._context, state.handle, call.call_id, call=info
                )
                value = await _call_callback(approval, admitted, collab_context, call)
                if admitted() and type(value) is bool and value:
                    reply = '{"action":"continue"}'
        except BaseException:
            # Callback errors and rejected values must never escape into native
            # history or public error chains.
            reply = '{"action":"deny"}'
        finally:
            native._resolve_native_interception(nonce, reply)
            scope.active = False
            _invocation.reset(token)

    async def close(self) -> None:
        self.closing = True
        native = self.session._native
        native._revoke_native_interception()
        if self.reader is not None:
            cancel_once(self.reader)
            await asyncio.gather(self.reader, return_exceptions=True)
        tasks = tuple(self.tasks.values())
        for task in tasks:
            cancel_once(task)
        await asyncio.gather(*tasks, return_exceptions=True)
        callback_failed = any(owner.cleanup_failed for owner in self.cell_owners.values())
        for owner in self.cell_owners.values():
            owner.live = False
            owner.release_context()
        self.cell_owners.clear()
        if self.cleanup_error is not None:
            raise self.cleanup_error
        if callback_failed:
            from .session import NativeError

            raise NativeError("Executor policy cleanup failed")


_preserve_module_identity(globals(), "agents.native_interception")
