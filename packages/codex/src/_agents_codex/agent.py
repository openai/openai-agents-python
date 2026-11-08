"""Native agent declarations and one-shot convenience methods."""

from __future__ import annotations

import asyncio as asyncio
import importlib as importlib
import json as json
from collections.abc import AsyncIterator as AsyncIterator, Mapping as Mapping, Sequence as Sequence
from contextlib import (
    AbstractAsyncContextManager as AbstractAsyncContextManager,
    asynccontextmanager as asynccontextmanager,
)
from copy import copy as copy
from dataclasses import (
    FrozenInstanceError as FrozenInstanceError,
    dataclass as dataclass,
    field as field,
    replace as replace,
)
from pathlib import Path as Path
from threading import RLock as RLock
from types import MappingProxyType as MappingProxyType, TracebackType as TracebackType
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Generic as Generic,
    Literal,
    SupportsIndex as SupportsIndex,
    cast as cast,
    overload as overload,
)

from typing_extensions import TypedDict as TypedDict, TypeVar as TypeVar

from ._lifecycle import (
    _close_after_error as _close_after_error,
    _join as _join,
    _sync_run as _sync_run,
    _SyncRun as _SyncRun,
)
from ._public import preserve_module_identity as _preserve_module_identity
from ._trace_context import TraceContext as TraceContext, _trace_context as _trace_context
from .compaction import _BeforeCompact
from .completion import _BeforeComplete
from .model_context import _ContextTransform
from .usage import RunBudget

if TYPE_CHECKING:

    class _TracingOpeningOptions(TypedDict, total=False, closed=True):
        _tracing_context: str

    from ._function_tools import Tool
    from .canonical_storage import CustomStorage
    from .executor import Environment, EnvironmentSource
    from .input import UserInput
    from .native_agents import NativeAgents
    from .prepared_request import _PrepareRequest
    from .recovery import ReconciledCall
    from .schema import OutputSpec
    from .session import (
        Completed,
        Session,
        _BeforeStep,
        _EnvironmentLease,
        _InteractionHandler,
        _OpeningOwner,
    )
    from .state import LocalStorage, SessionState
    from .telemetry import TraceOptions
    from .tools import ToolRef
    from .turn import Turn

ContextT = TypeVar("ContextT", default=None)
OutputT = TypeVar("OutputT", default=str)
SessionContextT = TypeVar("SessionContextT")
NewOutputT = TypeVar("NewOutputT")
TextAgentT = TypeVar("TextAgentT", bound="Agent[Any, str]")
TypedAgentT = TypeVar("TypedAgentT", bound="Agent[Any, Any]")

_MAX_FUNCTION_CONCURRENCY_UNSET: Any = object()


class ModelSettings(TypedDict, total=False, closed=True):
    """Native model overrides. Omission preserves the native default.

    ``service_tier=None`` clears the override. These settings select model
    behavior; they do not grant tools, filesystem access, or network access.
    ``tool_choice`` controls whether the model may call tools in the request.
    Use ``"auto"`` to let the model decide, ``"none"`` to disable tool calls,
    or ``"required"`` to require a call to an offered tool. The default is
    ``"auto"``.
    """

    reasoning_effort: str
    reasoning_summary: str
    service_tier: str | None
    tool_choice: Literal["auto", "none", "required"]


def _copy_model_settings(settings: ModelSettings | None) -> ModelSettings:
    if settings is None:
        return {}
    unknown = settings.keys() - ModelSettings.__annotations__.keys()
    if unknown:
        raise ValueError(f"Unsupported model setting(s): {sorted(unknown)}")
    return settings.copy()


@dataclass(frozen=True, kw_only=True)
class CodexLogin:
    """Reuse an existing file-backed Codex login for a local native Session.

    Codex locates its usual login directory unless you supply ``codex_home``.
    Set ``account_id`` to require a specific ChatGPT account or workspace.
    The SDK checks the saved login when it opens a Session.

    This experimental mode may change or be absent from the public beta or
    general availability of the Agents SDK.
    """

    codex_home: str | Path | None = field(default=None, repr=False)
    account_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.codex_home is not None and (
            not isinstance(self.codex_home, (str, Path)) or not str(self.codex_home).strip()
        ):
            raise ValueError("codex_home must be a non-empty path")
        if self.account_id is not None and (
            not isinstance(self.account_id, str) or not self.account_id.strip()
        ):
            raise ValueError("account_id must be a non-empty string")


@dataclass(frozen=True, kw_only=True)
class CodexAccount:
    """The saved plan type and optional workspace ID at Session opening."""

    account_id: str | None
    plan_type: str


@dataclass(frozen=True, kw_only=True)
class NativeProvider:
    """A native Responses connection. Construction performs no I/O.

    For API-key authentication, an omitted key uses ``OPENAI_API_KEY`` when
    the Session opens. Credentials and connection URLs are excluded from the
    descriptor's representation.
    With ``supports_websockets=None``, the SDK permits WebSockets only when
    ``base_url=None``; an explicit ``base_url`` defaults to HTTP. Set ``True``
    to permit WebSockets for a compatible custom provider, or ``False`` to use
    HTTP. The native runtime selects the final transport when WebSockets are
    enabled and owns retries.

    Set ``response_mode="buffered"`` to request complete JSON responses from an
    endpoint that supports nonstreaming Responses. Model output events become
    available after the full response arrives, without token-level progress.
    Each HTTP response body is limited to 64 MiB and one million structural
    characters (``{}[],:``) outside strings. Exceeding either limit fails without
    retry. Each model response is buffered separately. Tool calls start after that
    response completes; events remain available while tools execute.
    Successful output uses the normal Session history and tool processing.
    Provider failures deliver no partial output; validated response metadata
    remains available in the turn record. This mode uses HTTP with
    API-key authentication and cannot be combined with
    ``supports_websockets=True`` or ``CodexLogin``. The default is ``"streaming"``.
    Native child agents are not supported in buffered mode.

    Set ``auth=CodexLogin()`` to use the saved Codex login over HTTP.
    Omit ``api_key`` and ``base_url`` in this experimental mode.

    Explicit providers are borrowed by Sessions. Close those Sessions before
    leaving the provider's async context or awaiting ``aclose()``.

    Eligible API-key HTTP Responses calls share idle connections across fresh
    Sessions on Linux when proxy routing variables are absent. TLS configuration
    and cached system trust material may remain in use until this explicit
    Provider closes. OS trust changes,
    including removed roots, may require a new Provider to refresh construction.
    With only ``SSL_CERT_FILE`` set,
    each request rereads and validates that file and changes retire cached
    connections. Other CA, proxy-policy, cookie and login modes retain fresh
    transport construction. Closing the Provider joins native owners before
    releasing its idle clients. Sessions retain fresh auth, settings and history.
    Synchronize process-environment changes with SDK calls. Changes between
    calls are supported; client construction does not atomically snapshot the
    ambient environment.
    """

    base_url: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    supports_websockets: bool | None = None
    auth: CodexLogin | None = field(default=None, repr=False)
    response_mode: Literal["streaming", "buffered"] = "streaming"

    def __post_init__(self) -> None:
        for name in ("base_url", "api_key"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be a non-empty string when provided")
        if self.supports_websockets is not None and not isinstance(self.supports_websockets, bool):
            raise ValueError("supports_websockets must be a bool or None")
        if self.response_mode not in ("streaming", "buffered"):
            raise ValueError("response_mode must be 'streaming' or 'buffered'")
        if self.response_mode == "buffered" and self.supports_websockets is True:
            raise ValueError("Buffered Responses cannot use WebSockets")
        if self.auth is not None:
            if not isinstance(self.auth, CodexLogin):
                raise TypeError("auth must be CodexLogin or None")
            if self.response_mode == "buffered":
                raise ValueError("Buffered Responses require API-key authentication")
            if self.api_key is not None or self.base_url is not None or self.supports_websockets:
                raise ValueError("CodexLogin requires HTTP and its own endpoint and credentials")

    @property
    def _websockets_enabled(self) -> bool:
        return (
            self.auth is None
            and self.response_mode == "streaming"
            and (
                self.supports_websockets
                if self.supports_websockets is not None
                else self.base_url is None
            )
        )

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        reduction = super().__reduce_ex__(protocol)
        if not isinstance(reduction, tuple) or len(reduction) < 3:
            return reduction
        state = reduction[2]
        dictionary = state[0] if isinstance(state, tuple) and state else state
        if not isinstance(dictionary, dict):
            return reduction
        # Copy settings and the closed fence, never another descriptor's resource.
        dictionary = dictionary.copy()
        dictionary.pop("_transport", None)
        dictionary.pop("_deferred_openings", None)
        state = (dictionary, *state[1:]) if isinstance(state, tuple) else dictionary
        return (*reduction[:2], state, *reduction[3:])

    def _acquire_lease(self) -> tuple[Any, Any, Any]:
        # This lock guards only synchronous publication. It is never held across
        # an await and carries no event-loop affinity.
        with _PROVIDER_LOCK:
            if vars(self).get("_closed", False):
                raise RuntimeError("NativeProvider is closed")
            native = importlib.import_module("agents._native")
            transport = vars(self).get("_transport")
            if transport is None:
                transport = native.NativeProviderOwner()
                object.__setattr__(self, "_transport", transport)
            return native, transport, transport.acquire()

    def _defer_opening(self, opening: _OpeningOwner, environment: _EnvironmentLease) -> None:
        # Failed publication is no longer an opening Session. Both its native
        # reservations and its Python manager now belong to provider recovery.
        with _PROVIDER_LOCK:
            opening.defer_environment(environment)
            pending = vars(self).get("_deferred_openings")
            if pending is None:
                pending = {}
                object.__setattr__(self, "_deferred_openings", pending)
            if opening not in pending:
                environment._sync_run = _sync_run.get()
                if environment._sync_run is not None:
                    environment._sync_run.deferred += 1
            pending[opening] = environment

    async def aclose(self) -> None:
        """Release remaining acquisitions and fence this Provider permanently.

        Open or opening Sessions cause an immediate error. Close them first.
        Closing an explicitly shared provider fences every Agent that uses it.
        Cancellation joins the same native retirement before it propagates.
        This also recovers a managed Environment retained after a failed
        run_sync opening, in the task that originally entered its manager.
        """
        with _PROVIDER_LOCK:
            if vars(self).get("_closed", False):
                return
            transport = vars(self).get("_transport")
            if transport is None:
                object.__setattr__(self, "_closed", True)
                return
            deferred = tuple(vars(self).get("_deferred_openings", {}).items())
            # Admission and the close fence happen before any application exit.
            # Deferred reservations are native orphans; other openings stay live.
            native_closing = transport.close()
            if deferred:

                async def release_openings() -> None:
                    for opening, environment in deferred:

                        async def release(
                            opening: _OpeningOwner = opening,
                            environment: _EnvironmentLease = environment,
                        ) -> None:
                            try:
                                await opening.fail_environment(environment)
                            finally:
                                # Native retirement permits exit; only the owner
                                # task proves that manager cleanup has completed.
                                if environment._owner.done():
                                    with _PROVIDER_LOCK:
                                        removed = (
                                            vars(self)
                                            .get("_deferred_openings", {})
                                            .pop(opening, None)
                                        )
                                        if removed is not None and removed._sync_run is not None:
                                            removed._sync_run.deferred -= 1

                        owner = environment._sync_run
                        if owner is not None and owner.loop is not asyncio.get_running_loop():
                            await _join(
                                asyncio.create_task(asyncio.to_thread(owner.recover, release))
                            )
                        else:
                            await release()

                async def close_with_openings() -> None:
                    try:
                        await native_closing
                    except BaseException as error:
                        # A home removal error alone must not strand a manager.
                        # fail_environment separately proves its resource join.
                        await _close_after_error(error, release_openings)
                        raise
                    await release_openings()

                operation = close_with_openings()
            else:
                operation = native_closing

        pending = asyncio.ensure_future(operation)
        try:
            await _join(pending)
        finally:
            if pending.done() and not pending.cancelled() and pending.exception() is None:
                with _PROVIDER_LOCK:
                    object.__setattr__(self, "_closed", True)
                    object.__setattr__(self, "_transport", None)

    async def __aenter__(self) -> NativeProvider:
        with _PROVIDER_LOCK:
            if vars(self).get("_closed", False):
                raise RuntimeError("NativeProvider is closed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is None:
            await self.aclose()
        else:
            await _close_after_error(exc, self.aclose)


_PROVIDER_LOCK = RLock()


@dataclass(init=False, eq=False, repr=False)
class Agent(Generic[ContextT, OutputT]):
    """Declare a native agent without starting a runtime or connection.

    Declaration fields are immutable. Construct a new Agent to change defaults;
    tools and model settings are captured at construction. A session captures
    effective overrides when it opens. The native runtime owns
    provider requests, the model loop, admitted calls, and conversation history.
    Python functions execute in the application and use its own authority.
    Include NativeAgents in tools to grant native child agents at opening.

    ``transform_context`` runs before each model request, including children,
    output repair, and model-based compaction. Return replacement input items
    or None to keep the original input. The callback does not edit saved history.
    Raising an exception prevents that request from reaching the provider.

    ``before_compact`` runs before automatic or manual native compaction,
    including children. Return a nonempty summary to skip native summarization,
    or None to keep the default. Codex publishes the summary in its history.

    ``before_complete`` checks proposed completion for roots and native children.
    Return None to accept, ContinueTurn with instructions to keep the same turn
    running, or StopTurn to abort by policy. Rejected text may already have
    streamed; only accepted completion settles the turn.

    ``programmatic_tool_calling=True`` also permits native JavaScript cells to call the declared
    function and MCP tools. MCP result transforms run before those cells
    receive results. The cells use the SDK's bundled host and do not grant
    shell tools or a local workspace. Pass Environment to session() for
    remote execution. Use on_interaction or a Session to answer manual requests made by code cells.
    The tools selected for each Code Mode step must have unique generated identifiers.
    If names collide, select only one of the functions with the same generated
    identifier for each step, or use direct mode.
    """

    instructions: str
    name: str | None
    tools: tuple[Tool[ContextT], ...]
    model: str | None
    model_settings: ModelSettings
    provider: NativeProvider | None
    output_type: OutputSpec[OutputT] | None
    programmatic_tool_calling: bool
    transform_context: _ContextTransform[ContextT] | None
    before_compact: _BeforeCompact[ContextT] | None
    before_complete: _BeforeComplete[ContextT] | None

    def __setattr__(self, name: str, value: object) -> None:
        # Freeze declared behavior while retaining application-owned method hooks.
        if name in self.__dataclass_fields__:
            raise FrozenInstanceError(f"cannot assign to field {name!r}")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self.__dataclass_fields__:
            raise FrozenInstanceError(f"cannot delete field {name!r}")
        object.__delattr__(self, name)

    if TYPE_CHECKING:
        # Pyright uses these allocation types to retain defaults and subclasses.
        # The initializer checks the full call; runtime allocation stays ordinary.
        @overload
        def __new__(
            cls: type[TextAgentT],
            *args: Any,
            tools: Sequence[Tool[ContextT]] | None = None,
            output_type: None = None,
            transform_context: _ContextTransform[ContextT] | None = None,
            before_compact: _BeforeCompact[ContextT] | None = None,
            before_complete: _BeforeComplete[ContextT] | None = None,
            **kwargs: Any,
        ) -> TextAgentT: ...

        @overload
        def __new__(
            cls: type[TypedAgentT],
            *args: Any,
            output_type: OutputSpec[OutputT],
            tools: Sequence[Tool[ContextT]] | None = None,
            transform_context: _ContextTransform[ContextT] | None = None,
            before_compact: _BeforeCompact[ContextT] | None = None,
            before_complete: _BeforeComplete[ContextT] | None = None,
            **kwargs: Any,
        ) -> TypedAgentT: ...

        # Subclasses may supply output_type inside their own initializer.
        @overload
        def __new__(cls: type[TypedAgentT], *args: Any, **kwargs: Any) -> TypedAgentT: ...

        def __new__(cls, *args: Any, **kwargs: Any) -> Agent[Any, Any]:
            return object.__new__(cls)

    @overload
    def __init__(
        # Mypy 1.17 needs the class context here to preserve explicit generics
        # while specializing the default output. Pyright rejects this form.
        self: Agent[ContextT, str],  # pyright: ignore[reportInvalidTypeVarUse]
        *,
        instructions: str,
        name: str | None = None,
        tools: Sequence[Tool[ContextT]] | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider: NativeProvider | None = None,
        output_type: None = None,
        programmatic_tool_calling: bool = False,
        transform_context: _ContextTransform[ContextT] | None = None,
        before_compact: _BeforeCompact[ContextT] | None = None,
        before_complete: _BeforeComplete[ContextT] | None = None,
    ) -> None: ...

    @overload
    def __init__(
        self,
        *,
        instructions: str,
        name: str | None = None,
        output_type: OutputSpec[OutputT],
        tools: Sequence[Tool[ContextT]] | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider: NativeProvider | None = None,
        programmatic_tool_calling: bool = False,
        transform_context: _ContextTransform[ContextT] | None = None,
        before_compact: _BeforeCompact[ContextT] | None = None,
        before_complete: _BeforeComplete[ContextT] | None = None,
    ) -> None: ...

    def __init__(
        self,
        *,
        instructions: str,
        name: str | None = None,
        tools: Sequence[Tool[ContextT]] | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider: NativeProvider | None = None,
        output_type: OutputSpec[OutputT] | None = None,
        programmatic_tool_calling: bool = False,
        transform_context: _ContextTransform[ContextT] | None = None,
        before_compact: _BeforeCompact[ContextT] | None = None,
        before_complete: _BeforeComplete[ContextT] | None = None,
    ) -> None:
        if not isinstance(instructions, str):
            raise TypeError("instructions must be a string")
        if name is not None and not isinstance(name, str):
            raise TypeError("name must be a string or None")
        if not isinstance(programmatic_tool_calling, bool):
            raise TypeError("programmatic_tool_calling must be a bool")
        if transform_context is not None and not callable(transform_context):
            raise TypeError("transform_context must be callable or None")
        if before_compact is not None and not callable(before_compact):
            raise TypeError("before_compact must be callable or None")
        if before_complete is not None and not callable(before_complete):
            raise TypeError("before_complete must be callable or None")
        if model is not None and not isinstance(model, str):
            raise TypeError("model must be a string or None")
        if provider is not None and not isinstance(provider, NativeProvider):
            raise TypeError("provider must be a NativeProvider or None")
        object.__setattr__(self, "instructions", instructions)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "tools", tuple(tools or ()))
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(
            self, "model_settings", MappingProxyType(_copy_model_settings(model_settings))
        )
        object.__setattr__(self, "output_type", output_type)
        object.__setattr__(self, "programmatic_tool_calling", programmatic_tool_calling)
        object.__setattr__(self, "transform_context", transform_context)
        object.__setattr__(self, "before_compact", before_compact)
        object.__setattr__(self, "before_complete", before_complete)

    def validate(self) -> None:
        """Check the declaration without opening a Session.

        Return None on success, or raise the first TypeError or ValueError.
        Uses the opening checks for tool admission, MCP names and output schema.
        Context, Environment, effective overrides and native model support can
        only be checked when a Session opens. This never reads credentials or
        connects to a provider, executor or MCP server.
        """
        from .mcp import _tool_names
        from .schema import _prepare_spec
        from .session import _check_namespaced_tool_collisions, _split_tools

        python_tools, mcp_tools, native_agents = _split_tools(
            self.tools, self.programmatic_tool_calling
        )
        _check_namespaced_tool_collisions(
            python_tools,
            _tool_names(mcp_tools),
            self.tools,
            native_agents=native_agents,
        )
        native = importlib.import_module("agents._native")
        native._validate_python_tool_declarations(
            json.dumps([json.loads(tool._spec_json()) for tool in python_tools])
        )
        _prepare_spec(self.output_type)

    def with_output_type(self, output_type: OutputSpec[NewOutputT]) -> Agent[ContextT, NewOutputT]:
        """Return a declaration variant with the same settings and a new output.

        To restore a per-turn manual override, pass the corresponding variant
        to session(state=...). Current tools, context and provider still apply.
        Application subclass hooks and their policy references are retained;
        the application initializer is not run again.
        """
        variant = copy(self)
        object.__setattr__(variant, "output_type", output_type)
        return cast("Agent[ContextT, NewOutputT]", variant)

    @overload
    def session(
        self: Agent[None, OutputT],
        *,
        context: None = None,
        state: SessionState | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[None] | None = None,
        tools: Sequence[Tool[None]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        max_function_concurrency: int | None = None,
    ) -> AbstractAsyncContextManager[Session[None, OutputT]]: ...

    @overload
    def session(
        self: Agent[object, OutputT],
        *,
        context: object = None,
        state: SessionState | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[object] | None = None,
        tools: Sequence[Tool[object]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        max_function_concurrency: int | None = None,
    ) -> AbstractAsyncContextManager[Session[object, OutputT]]: ...

    @overload
    def session(
        self,
        *,
        context: ContextT,
        state: SessionState | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[ContextT] | None = None,
        tools: Sequence[Tool[ContextT]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        max_function_concurrency: int | None = None,
    ) -> AbstractAsyncContextManager[Session[ContextT, OutputT]]: ...

    @asynccontextmanager
    async def session(
        self: Agent[SessionContextT, OutputT],
        *,
        context: SessionContextT | None = None,
        state: SessionState | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[SessionContextT] | None = None,
        tools: Sequence[Tool[SessionContextT]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        on_interaction: _InteractionHandler | None = None,
        max_function_concurrency: int | None = _MAX_FUNCTION_CONCURRENCY_UNSET,
    ) -> AsyncIterator[Session[SessionContextT, OutputT]]:
        """Open one conversation and close all runtime work on context exit.

        max_function_concurrency bounds automatic Python callbacks per turn owner,
        including validation and hooks through reply settlement. None adds no
        limit. Manual requests and native tools do not consume slots. Each child
        owner has an independent limit. Callback deadlines exclude this wait.
        When a DeferredTools.start session hook omits this argument, the restored
        Session inherits the source limit. An explicit value, including None,
        overrides that limit.

        on_interaction handles supported live requests for this Session's entire
        lifetime, including requests without Turn IDs. Return the exact request's
        reply. A failure affects the identified Turn, or the Session for an
        unscoped request. Closing joins started handlers. The handler is registered
        after supported opening completes; it does not enable startup prompts.

        parent_trace parents startup and defaults every new root or restored manual
        continuation. None supplies no explicit parent. Pass a fresh worker
        carrier on restore; trace context is never restored from saved state.

        An explicit provider here overrides this Agent's provider. Both are
        borrowed; the application closes them. When neither is supplied, this
        Session creates and closes its own provider, including on opening failure.
        `model=None` inherits the Agent model. Present model_settings keys
        override Agent defaults; only a present `service_tier=None` clears a
        value. Other omitted keys keep the Agent or native defaults.

        Omitted ``tools`` or ``tools=None`` uses this Agent's tools.
        A sequence replaces the finite opening definitions and defaults for
        this Session. An empty sequence admits none. Current callbacks and MCP
        connection details are captured at opening. Fresh and idle-restored
        Sessions acquire external MCP resources when an operation selecting
        them starts. Startup errors are reported on that Turn. Restoring an
        already-admitted pending manual turn readies its selected resources
        before exposing its replies.

        trace_options captures native tracing defaults before opening. Every
        submission inherits its disabled/content restrictions, including when
        another ambient scope or explicit parent changes the trace linkage.
        Tracing context and export credentials never enter SessionState.

        ``environment`` belongs to this session, so one Agent can use different
        application-owned executors for different users. A supplied Environment is
        borrowed. A standard async context manager yielding an Environment
        enters and exits in one SDK lease task. Native work closes before exit,
        including on opening failure. Lease-local ContextVar changes stay in
        that task. Restore uses only the supplied current environment and roots;
        checkpoints contain no credentials.

        With ``state``, restore an idle or settled manual native checkpoint
        using the effective provider, current tools, and application context. Tool declarations and
        instructions must match the saved conversation. Saved model settings
        take precedence over this Agent's model settings. Non-None model or
        model_settings overrides, including an empty mapping, are rejected with
        state. Use update(model=...) on the restored Session after it becomes idle.
        Live submission handles
        and workspace files do not transfer to the restored Session. Each restore
        owns isolated native storage; applications coordinate shared state files.
        Programmatic tool calling uses this Agent's current declaration, not a saved setting;
        enabling it on restore requires ``programmatic_tool_calling=True`` again.
        Load checkpoints only from trusted application storage: their history
        includes instructions. Unverifiable histories are rejected.

        With ``storage=CustomStorage(backend)``, use an application-owned
        canonical native backend. See SessionStore for its exclusive
        ownership and commit contract. This preview supports root Sessions;
        child agents, goals, checklist, code mode, manual and native tools,
        and environments are rejected.

        With ``storage=LocalStorage(path)``, use Core's native local storage.
        Retain Session.id and supply ``resume=id`` in a later process to reopen
        the same conversation. This requires the current tools, context and
        provider. An unknown root tool outcome raises LocalRecoveryRequired.
        Check the effect using application records and pass reconciled_calls
        on reopen. The SDK records those outcomes without running those
        calls. An unknown descendant tool outcome raises RuntimeError because
        descendant calls cannot be reconciled. Local storage and portable
        ``state`` cannot be combined. Local storage rejects code mode because
        individual nested tool identities lack durable intent and result receipts.

        Pass instruction_files to load trusted UTF-8 files as application
        instructions. Use files and parent directories that untrusted actors
        cannot modify. Each file's receipt identifies the exact bytes loaded.
        """
        if on_interaction is not None and not callable(on_interaction):
            raise TypeError("on_interaction must be callable or None")
        from .session import Session
        from .telemetry import _capture

        try:
            parent_trace = _trace_context(parent_trace)
            captured = _capture(trace_options)
        finally:
            del trace_options
        trace_error: BaseException | None = None
        try:
            session = await Session[SessionContextT, OutputT]._open(
                self,
                context=context,
                state=state,
                storage=storage,
                resume=resume,
                environment=environment,
                provider=provider,
                model=model,
                model_settings=model_settings,
                tool_selection=tools,
                instruction_files=instruction_files,
                reconciled_calls=reconciled_calls,
                max_function_concurrency=max_function_concurrency,
                parent_trace=parent_trace,
                **cast(
                    "_TracingOpeningOptions",
                    {"_tracing_context": captured.context} if captured.context is not None else {},
                ),
            )
            session._trace_context = captured.context
            if on_interaction is not None:
                session._start_interaction_handler(on_interaction)
            try:
                yield session
            except BaseException as error:
                lease = session._environment_lease
                await _close_after_error(error, session.aclose)
                if not session._closed or session._environment_lease is not None:
                    cast(Any, error).cleanup = session
                if lease is not None:
                    lease.raise_if_interrupted(error)
                raise
            else:
                lease = session._environment_lease
                try:
                    await session.aclose()
                except BaseException as error:
                    if lease is not None:
                        lease.raise_if_interrupted(error)
                    raise

        except BaseException as error:
            trace_error = error
            raise
        finally:
            captured.close(trace_error)

    @overload
    async def run(
        self: Agent[None, OutputT],
        input: UserInput | None,
        *,
        context: None = None,
        on_interaction: _InteractionHandler | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[None] | None = None,
        tools: Sequence[ToolRef | Tool[None] | Environment[None] | NativeAgents[None]]
        | None = None,
        before_step: _BeforeStep[None] | None = None,
        state: SessionState | None = None,
        return_state: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
    ) -> Completed[OutputT]: ...

    @overload
    async def run(
        self: Agent[object, OutputT],
        input: UserInput | None,
        *,
        context: object = None,
        on_interaction: _InteractionHandler | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[object] | None = None,
        tools: Sequence[ToolRef | Tool[object] | Environment[object] | NativeAgents[object]]
        | None = None,
        before_step: _BeforeStep[object] | None = None,
        state: SessionState | None = None,
        return_state: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
    ) -> Completed[OutputT]: ...

    @overload
    async def run(
        self,
        input: UserInput | None,
        *,
        context: ContextT,
        on_interaction: _InteractionHandler | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[ContextT] | None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        state: SessionState | None = None,
        return_state: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
    ) -> Completed[OutputT]: ...

    async def run(
        self: Agent[SessionContextT, OutputT],
        input: UserInput | None,
        *,
        context: SessionContextT | None = None,
        on_interaction: _InteractionHandler | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[SessionContextT] | None = None,
        tools: Sequence[
            ToolRef
            | Tool[SessionContextT]
            | Environment[SessionContextT]
            | NativeAgents[SessionContextT]
        ]
        | None = None,
        before_step: _BeforeStep[SessionContextT] | None = None,
        state: SessionState | None = None,
        return_state: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
    ) -> Completed[OutputT]:
        """Run one submission through a Session and close it on completion.

        parent_trace sets the W3C parent for native work, including a restored
        manual continuation. trace_options controls captured SDK workflow policy.

        max_function_concurrency follows session() and bounds automatic callbacks.

        provider, model, and model_settings follow the same precedence and
        restore restrictions as session(). An implicit provider is retired when
        the run closes, including failure and cancellation.

        prepare_request(turn, request, *, preparation_index, recovery_attempt)
        edits a detached next-request sampling copy. Return its complete value
        or None. Use an explicitly HTTP provider and eager ordinary function/MCP
        tools without children, Environment or code mode. The callback must
        tolerate repeated preparation and avoid business effects. HTTP retries
        reuse its captured edit; native stream recovery calls it again. Separate
        output correction attempts are unsupported; set max_retries=0 or use
        NativeOutput. Callback operations cannot suspend or export active state. Cancellation joins
        callback settlement before a later operation can start.

        on_interaction receives a live request and returns request.reply(value),
        directly or through an awaitable. The SDK validates and delivers it.
        For approvals, use reply("allow_once") or reply("deny"). For forms,
        use reply(values); reply(None) declines a form or URL request.
        Native replies acknowledge enqueue only. Direct Session callers can use
        session.respond(request.reply(value)) or request-specific response methods.
        Handlers may run concurrently; synchronous
        handlers run in worker threads. The SDK joins them before closing.
        Without a handler, a plain or native-output Python/manual-only run can complete or
        raise InteractionRequired with a checkpoint at a settled manual call.
        Other interactive combinations require a handler or Agent.session().

        budget limits this logical run's next root sampling admission, including
        output repair. Pass on_interaction if the budgeted run uses manual tools.
        The SDK cannot capture a budgeted active run or add a budget to a
        restored active turn. return_state=True can still save an idle result.

        Pass state to restore with current bindings. For a supported pending
        manual checkpoint, input=None resumes the existing turn. Idle state
        needs input for a new turn. return_state=True captures a successful
        state before close; save_state restrictions and failures still apply.
        """
        options: dict[str, Any] = {}
        try:
            from .prepared_request import _check_output, _check_profile, _check_tools
            from .schema import PromptedOutput, _prepare_spec
            from .session import CapabilityError, InteractionRequired, NativeError, _split_tools
            from .state import SessionState
            from .tools import _HOSTED_TOOLS, WebSearch, _CapturedFunction

            if budget is not None and not isinstance(budget, RunBudget):
                raise TypeError("budget must be a RunBudget or None")
            if prepare_request is not None:
                if not callable(prepare_request):
                    raise TypeError("prepare_request must be callable")
                if getattr(self.session, "__func__", None) is Agent.session:
                    _check_output(_prepare_spec(self.output_type))
                    effective_provider = self.provider if provider is None else provider
                    if effective_provider is not None and not isinstance(
                        effective_provider, NativeProvider
                    ):
                        raise TypeError("provider must be a NativeProvider or None")
                    _check_profile(
                        self,
                        websockets=True
                        if effective_provider is None
                        else effective_provider._websockets_enabled,
                        environment=environment,
                    )
                    if tools is None and before_step is None and state is None:
                        _check_tools(self.tools)
            if on_interaction is not None and not callable(on_interaction):
                raise TypeError("on_interaction must be callable")
            if not isinstance(return_state, bool):
                raise TypeError("return_state must be a bool")
            if state is not None and not isinstance(state, SessionState):
                raise TypeError("state must be a SessionState")
            resume_manual = state is not None and state.native_continuation is not None
            if resume_manual:
                if budget is not None:
                    raise CapabilityError("Cannot set a budget when restoring an active checkpoint")
                if input is not None:
                    raise ValueError("Use input=None to resume a pending manual checkpoint")
                if on_interaction is None:
                    error = InteractionRequired(
                        "Resuming a pending manual checkpoint requires on_interaction "
                        "or Agent.session()"
                    )
                    error.state = state
                    raise error
                if tools is not None or before_step is not None or prepare_request is not None:
                    raise ValueError(
                        "Cannot change tools, before_step or prepare_request "
                        "on an already admitted turn"
                    )
            elif input is None:
                raise TypeError("input is required unless resuming a pending manual checkpoint")

            declared, mcp_tools, native_agents = _split_tools(
                self.tools, self.programmatic_tool_calling
            )
            pause_manual = tuple(
                tool
                for tool in declared
                if on_interaction is None
                and before_step is None
                and prepare_request is None
                and tool._bind is None
                and (tools is None or tool in tools or tool.ref in tools)
            )
            budget_capture_error = None
            if budget is not None and pause_manual:
                budget_capture_error = CapabilityError(
                    "An active run with a budget cannot be captured for manual replies. "
                    "Pass on_interaction or use Agent.session() for live replies."
                )
                if state is None or tools is not None:
                    raise budget_capture_error
            # Restore persisted defaults before judging an omitted selection.
            if (
                pause_manual
                and (state is None or tools is not None)
                and (
                    self.programmatic_tool_calling
                    or isinstance(self.output_type, PromptedOutput)
                    or environment is not None
                    or native_agents is not None
                    or before_step is not None
                    or any(tool._requires_live_manual_session for tool in declared)
                    or (
                        mcp_tools
                        and (
                            (tools is None and state is None)
                            or (tools is not None and _split_tools(tools, available=self.tools)[1])
                        )
                    )
                )
            ):
                raise InteractionRequired(
                    "Pausing for manual replies requires ordinary Python callbacks "
                    "and bare manual tools, "
                    "plain output or native strict output, "
                    "no environment, children or before_step. "
                    "Selected MCP bindings and native MCP work require live replies. "
                    "Pass on_interaction or use Agent.session() for live replies."
                )
            # Capture before opening. An override may expose the Session and its
            # observations, including when it delegates to the original method.
            session_factory = self.session
            if on_interaction is not None:
                options["on_interaction"] = on_interaction
            if parent_trace is not None:
                options["parent_trace"] = _trace_context(parent_trace)
            if trace_options is not None:
                options["trace_options"] = trace_options
            if state is not None:
                options["state"] = state
            if provider is not None:
                options["provider"] = provider
            if model is not None:
                options["model"] = model
            if model_settings is not None:
                options["model_settings"] = model_settings
            if max_function_concurrency is not None:
                options["max_function_concurrency"] = max_function_concurrency
            if environment is not None:
                options["environment"] = environment
            async with session_factory(
                context=cast(SessionContextT, context),
                **options,
            ) as session:
                if pause_manual or (prepare_request is not None and on_interaction is None):
                    # A delegating factory may change the opening roster or its
                    # default operation. Check the same effective selection that
                    # submit will capture, before it admits work or starts MCP.
                    opening = session._opening_agent
                    assert opening is not None
                    effective = session._default_tools if tools is None else tools
                    selected, selected_mcp, _ = _split_tools(
                        opening.tools if effective is None else effective,
                        opening.programmatic_tool_calling,
                        available=None if effective is None else session._bindings,
                        ceiling=opening.tools,
                        environment=session._executor,
                        native_agents=None if effective is None else session._native_agents,
                    )
                    if budget_capture_error is not None:
                        if any(tool._bind is None for tool in selected):
                            raise budget_capture_error
                        pause_manual = ()
                    if (
                        prepare_request is not None
                        and before_step is None
                        and any(tool._bind is None for tool in selected)
                    ):
                        raise InteractionRequired(
                            "Running manual tools with prepare_request requires on_interaction. "
                            "Use Agent.session() to reply to requests directly."
                        )
                    # Submit can republish bound tools beyond the restored idle catalog.
                    if any(tool is eligible for tool in selected for eligible in pause_manual) and (
                        opening.programmatic_tool_calling
                        or session._default_output.mode == "prompted"
                        or session._executor is not None
                        or session._native_agents is not None
                        or session._storage is not None
                        or selected_mcp
                        or any(
                            isinstance(tool, _CapturedFunction)
                            and tool._requires_live_manual_session
                            for tool in session._bound_tools
                        )
                        or any(
                            isinstance(tool, (WebSearch, *_HOSTED_TOOLS)) for tool in opening.tools
                        )
                    ):
                        raise InteractionRequired(
                            "Pausing for manual replies requires ordinary Python callbacks "
                            "and bare manual tools, "
                            "plain output or native strict output, "
                            "no local storage, environment, children or before_step. "
                            "Selected MCP bindings and native MCP work require live replies. "
                            "Pass on_interaction or use Agent.session() for live replies."
                        )
                session._fail_unhandled_questions = (
                    session._interaction_handler is None and not pause_manual
                )
                if resume_manual:
                    restored = session.restored_turn
                    if restored is None:
                        raise NativeError("Restored manual checkpoint has no pending submission")
                    submission = cast("Turn[OutputT]", restored)
                else:
                    assert input is not None
                    submit_options: dict[str, Any] = {}
                    if prepare_request is not None:
                        submit_options["prepare_request"] = prepare_request
                    if budget is not None:
                        submit_options["budget"] = budget
                    submission = await session.submit(
                        input, tools=tools, before_step=before_step, **submit_options
                    )
                if session._interaction_handler is None and pause_manual:
                    return await session._run_with_interactions(
                        submission,
                        on_interaction,
                        return_state=return_state,
                        pause_manual=pause_manual,
                    )
                result = await submission.get_result()
                if return_state:
                    result = replace(result, state=await session.save_state())
                return result
        finally:
            options.pop("trace_options", None)
            del trace_options

    @overload
    def run_sync(
        self: Agent[None, OutputT],
        input: UserInput | None,
        *,
        context: None = None,
        on_interaction: _InteractionHandler | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[None] | None = None,
        tools: Sequence[ToolRef | Tool[None] | Environment[None] | NativeAgents[None]]
        | None = None,
        before_step: _BeforeStep[None] | None = None,
        state: SessionState | None = None,
        return_state: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
    ) -> Completed[OutputT]: ...

    @overload
    def run_sync(
        self: Agent[object, OutputT],
        input: UserInput | None,
        *,
        context: object = None,
        on_interaction: _InteractionHandler | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[object] | None = None,
        tools: Sequence[ToolRef | Tool[object] | Environment[object] | NativeAgents[object]]
        | None = None,
        before_step: _BeforeStep[object] | None = None,
        state: SessionState | None = None,
        return_state: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
    ) -> Completed[OutputT]: ...

    @overload
    def run_sync(
        self,
        input: UserInput | None,
        *,
        context: ContextT,
        on_interaction: _InteractionHandler | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[ContextT] | None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        state: SessionState | None = None,
        return_state: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
    ) -> Completed[OutputT]: ...

    def run_sync(
        self: Agent[SessionContextT, OutputT],
        input: UserInput | None,
        *,
        context: SessionContextT | None = None,
        on_interaction: _InteractionHandler | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        environment: EnvironmentSource[SessionContextT] | None = None,
        tools: Sequence[
            ToolRef
            | Tool[SessionContextT]
            | Environment[SessionContextT]
            | NativeAgents[SessionContextT]
        ]
        | None = None,
        before_step: _BeforeStep[SessionContextT] | None = None,
        state: SessionState | None = None,
        return_state: bool = False,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        max_function_concurrency: int | None = None,
    ) -> Completed[OutputT]:
        """Run through session() outside an event loop.

        on_interaction and state follow run(). A sync or async handler
        returns request.reply(value); the SDK delivers it before closing.

        If opening with a managed Environment fails and automatic cleanup leaves
        the implicit provider open, the original error carries ``error.provider``.
        Outside an event loop, call ``asyncio.run(error.provider.aclose())`` to
        retry cleanup. Explicit providers remain the caller's responsibility and
        are not attached to the error.
        """
        if budget is not None and not isinstance(budget, RunBudget):
            raise TypeError("budget must be a RunBudget or None")
        options: dict[str, Any] = {}
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                if parent_trace is not None:
                    options["parent_trace"] = _trace_context(parent_trace)
                if trace_options is not None:
                    options["trace_options"] = trace_options
                if state is not None:
                    options["state"] = state
                if return_state is not False:
                    options["return_state"] = return_state
                if on_interaction is not None:
                    options["on_interaction"] = on_interaction
                if provider is not None:
                    options["provider"] = provider
                if model is not None:
                    options["model"] = model
                if model_settings is not None:
                    options["model_settings"] = model_settings
                if max_function_concurrency is not None:
                    options["max_function_concurrency"] = max_function_concurrency
                if environment is not None:
                    options["environment"] = environment
                if prepare_request is not None:
                    options["prepare_request"] = prepare_request
                if budget is not None:
                    options["budget"] = budget
                return _SyncRun().run(
                    self.run(
                        input,
                        context=cast(SessionContextT, context),
                        tools=tools,
                        before_step=before_step,
                        **options,
                    )
                )
            raise RuntimeError("run_sync cannot run inside an event loop; await agent.run()")
        finally:
            options.pop("trace_options", None)
            del trace_options


_preserve_module_identity(globals(), "agents.agent")
