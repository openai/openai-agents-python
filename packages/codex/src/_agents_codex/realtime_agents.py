"""Application-owned defaults for captured native Realtime Agents."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, RootModel, TypeAdapter, create_model

from ._function_tools import FunctionTool, ToolContext
from ._public import preserve_module_identity as _preserve_module_identity
from .native_interception import Allow, Deny
from .realtime_guardrails import RealtimeOutputGuardrail
from .realtime_settings import NativeRealtimePrompt
from .tools import ToolRef, _CapturedFunction

if TYPE_CHECKING:
    from .agent import Agent


@dataclass(frozen=True)
class RealtimeAgentContext:
    """The application context and exact captured Agent being configured."""

    context: Any
    agent: Agent[Any, Any]


@dataclass(frozen=True)
class RealtimeAgentConfig:
    """Voice defaults without changing the Agent's backend instructions.

    Configuration callbacks run at voice startup and acknowledged Agent changes.
    A failed callback prevents its configuration from being sent. Tool predicates
    restrict the captured catalog and run again before host functions and native
    MCP tools execute.
    Explicit connection instructions and prompt settings override these defaults.
    """

    instructions: str | Callable[[RealtimeAgentContext], str | Awaitable[str]] | None = None
    prompt: NativeRealtimePrompt | None = None
    output_guardrails: tuple[RealtimeOutputGuardrail, ...] = ()
    tool_enabled: Callable[[RealtimeAgentContext, ToolRef], bool | Awaitable[bool]] | None = None

    def __post_init__(self) -> None:
        if self.instructions is not None and not (
            isinstance(self.instructions, str) or callable(self.instructions)
        ):
            raise TypeError("Realtime instructions must be a string, callback, or None")
        if self.prompt is not None and not isinstance(self.prompt, Mapping):
            raise TypeError("Realtime prompt must be a mapping or None")
        if any(not isinstance(check, RealtimeOutputGuardrail) for check in self.output_guardrails):
            raise TypeError(
                "Realtime output_guardrails must contain RealtimeOutputGuardrail objects"
            )
        if self.tool_enabled is not None and not callable(self.tool_enabled):
            raise TypeError("Realtime tool_enabled must be callable or None")

    def _capture(self) -> RealtimeAgentConfig:
        return replace(
            self,
            prompt=deepcopy(self.prompt),
            output_guardrails=tuple(self.output_guardrails),
        )


@dataclass(frozen=True)
class RealtimeHandoff:
    """A captured target and an application callback before handoff acceptance.

    The callback receives the issuing Agent's context and validated input. Its
    return value cannot choose another target. Native execution owns admission,
    cancellation and the final profile change after the callback succeeds.
    """

    agent: Agent[Any, Any]
    name: str | None = None
    description: str | None = None
    input_type: type[Any] | None = None
    on_handoff: Callable[..., Any] | None = None
    is_enabled: bool | Callable[[RealtimeAgentContext], bool | Awaitable[bool]] = True

    def __post_init__(self) -> None:
        from .agent import Agent

        if not isinstance(self.agent, Agent):
            raise TypeError("Realtime handoff target must be a native Agent")
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError("Realtime handoff name must be a string or None")
        if self.description is not None and not isinstance(self.description, str):
            raise TypeError("Realtime handoff description must be a string or None")
        if self.input_type is not None:
            if not isinstance(self.input_type, type):
                raise TypeError("Realtime handoff input_type must be a type")
            TypeAdapter(self.input_type).json_schema()
        if self.input_type is not None and self.on_handoff is None:
            raise ValueError("Realtime handoff input_type requires on_handoff")
        if self.on_handoff is not None and not callable(self.on_handoff):
            raise TypeError("Realtime handoff on_handoff must be callable or None")
        if type(self.is_enabled) is not bool and not callable(self.is_enabled):
            raise TypeError("Realtime handoff is_enabled must be a bool or callback")


def realtime_handoff(
    agent: Agent[Any, Any],
    *,
    name: str | None = None,
    description: str | None = None,
    input_type: type[Any] | None = None,
    on_handoff: Callable[..., Any] | None = None,
    is_enabled: bool | Callable[[RealtimeAgentContext], bool | Awaitable[bool]] = True,
) -> RealtimeHandoff:
    """Declare a native handoff with optional structured application input."""
    return RealtimeHandoff(agent, name, description, input_type, on_handoff, is_enabled)


@dataclass(frozen=True)
class _CapturedHandoff(_CapturedFunction):
    unwrap_root: bool = False

    def _arguments(self, parsed: BaseModel) -> dict[str, Any]:
        return {
            "payload": parsed.root if self.unwrap_root and isinstance(parsed, RootModel) else parsed
        }


class _HandoffAllowedAfterAwait(Allow):
    """A handoff grant returned after awaiting application policy."""


def _handoff_tool(
    handoff: RealtimeHandoff, source: Agent[Any, Any], name: str
) -> FunctionTool[Any, Any, Any]:
    model: type[BaseModel]
    declared = handoff.input_type
    if declared is None:
        model = create_model("RealtimeHandoffInput", __config__=ConfigDict(extra="forbid"))
    elif issubclass(declared, BaseModel):
        model = declared
    else:
        adapter = TypeAdapter(declared)

        class StructuredHandoffInput(RootModel[declared]):  # type: ignore[valid-type]
            @classmethod
            def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
                # Keep TypeAdapter's original object schema, without a root wrapper.
                return adapter.json_schema(*args, **kwargs)

        model = StructuredHandoffInput

    def bind(
        context: Any, policy_context: Any, declaration: FunctionTool[Any, Any, Any]
    ) -> _CapturedFunction:
        application = RealtimeAgentContext(context, source)

        def invoke(payload: BaseModel) -> Any:
            acknowledgement = "Handoff reserved."
            callback = handoff.on_handoff
            if callback is None:
                return acknowledgement
            value = (
                callback(application, payload)
                if handoff.input_type is not None
                else callback(application)
            )
            if inspect.isawaitable(value):

                async def finish() -> str:
                    await value
                    return acknowledgement

                return finish()
            return acknowledgement

        async def before(_: ToolContext[Any]) -> Allow | Deny:
            enabled = handoff.is_enabled
            value = enabled(application) if callable(enabled) else enabled
            if inspect.isawaitable(value):
                result = await value
                allowed: Allow = _HandoffAllowedAfterAwait()
            else:
                result = value
                allowed = Allow()
            if type(result) is not bool:
                raise TypeError("Realtime handoff is_enabled must return a bool")
            return allowed if result else Deny("This handoff is disabled.")

        return _CapturedHandoff(
            name=name,
            spec_json=declaration._spec_json(),
            model=model,
            field_names=tuple(model.model_fields),
            output_adapter=TypeAdapter(str),
            callback=invoke,
            context_parameter=None,
            callback_timeout=None,
            timeout_behavior="raise_exception",
            timeout_error_function=None,
            before=before,
            after=None,
            context=context,
            unwrap_root=declared is not None and not issubclass(declared, BaseModel),
        )

    return FunctionTool(
        name=name,
        description=handoff.description or f"Transfer the conversation to {handoff.agent.name}.",
        input_type=model,
        output_type=str,
        _bind=bind,
    )


async def _resolve_instructions(
    config: RealtimeAgentConfig, context: RealtimeAgentContext
) -> str | None:
    value = config.instructions
    if callable(value):
        resolved = value(context)
        value = await resolved if inspect.isawaitable(resolved) else resolved
        if not isinstance(value, str):
            raise TypeError("Realtime instructions callback must return a string")
    if value is not None and not isinstance(value, str):
        raise TypeError("Realtime instructions callback must return a string")
    return value


async def _enabled(
    config: RealtimeAgentConfig, context: RealtimeAgentContext, tool: ToolRef
) -> bool:
    if config.tool_enabled is None:
        return True
    value = config.tool_enabled(context, tool)
    result = await value if inspect.isawaitable(value) else value
    if type(result) is not bool:
        raise TypeError("Realtime tool_enabled must return a bool")
    return result


@contextmanager
def _configuration_callback_scope(session: Any) -> Any:
    from .session import _Invocation, _invocation

    guard = _Invocation(session)
    token = _invocation.set(guard)
    try:
        yield
    finally:
        guard.active = False
        _invocation.reset(token)


async def _resolve_realtime_configuration(
    session: Any,
    instructions: str | None,
    settings: dict[str, Any],
    tools: Any,
    *,
    target: Mapping[str, Any] | None = None,
    allow_missing: bool = False,
    reset_prompt: bool = False,
    source_snapshot: Mapping[str, Any] | None = None,
    source_profile: Any = None,
) -> tuple[str | None, dict[str, Any], list[dict[str, Any]] | None, str | None]:
    """Resolve app defaults against one exact native catalog receipt."""
    source = session._effective_agent() if hasattr(session, "_effective_agent") else None
    defaults = getattr(source, "realtime", None)
    profile = source_profile
    profiles = getattr(session, "_profiles", {})
    if profile is not None:
        source = profile.declaration
        defaults = source.realtime
    elif target is not None and profiles:
        profile = profiles[target["profile_id"]].profile
        source = profile.declaration
        defaults = source.realtime
    elif getattr(session, "_profile_current", None) is not None:
        profile = profiles[session._profile_current[0]].profile
    handoffs = profile.handoff_options if profile is not None else {}
    if defaults is None and not handoffs and source_snapshot is None:
        definitions, selection = await session._realtime_function_tools(
            tools, **({"allow_missing": True} if allow_missing else {})
        )
        resolved_settings = dict(settings)
        if reset_prompt:
            resolved_settings.setdefault("prompt", None)
        return instructions, resolved_settings, definitions, selection

    snapshot = (
        source_snapshot
        if source_snapshot is not None
        else json.loads(await session._native.call("realtime_tool_selection_sdk", "{}"))
    )
    receipt = snapshot["profile_receipt"]
    if target is not None and receipt != target:
        raise RuntimeError("Realtime Agent changed before its configuration was resolved")
    captured = session._agent_for_profile(receipt)
    if captured is not source:
        # Declarations retain their identity in the Session's immutable registry.
        raise RuntimeError("Realtime configuration belongs to another captured Agent")
    original = profile.agent if profile is not None else getattr(session, "_agent", source)
    if original is None:
        raise RuntimeError("Realtime configuration has no source Agent")
    context = RealtimeAgentContext(session._context, original)
    if defaults is not None and instructions is None:
        with _configuration_callback_scope(session):
            instructions = await _resolve_instructions(defaults, context)
    definitions, selection = await session._realtime_function_tools(
        tools, snapshot=snapshot, **({"allow_missing": True} if allow_missing else {})
    )
    if selection is not None and selection != snapshot["selection_id"]:
        raise RuntimeError("Realtime catalog changed while resolving Agent callbacks")
    wire_tools = snapshot["function_tools"]
    if definitions is None:
        definitions = [
            {
                "name": definition["name"],
                "description": definition.get("description") or "",
                "inputSchema": definition["parameters"],
            }
            for definition in wire_tools
        ]
    refs = {definition["name"]: definition["native_ref"] for definition in wire_tools}
    selected = []
    for definition in definitions:
        name = definition["name"]
        if defaults is not None and defaults.tool_enabled is not None:
            with _configuration_callback_scope(session):
                enabled_tool = await _enabled(defaults, context, ToolRef(**refs[name]))
            if not enabled_tool:
                continue
        handoff = handoffs.get(name)
        if handoff is not None:
            enabled = handoff.is_enabled
            with _configuration_callback_scope(session):
                value = enabled(context) if callable(enabled) else enabled
                result = await value if inspect.isawaitable(value) else value
            if type(result) is not bool:
                raise TypeError("Realtime handoff is_enabled must return a bool")
            if not result:
                continue
        selected.append(definition)
    resolved_settings = dict(settings)
    if "prompt" not in resolved_settings:
        if defaults is not None and defaults.prompt is not None:
            resolved_settings["prompt"] = deepcopy(defaults.prompt)
        elif reset_prompt:
            resolved_settings["prompt"] = None
    return instructions, resolved_settings, selected, snapshot["selection_id"]


def _capture_voice_graph(
    session: Any, agent: Any, prefix: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from ._agent_profiles import _capture_agent_graph
    from .agent import Agent
    from .mcp import _MCP
    from .schema import _prepare_spec
    from .session import (
        _capture_opening_agent,
        _capture_profile_runtimes,
        _capture_tools,
        _complete_profile_mcp,
        _split_tools,
    )

    if not isinstance(agent, Agent):
        raise TypeError("update_agent requires a native Agent")
    opening = session._opening_agent
    if opening is None:
        raise RuntimeError("Session has no captured opening Agent")
    if agent.provider is not None and agent.provider is not opening.provider:
        raise ValueError("A voice Agent must use the Session's provider")
    if (
        _prepare_spec(agent.output_type).fingerprint
        != _prepare_spec(opening.output_type).fingerprint
    ):
        raise ValueError("A voice Agent must preserve the Session's output contract")
    if agent.programmatic_tool_calling != opening.programmatic_tool_calling:
        raise ValueError("A voice Agent must preserve the Session's tool calling mode")

    def fixed(declaration: Agent[Any, Any]) -> tuple[Any, ...]:
        return tuple(
            tool for tool in declaration.tools if not isinstance(tool, (FunctionTool, _MCP))
        )

    if fixed(agent) != fixed(opening):
        raise ValueError("A voice Agent cannot change the Session's fixed runtime tools")
    declaration = _capture_opening_agent(
        agent, provider=opening.provider, model=None, model_settings=None, tool_selection=None
    )
    profiles = _capture_agent_graph(agent, root_declaration=declaration, id_prefix=prefix)
    functions, _, _ = _split_tools(
        profiles[0].declaration.tools, declaration.programmatic_tool_calling
    )
    for profile in profiles:
        for tool in profile.declaration.tools:
            check = getattr(tool, "_check_context", None)
            if check is not None:
                check(session._context)
    catalog = _capture_tools(0, functions, session._context)
    runtimes, roster = _capture_profile_runtimes(
        profiles,
        catalog,
        declaration.instructions,
        session._context,
        session._output_guardrails,
        session._input_guardrails,
        run_error_formatter=session._tool_error_formatter is not None,
    )
    _complete_profile_mcp(profiles, roster, session._executor)
    return runtimes, roster


_preserve_module_identity(globals(), "agents.realtime")
