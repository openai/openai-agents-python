"""Computer drivers executed by the native Session's admitted callback owner."""

from __future__ import annotations

import asyncio as asyncio
import inspect as inspect
import json as json
import threading as threading
import weakref as weakref
from collections.abc import (
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    Callable as Callable,
)
from contextlib import AsyncExitStack, asynccontextmanager as asynccontextmanager
from copy import deepcopy as deepcopy
from dataclasses import dataclass as dataclass, field as field
from typing import Any as Any, Literal as Literal, Protocol as Protocol

from pydantic import (
    BaseModel as BaseModel,
    ConfigDict as ConfigDict,
    Field as Field,
    JsonValue as JsonValue,
    TypeAdapter as TypeAdapter,
)

from . import tools as _tool_output_types
from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._function_tools import FunctionTool as FunctionTool, ToolContext as ToolContext
from ._public import preserve_module_identity as _preserve_module_identity
from .tools import ToolOutput as ToolOutput, _CapturedFunction as _CapturedFunction

Button = Literal["left", "right", "wheel", "back", "forward"]
Environment = Literal["mac", "windows", "ubuntu", "browser"]
_ComputerResultData = Callable[
    [ToolContext[Any], dict[str, Any]],
    dict[str, JsonValue] | None | Awaitable[dict[str, JsonValue] | None],
]


class Computer(Protocol):
    """A sync or async driver. Screenshots return base64-encoded PNG data.

    Mouse methods may also accept keyword-only ``keys`` for held modifiers.
    Passing a driver directly borrows it; the Session never closes it.
    """

    def screenshot(self) -> str | Awaitable[str]: ...
    def click(self, x: float, y: float, button: Button) -> None | Awaitable[None]: ...
    def double_click(self, x: float, y: float) -> None | Awaitable[None]: ...
    def scroll(
        self, x: float, y: float, scroll_x: float, scroll_y: float
    ) -> None | Awaitable[None]: ...
    def type(self, text: str) -> None | Awaitable[None]: ...
    def wait(self) -> None | Awaitable[None]: ...
    def move(self, x: float, y: float) -> None | Awaitable[None]: ...
    def keypress(self, keys: list[str]) -> None | Awaitable[None]: ...
    def drag(self, path: list[tuple[float, float]]) -> None | Awaitable[None]: ...


@_sdk_dataclass(frozen=True)
class ComputerProvider:
    """Acquire one driver lazily per Session and dispose it after callbacks join.

    ``dispose`` is invoked once for each successfully acquired driver, including
    after driver failure or cancellation. Omit it for a borrowed factory result.
    Disposal receives the driver, without retaining expired invocation authority.
    On first use of a borrowed factory result, policies authorize creation and
    run again under the resolved device gate before any action or screenshot.
    """

    create: Callable[[ToolContext[Any]], Computer | Awaitable[Computer]] = field(repr=False)
    dispose: Callable[[Computer], None | Awaitable[None]] | None = field(default=None, repr=False)


class ComputerSafetyError(RuntimeError):
    """The application's policy did not acknowledge pending computer checks."""


class _ComputerCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["computer_call"]
    call_id: str
    action: dict[str, Any] | None = None
    actions: list[dict[str, Any]] | None = None
    pending_safety_checks: list[dict[str, Any]] = Field(default_factory=list)


@_sdk_dataclass(frozen=True, eq=False)
class _Declaration:
    source: Computer | ComputerProvider = field(repr=False)
    approve: Callable[[ToolContext[Any], list[dict[str, Any]]], bool | Awaitable[bool]] | None = (
        field(repr=False)
    )
    on_safety_check: (
        Callable[[ToolContext[Any], list[dict[str, Any]]], bool | Awaitable[bool]] | None
    )
    extract_application_data: _ComputerResultData | None = field(repr=False)


class _DeviceGate:
    def __init__(self, borrowed: Computer | None = None) -> None:
        # A registry entry cannot outlive the physical object identity.
        self._borrowed = borrowed
        self._mutex = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None
        self._users = 0

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        with self._mutex:
            if self._users and self._loop is not loop:
                raise RuntimeError("Computer is already in use by another event loop")
            if not self._users:
                self._loop, self._lock = loop, asyncio.Lock()
            self._users += 1
            lock = self._lock
        assert lock is not None
        try:
            async with lock:
                yield
        finally:
            with self._mutex:
                self._users -= 1
                if not self._users:
                    self._loop, self._lock = None, None


# Weak gate values avoid requiring hashable/weak-referenceable user drivers.
# A live declaration or lease keeps the borrowed driver (and its ID) alive.
_borrowed_gates: weakref.WeakValueDictionary[int, _DeviceGate] = weakref.WeakValueDictionary()
_borrowed_gates_mutex = threading.Lock()


def _borrowed_gate(driver: Computer) -> _DeviceGate:
    with _borrowed_gates_mutex:
        gate = _borrowed_gates.get(id(driver))
        if gate is None:
            gate = _DeviceGate(driver)
            _borrowed_gates[id(driver)] = gate
        return gate


@_sdk_dataclass
class _Lease:
    lock: _DeviceGate = field(default_factory=_DeviceGate)
    driver: Computer | None = None
    disposer: Callable[[Computer], None | Awaitable[None]] | None = None


class _ComputerResources:
    def __init__(self) -> None:
        self._leases: dict[_Declaration, _Lease] = {}
        self._closed = False

    def lease(self, declaration: _Declaration) -> _Lease:
        if self._closed:
            raise RuntimeError("Computer resources are closed")
        lease = self._leases.get(declaration)
        if lease is None:
            gate = (
                _DeviceGate()
                if isinstance(declaration.source, ComputerProvider)
                else _borrowed_gate(declaration.source)
            )
            lease = _Lease(lock=gate)
            self._leases[declaration] = lease
        return lease

    async def close(self) -> None:
        from .session import _call_callback, _error_contexts, _retain_cleanup_error

        self._closed = True
        errors: list[BaseException] = []
        # Session joins every callback before disposal, including sync workers.
        for lease in self._leases.values():
            driver, dispose = lease.driver, lease.disposer
            lease.driver = None
            lease.disposer = None
            if driver is not None and dispose is not None:
                try:
                    await _call_callback(dispose, lambda: True, driver)
                except BaseException as error:
                    errors.append(error)
        self._leases.clear()
        if errors:
            first = errors[0]
            for other in errors[1:]:
                _retain_cleanup_error(first, other, _error_contexts(first.__context__))
            raise first


_ACTION_FIELDS = {
    "click": ("x", "y", "button"),
    "double_click": ("x", "y"),
    "scroll": ("x", "y", "scroll_x", "scroll_y"),
    "type": ("text",),
    "wait": (),
    "move": ("x", "y"),
    "keypress": ("keys",),
    "drag": ("path",),
    "screenshot": (),
}


# Validate every action before the first device effect. Preserve the original
# pending objects separately; policy callbacks cannot rewrite acknowledgments.
def _actions(call: _ComputerCall) -> list[dict[str, Any]]:
    actions = call.actions or ([call.action] if call.action is not None else [])
    for action in actions:
        kind = action.get("type")
        if not isinstance(kind, str) or kind not in _ACTION_FIELDS:
            raise ValueError("Unsupported computer action")
        if any(name not in action for name in _ACTION_FIELDS[kind]):
            raise ValueError(f"Incomplete computer {kind} action")
        # These values cross into application drivers rather than a provider.
        for name in ("x", "y", "scroll_x", "scroll_y"):
            if name in action:
                TypeAdapter(float).validate_python(action[name], strict=True)
        if "button" in action:
            TypeAdapter(Button).validate_python(action["button"], strict=True)
        if "text" in action:
            TypeAdapter(str).validate_python(action["text"], strict=True)
        if "keys" in action and (kind == "keypress" or action["keys"] is not None):
            TypeAdapter(list[str]).validate_python(action["keys"], strict=True)
        if "path" in action:
            TypeAdapter(list[dict[str, Any]]).validate_python(action["path"], strict=True)
            if any("x" not in point or "y" not in point for point in action["path"]):
                raise ValueError("Computer drag waypoints require x and y")
            for point in action["path"]:
                TypeAdapter(float).validate_python(point["x"], strict=True)
                TypeAdapter(float).validate_python(point["y"], strict=True)
    return actions


async def _call_owned_callback(
    callback: Callable[..., Any],
    admit: Callable[[], bool],
    /,
    *args: Any,
    _retain: Callable[[Any], Any] | None = None,
    **kwargs: Any,
) -> Any:
    from .session import _call_callback

    issued: Awaitable[Any] | None = None
    completed = False

    def finish(value: Any) -> Any:
        nonlocal completed
        completed = True
        return _retain(value) if _retain is not None else value

    def enter() -> Any:
        nonlocal issued
        value = callback(*args, **kwargs)
        if not inspect.isawaitable(value):
            return finish(value)
        issued = value

        async def settle() -> Any:
            nonlocal issued
            if not inspect.iscoroutine(value):
                issued = asyncio.ensure_future(value)
                return finish(await asyncio.shield(issued))
            return finish(await value)

        return settle()

    try:
        return await _call_callback(enter, admit)
    finally:
        # A sync worker may return an already-issued operation after revocation.
        # Its device lease must outlive physical completion, including repeated
        # cancellation. Never start an unentered coroutine after revocation.
        if issued is not None and not inspect.iscoroutine(issued) and not completed:
            operation = asyncio.ensure_future(issued)
            cancelled: asyncio.CancelledError | None = None
            while not operation.done():
                try:
                    await asyncio.shield(operation)
                except asyncio.CancelledError as error:
                    cancelled = cancelled or error
            finish(operation.result())
            if cancelled is not None:
                raise cancelled
        elif (
            inspect.iscoroutine(issued)
            and inspect.getcoroutinestate(issued) == inspect.CORO_CREATED
        ):
            issued.close()


@_sdk_dataclass(frozen=True, kw_only=True)
class _CapturedComputer(_CapturedFunction):
    computer: _Declaration = field(repr=False)

    def _arguments(self, parsed: BaseModel) -> dict[str, Any]:
        return parsed.model_dump()

    async def invoke(
        self,
        resources: _ComputerResources,
        admit: Callable[[], bool],
        context: ToolContext[Any],
        call: _ComputerCall,
    ) -> str | ToolOutput:
        async def invoke(callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            if not admit():
                raise asyncio.CancelledError
            result = await _call_owned_callback(callback, admit, *args, **kwargs)
            if not admit():
                raise asyncio.CancelledError
            return result

        actions = _actions(call)
        pending = deepcopy(call.pending_safety_checks)
        lease = resources.lease(self.computer)

        async def authorize() -> None:
            if not admit():
                raise asyncio.CancelledError
            if self.computer.approve is not None:
                if await invoke(self.computer.approve, context, deepcopy(actions)) is not True:
                    raise ComputerSafetyError("Computer call was not approved")
            if pending:
                policy = self.computer.on_safety_check
                if policy is None or await invoke(policy, context, deepcopy(pending)) is not True:
                    raise ComputerSafetyError(
                        "Pending computer safety checks require acknowledgment"
                    )

        source = self.computer.source
        borrowed_factory = isinstance(source, ComputerProvider) and source.dispose is None
        async with lease.lock.hold(), AsyncExitStack() as device:
            resolving = borrowed_factory and lease.driver is None
            if borrowed_factory and lease.driver is not None:
                await device.enter_async_context(_borrowed_gate(lease.driver).hold())
            await authorize()
            if lease.driver is None:
                if isinstance(source, ComputerProvider):
                    # Retain an acquired driver before rechecking admission: a
                    # canceled sync creator can still return an owned resource.
                    def retain(driver: Computer) -> Computer:
                        lease.driver = driver
                        lease.disposer = source.dispose
                        return driver

                    await _call_owned_callback(source.create, admit, context, _retain=retain)
                else:
                    lease.driver = source
                if not admit():
                    raise asyncio.CancelledError
            driver = lease.driver
            if driver is None:
                raise TypeError("ComputerProvider.create must return a driver")
            if resolving:
                await device.enter_async_context(_borrowed_gate(driver).hold())
                await authorize()
            screenshot: str | None = None
            result: Any = None
            for action in actions:
                kind = action["type"]
                callback = getattr(driver, kind)
                arguments = [action[key] for key in _ACTION_FIELDS[kind]]
                if kind == "drag":
                    arguments = [[(point["x"], point["y"]) for point in action["path"]]]
                kwargs: dict[str, Any] = {}
                keys = (
                    action.get("keys")
                    if kind in ("click", "double_click", "scroll", "move", "drag")
                    else None
                )
                if keys:
                    try:
                        parameters = list(inspect.signature(callback).parameters.values())
                    except (TypeError, ValueError):
                        parameters = []
                    if any(
                        parameter.kind == inspect.Parameter.VAR_KEYWORD
                        or (
                            parameter.name == "keys"
                            and parameter.kind
                            in (
                                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                inspect.Parameter.KEYWORD_ONLY,
                            )
                        )
                        for parameter in parameters
                    ):
                        kwargs["keys"] = keys
                result = await invoke(callback, *arguments, **kwargs)
                screenshot = result if kind == "screenshot" else None
            if not actions or actions[-1]["type"] != "screenshot":
                screenshot = await invoke(driver.screenshot)
            if not isinstance(screenshot, str) or not screenshot:
                screenshot = result = None
                raise TypeError("Computer screenshot must return base64 PNG data")
            response = {
                "output": {
                    "type": "computer_screenshot",
                    "image_url": f"data:image/png;base64,{screenshot}",
                },
                "acknowledged_safety_checks": pending,
            }
            wire = json.dumps(response, allow_nan=False)
            extract = self.computer.extract_application_data
            if extract is None:
                return wire
            data = await invoke(
                extract,
                context,
                {"type": "computer_call_output", "call_id": call.call_id, **response},
            )
            if data is None:
                return wire
            tool_output = ToolOutput.text(wire)
            tool_output.application_data = data
            return tool_output


@_sdk_dataclass(frozen=True, kw_only=True)
class _ComputerTool(FunctionTool[Any, str, Any]):
    computer_spec: str = field(repr=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            self.name != "computer"
            or self.namespace is not None
            or self.defer_loading
            or self.finish_turn
            or self.before is not None
            or self.after is not None
            or self.callback_timeout is not None
            or self.timeout_behavior != "raise_exception"
            or self.timeout_error_function is not None
            or self.input_type is not _ComputerCall
            or self.output_type is not str
        ):
            raise TypeError(
                "Computer declarations use computer_tool's approval and safety policies"
            )

    def _spec_json(self) -> str:
        return self.computer_spec


def computer_tool(
    computer: Computer | ComputerProvider,
    *,
    tool_type: Literal["computer", "computer_use_preview"] = "computer",
    dimensions: tuple[int, int] | None = None,
    environment: Environment | None = None,
    approve: Callable[[ToolContext[Any], list[dict[str, Any]]], bool | Awaitable[bool]]
    | None = None,
    on_safety_check: (
        Callable[[ToolContext[Any], list[dict[str, Any]]], bool | Awaitable[bool]] | None
    ) = None,
    extract_application_data: _ComputerResultData | None = None,
) -> FunctionTool[Any, str, Any]:
    """Declare a native computer tool; Core owns its call/result continuation.

    GA accepts batched actions. Preview requires explicit display dimensions
    and environment. Pending checks require ``on_safety_check`` to return True
    before any device effect; missing/false acknowledgment fails the call.
    ``approve``, when supplied, must return True for every call, including calls
    without pending checks. Direct drivers are borrowed. Use ComputerProvider
    for per-Session acquisition
    and explicit disposal. Sessions serialize calls against each declaration.
    ``extract_application_data`` receives the tool context and a detached
    computer output. Core acceptance makes the optional JSON object available
    through ``application_tool_outputs`` on the completed turn.
    """
    if tool_type == "computer_use_preview" and (dimensions is None or environment is None):
        raise ValueError("Preview computer requires dimensions and environment")
    if tool_type not in ("computer", "computer_use_preview"):
        raise ValueError("Unknown computer tool type")
    spec: dict[str, Any] = {"type": "computer", "toolType": tool_type}
    if dimensions is not None:
        spec.update(displayWidth=dimensions[0], displayHeight=dimensions[1])
    if environment is not None:
        spec["environment"] = environment
    declaration = _Declaration(computer, approve, on_safety_check, extract_application_data)

    def bind(
        context: object, policy_context: object, tool: FunctionTool[Any, Any, Any]
    ) -> _CapturedComputer:
        output_adapter: TypeAdapter[Any] = TypeAdapter(
            str | ToolOutput if extract_application_data is not None else str
        )
        # ToolOutput's annotations need their implementation namespace on a cold import.
        output_adapter.rebuild(_types_namespace=vars(_tool_output_types))
        return _CapturedComputer(
            name="computer",
            spec_json=tool._spec_json(),
            model=_ComputerCall,
            field_names=(),
            output_adapter=output_adapter,
            callback=bind,
            context_parameter=inspect.Parameter("context", inspect.Parameter.POSITIONAL_ONLY),
            callback_timeout=None,
            timeout_behavior="raise_exception",
            timeout_error_function=None,
            before=None,
            after=None,
            context=context,
            computer=declaration,
        )

    return _ComputerTool(
        name="computer",
        description="Operate the application's computer",
        input_type=_ComputerCall,
        output_type=str,
        _bind=bind,
        computer_spec=json.dumps(spec),
    )


_preserve_module_identity(globals(), "agents.computer")
