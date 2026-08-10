from __future__ import annotations

import asyncio
import builtins
import sys
import traceback
import types
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn, cast

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup
else:
    BaseExceptionGroup = builtins.BaseExceptionGroup

if TYPE_CHECKING:
    from .agent import Agent
    from .guardrail import InputGuardrailResult, OutputGuardrailResult
    from .items import ModelResponse, RunItem, TResponseInputItem
    from .run_context import RunContextWrapper
    from .tool_guardrails import (
        ToolGuardrailFunctionOutput,
        ToolInputGuardrail,
        ToolInputGuardrailResult,
        ToolOutputGuardrail,
        ToolOutputGuardrailResult,
    )

from .util._pretty_print import pretty_print_run_error_details

_DRAIN_STREAM_EVENTS_ATTR = "_agents_drain_queued_stream_events"
_DATA_REDACTED_ATTR = "_agents_data_redacted"
_DATA_REDACTED_ERROR_MESSAGE = "Error details are redacted."
_TYPE_NAMESPACE_DESCRIPTOR = cast(Any, type).__dict__["__dict__"]
_TYPE_MRO_DESCRIPTOR = cast(Any, type).__dict__["__mro__"]
_SYSTEM_EXIT_CODE_DESCRIPTOR = cast(Any, SystemExit).__dict__["code"]


def _mark_error_to_drain_stream_events(error: BaseException) -> None:
    setattr(error, _DRAIN_STREAM_EVENTS_ATTR, True)


def _should_drain_stream_events_before_raising(error: BaseException) -> bool:
    return bool(getattr(error, _DRAIN_STREAM_EVENTS_ATTR, False))


def _mark_error_data_redacted(error: BaseException) -> None:
    setattr(error, _DATA_REDACTED_ATTR, True)


def _base_exception_instance_dict(error: BaseException) -> dict[object, object] | None:
    """Return built-in exception state without invoking subclass attribute descriptors."""
    try:
        reduced = BaseException.__reduce__(error)
    except BaseException:
        return None
    if type(reduced) is not tuple or len(reduced) < 3:
        return None
    state = reduced[2]
    return state if type(state) is dict else None


def _static_type_metadata(
    value: type,
) -> tuple[types.MappingProxyType[str, object], tuple[type, ...]] | None:
    """Return built-in type metadata without invoking metaclass descriptors."""
    try:
        namespace = _TYPE_NAMESPACE_DESCRIPTOR.__get__(value, type(value))
        mro = _TYPE_MRO_DESCRIPTOR.__get__(value, type(value))
    except BaseException:
        return None
    if type(namespace) is not types.MappingProxyType or type(mro) is not tuple:
        return None
    return cast(types.MappingProxyType[str, object], namespace), cast(tuple[type, ...], mro)


def _object_instance_dict(
    value: object,
    *,
    trusted_base: type,
) -> dict[object, object] | None:
    """Return instance state through a descriptor owned by a trusted base type."""
    value_metadata = _static_type_metadata(type(value))
    trusted_metadata = _static_type_metadata(trusted_base)
    if value_metadata is None or trusted_metadata is None:
        return None
    _, value_mro = value_metadata
    if not any(base is trusted_base for base in value_mro):
        return None

    _, trusted_mro = trusted_metadata
    for base in trusted_mro:
        base_metadata = _static_type_metadata(base)
        if base_metadata is None:
            continue
        namespace, _ = base_metadata
        for name, descriptor in namespace.items():
            if (
                type(name) is str
                and str.__eq__(name, "__dict__") is True
                and type(descriptor) is types.GetSetDescriptorType
            ):
                try:
                    state = descriptor.__get__(value, type(value))
                except BaseException:
                    return None
                return state if type(state) is dict else None
    return None


def _exact_string_state_entry(
    state: dict[object, object],
    name: str,
) -> tuple[bool, object | None]:
    """Read an exact string key without hashing or comparing provider keys."""
    for candidate, value in dict.items(state):
        if type(candidate) is str and str.__eq__(candidate, name) is True:
            return True, value
    return False, None


def _exact_string_state_value(
    state: dict[object, object],
    name: str,
) -> object | None:
    """Read an exact string key, returning `None` for a missing key."""
    return _exact_string_state_entry(state, name)[1]


def _is_error_data_redacted(error: BaseException) -> bool:
    state = _base_exception_instance_dict(error)
    return state is not None and _exact_string_state_value(state, _DATA_REDACTED_ATTR) is True


def _clear_data_redacted_error_traceback(error: BaseException) -> None:
    if not _is_error_data_redacted(error):
        return
    descriptor = cast(Any, BaseException.__traceback__)
    source_traceback = descriptor.__get__(error, type(error))
    if source_traceback is not None:
        traceback.clear_frames(source_traceback)


def _detach_data_redacted_error_traceback(error: BaseException) -> None:
    if _is_error_data_redacted(error):
        for descriptor in (
            cast(Any, BaseException.__traceback__),
            cast(Any, BaseException.__cause__),
            cast(Any, BaseException.__context__),
        ):
            descriptor.__set__(error, None)


def _prepare_data_redacted_error(
    error: BaseException,
    *,
    trusted_error_message: str | None = None,
) -> BaseException:
    """Detach payload-owned state and return a safe error for a public boundary."""
    error_type = type(error)
    process_control_error = _replace_data_redacted_process_control_error(error)
    if process_control_error is not None:
        return process_control_error
    safe_message: str | None = None
    if error_type is UserError or error_type is ValueError:
        try:
            args = cast(Any, BaseException.args).__get__(error, error_type)
            if isinstance(args, tuple) and len(args) == 1 and isinstance(args[0], str):
                message = args[0]
                if message == trusted_error_message:
                    safe_message = message
        except BaseException:
            pass

    _discard_exception_graph(error)

    safe_error: BaseException = RuntimeError(_DATA_REDACTED_ERROR_MESSAGE)
    if error_type is ModelBehaviorError:
        safe_error = ModelBehaviorError(_DATA_REDACTED_ERROR_MESSAGE)
    elif error_type is UserError and safe_message is not None:
        safe_error = UserError(safe_message)
    elif error_type is ValueError and safe_message is not None:
        safe_error = ValueError(safe_message)
    try:
        _mark_error_data_redacted(safe_error)
    except BaseException:
        pass
    return safe_error


def _replace_data_redacted_process_control_error(
    error: BaseException,
) -> BaseException | None:
    """Discard a process-control source and return a fresh value-free replacement."""
    error_type = type(error)
    if issubclass(error_type, asyncio.CancelledError):
        safe_error: BaseException | None = asyncio.CancelledError()
    elif issubclass(error_type, GeneratorExit):
        safe_error = GeneratorExit()
    elif issubclass(error_type, KeyboardInterrupt):
        safe_error = KeyboardInterrupt()
    elif not issubclass(error_type, SystemExit):
        return None
    elif error_type is not SystemExit:
        safe_error = SystemExit(1)
    else:
        try:
            effective_code = _SYSTEM_EXIT_CODE_DESCRIPTOR.__get__(error, SystemExit)
        except BaseException:
            safe_error = SystemExit(1)
        else:
            if type(effective_code) not in {type(None), bool, int}:
                safe_error = SystemExit(1)
            elif effective_code is None:
                safe_error = SystemExit()
            else:
                safe_error = SystemExit(effective_code)

    assert safe_error is not None
    _discard_exception_graph(error)
    try:
        _mark_error_data_redacted(safe_error)
    except BaseException:
        pass
    return safe_error


def _collect_nested_exceptions(value: object, linked: list[BaseException]) -> None:
    """Collect exceptions reachable through exact built-in containers without callbacks."""
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if issubclass(type(current), BaseException):
            linked.append(cast(BaseException, current))
        elif type(current) is dict:
            for key, item in dict.items(current):
                pending.extend((key, item))
        elif (
            type(current) is list
            or type(current) is tuple
            or type(current) is set
            or type(current) is frozenset
        ):
            pending.extend(current)


def _discard_exception_graph(error: BaseException) -> None:
    """Best-effort clear discoverable state from an exception and its linked graph.

    Exact built-in descriptors avoid provider-controlled attribute, descriptor, and
    metaclass callbacks. Read-only storage such as an exception group's message
    cannot be changed in place, so public boundaries must return a fresh error that
    does not retain the source exception.
    """
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        linked: list[BaseException] = []
        group_exceptions: tuple[BaseException, ...] | None = None
        current_type = type(current)
        if issubclass(current_type, BaseExceptionGroup):
            try:
                if sys.version_info < (3, 11):
                    group_state = _base_exception_instance_dict(current)
                    raw_group_exceptions = (
                        _exact_string_state_value(group_state, "_exceptions")
                        if group_state is not None
                        else None
                    )
                else:
                    group_descriptor = type.__getattribute__(BaseExceptionGroup, "__dict__")[
                        "exceptions"
                    ]
                    raw_group_exceptions = group_descriptor.__get__(current, current_type)
                if type(raw_group_exceptions) is tuple:
                    group_exceptions = tuple(
                        cast(BaseException, candidate)
                        for candidate in raw_group_exceptions
                        if issubclass(type(candidate), BaseException)
                    )
                else:
                    group_exceptions = ()
                linked.extend(group_exceptions)
            except BaseException:
                pass
        for descriptor in (
            cast(Any, BaseException.__cause__),
            cast(Any, BaseException.__context__),
        ):
            try:
                candidate = descriptor.__get__(current, current_type)
            except BaseException:
                continue
            if issubclass(type(candidate), BaseException):
                linked.append(cast(BaseException, candidate))

        try:
            args = cast(Any, BaseException.args).__get__(current, current_type)
            _collect_nested_exceptions(args, linked)
        except BaseException:
            pass

        try:
            source_traceback = cast(Any, BaseException.__traceback__).__get__(current, current_type)
        except BaseException:
            source_traceback = None
        if source_traceback is not None:
            try:
                traceback.clear_frames(source_traceback)
            except BaseException:
                pass

        state = _base_exception_instance_dict(current)
        if state is not None:
            _collect_nested_exceptions(state, linked)
            state.clear()

        current_metadata = _static_type_metadata(current_type)
        if current_metadata is not None:
            _, current_mro = current_metadata
            for base in current_mro:
                base_metadata = _static_type_metadata(base)
                if base_metadata is None:
                    continue
                namespace, _ = base_metadata
                for descriptor in namespace.values():
                    if type(descriptor) is not types.MemberDescriptorType:
                        continue
                    try:
                        value = descriptor.__get__(current, current_type)
                        _collect_nested_exceptions(value, linked)
                    except BaseException:
                        pass
                    try:
                        descriptor.__delete__(current)
                    except BaseException:
                        pass

        if group_exceptions is None:
            safe_args: tuple[object, ...] = ()
        else:
            safe_args = (_DATA_REDACTED_ERROR_MESSAGE, group_exceptions)
        for descriptor, value in (
            (cast(Any, BaseException.args), safe_args),
            (cast(Any, BaseException.__traceback__), None),
            (cast(Any, BaseException.__cause__), None),
            (cast(Any, BaseException.__context__), None),
        ):
            try:
                descriptor.__set__(current, value)
            except BaseException:
                pass
        pending.extend(linked)


def _raise_data_redacted_error(error: BaseException) -> NoReturn:
    """Raise a detached redacted error from a frame that owns no payload data."""
    raise error from None


@dataclass
class RunErrorDetails:
    """Data collected from an agent run when an exception occurs."""

    input: str | list[TResponseInputItem]
    new_items: list[RunItem]
    raw_responses: list[ModelResponse]
    last_agent: Agent[Any]
    context_wrapper: RunContextWrapper[Any]
    input_guardrail_results: list[InputGuardrailResult]
    output_guardrail_results: list[OutputGuardrailResult]
    tool_input_guardrail_results: list[ToolInputGuardrailResult] = field(default_factory=list)
    """Tool input guardrail results accumulated from completed turns before the run failed."""

    tool_output_guardrail_results: list[ToolOutputGuardrailResult] = field(default_factory=list)
    """Tool output guardrail results accumulated from completed turns before the run failed."""

    def __str__(self) -> str:
        return pretty_print_run_error_details(self)


class AgentsException(Exception):
    """Base class for all exceptions in the Agents SDK."""

    run_data: RunErrorDetails | None

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.run_data = None


class MaxTurnsExceeded(AgentsException):
    """Exception raised when the maximum number of turns is exceeded."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ModelBehaviorError(AgentsException):
    """Exception raised when the model does something unexpected, e.g. calling a tool that doesn't
    exist, or providing malformed JSON.
    """

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ModelRefusalError(AgentsException):
    """Exception raised when the model refuses to produce the requested output."""

    refusal: str
    """The refusal text returned by the model."""

    def __init__(self, refusal: str):
        self.refusal = refusal
        super().__init__(f"Model refused to produce output: {refusal}")


class UserError(AgentsException):
    """Exception raised when the user makes an error using the SDK."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class MCPToolCancellationError(AgentsException):
    """Exception raised when an MCP tool call is internally cancelled."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ToolTimeoutError(AgentsException):
    """Exception raised when a function tool invocation exceeds its timeout."""

    tool_name: str
    timeout_seconds: float

    def __init__(self, tool_name: str, timeout_seconds: float):
        self.tool_name = tool_name
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Tool '{tool_name}' timed out after {timeout_seconds:g} seconds.")


class InputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a guardrail tripwire is triggered."""

    guardrail_result: InputGuardrailResult
    """The result data of the guardrail that was triggered."""

    def __init__(self, guardrail_result: InputGuardrailResult):
        self.guardrail_result = guardrail_result
        super().__init__(
            f"Guardrail {guardrail_result.guardrail.__class__.__name__} triggered tripwire"
        )


class OutputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a guardrail tripwire is triggered."""

    guardrail_result: OutputGuardrailResult
    """The result data of the guardrail that was triggered."""

    def __init__(self, guardrail_result: OutputGuardrailResult):
        self.guardrail_result = guardrail_result
        super().__init__(
            f"Guardrail {guardrail_result.guardrail.__class__.__name__} triggered tripwire"
        )


class ToolInputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a tool input guardrail tripwire is triggered."""

    guardrail: ToolInputGuardrail[Any]
    """The guardrail that was triggered."""

    output: ToolGuardrailFunctionOutput
    """The output from the guardrail function."""

    def __init__(self, guardrail: ToolInputGuardrail[Any], output: ToolGuardrailFunctionOutput):
        self.guardrail = guardrail
        self.output = output
        super().__init__(f"Tool input guardrail {guardrail.__class__.__name__} triggered tripwire")


class ToolOutputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a tool output guardrail tripwire is triggered."""

    guardrail: ToolOutputGuardrail[Any]
    """The guardrail that was triggered."""

    output: ToolGuardrailFunctionOutput
    """The output from the guardrail function."""

    def __init__(self, guardrail: ToolOutputGuardrail[Any], output: ToolGuardrailFunctionOutput):
        self.guardrail = guardrail
        self.output = output
        super().__init__(f"Tool output guardrail {guardrail.__class__.__name__} triggered tripwire")
