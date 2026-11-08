"""Keep captured MCP credentials out of errors returned by public SDK calls."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing, asynccontextmanager
from functools import wraps
from types import TracebackType
from typing import Any, TypeVar, cast

_Function = TypeVar("_Function", bound=Callable[..., Any])
_Declarations = Callable[[Any], Sequence[Any]]


def _strip_mcp_frames(error: BaseException) -> None:
    # Removing a traceback entry does not clear or finalize its frame. Cleanup
    # tasks and suspended context managers can still own and use that frame.
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        retained = []
        traceback = current.__traceback__
        while traceback is not None:
            if not traceback.tb_frame.f_globals.get("__name__", "").startswith("_agents_codex."):
                retained.append(traceback)
            traceback = traceback.tb_next
        rebuilt = None
        for entry in reversed(retained):
            rebuilt = TracebackType(rebuilt, entry.tb_frame, entry.tb_lasti, entry.tb_lineno)
        current.__traceback__ = rebuilt
        pending.extend(
            cause for cause in (current.__cause__, current.__context__) if cause is not None
        )
        children = getattr(current, "exceptions", ())
        if isinstance(children, tuple):
            pending.extend(child for child in children if isinstance(child, BaseException))


def _uses_mcp(args: tuple[Any, ...], kwargs: dict[str, Any], declarations: _Declarations) -> bool:
    from .mcp import _MCP

    try:
        if any(isinstance(tool, _MCP) for tool in kwargs.get("tools", ())):
            return True
    except TypeError:
        pass
    owner = args[0] if args else kwargs.get("self")
    try:
        return any(isinstance(tool, _MCP) for tool in declarations(owner))
    except (AttributeError, TypeError):
        # Preserve the ordinary error for an invalid or partially initialized owner.
        return False


def _protect_method(function: _Function, declarations: _Declarations) -> _Function:
    if inspect.isasyncgenfunction(function):

        @wraps(function)
        async def iterator(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            source, item = None, None
            try:
                source = function(*args, **kwargs)
                async with aclosing(source):
                    async for item in source:
                        yield item
            except BaseException as error:
                if _uses_mcp(args, kwargs, declarations):
                    _strip_mcp_frames(error)
                    raise error  # noqa: TRY201 - Python 3.10 must use the edited traceback.
                raise
            finally:
                del args, kwargs, source, item

        return cast(_Function, iterator)

    if inspect.isasyncgenfunction(getattr(function, "__wrapped__", None)):

        @wraps(function)
        @asynccontextmanager
        async def context(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            manager, value = None, None
            try:
                manager = function(*args, **kwargs)
                async with manager as value:
                    yield value
            except BaseException as error:
                if _uses_mcp(args, kwargs, declarations):
                    _strip_mcp_frames(error)
                    raise error  # noqa: TRY201 - Python 3.10 must use the edited traceback.
                raise
            finally:
                del args, kwargs, manager, value

        return cast(_Function, context)

    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def asynchronous(*args: Any, **kwargs: Any) -> Any:
            try:
                return await function(*args, **kwargs)
            except BaseException as error:
                if _uses_mcp(args, kwargs, declarations):
                    _strip_mcp_frames(error)
                    raise error  # noqa: TRY201 - Python 3.10 must use the edited traceback.
                raise
            finally:
                del args, kwargs

        return cast(_Function, asynchronous)

    @wraps(function)
    def synchronous(*args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            if _uses_mcp(args, kwargs, declarations):
                _strip_mcp_frames(error)
                raise error  # noqa: TRY201 - Python 3.10 must use the edited traceback.
            raise
        finally:
            del args, kwargs

    return cast(_Function, synchronous)


def _protect_mcp_methods(owner: type[Any], declarations: _Declarations) -> None:
    for name, function in tuple(vars(owner).items()):
        if name.startswith("_") and name not in {
            "__init__",
            "__anext__",
            "__setattr__",
            "__delattr__",
            "__hash__",
            "__reduce__",
        }:
            continue
        if isinstance(function, property):
            setattr(
                owner,
                name,
                property(
                    _protect_method(function.fget, declarations)
                    if function.fget is not None
                    else None,
                    _protect_method(function.fset, declarations)
                    if function.fset is not None
                    else None,
                    _protect_method(function.fdel, declarations)
                    if function.fdel is not None
                    else None,
                    function.__doc__,
                ),
            )
        elif inspect.isfunction(function):
            setattr(owner, name, _protect_method(function, declarations))
