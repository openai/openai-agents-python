"""Reuse wheel-built method factories for SDK-owned dataclass declarations."""

from __future__ import annotations

import builtins
import dataclasses
import sys
from builtins import compile as _compile, exec as _exec
from collections.abc import Callable
from hashlib import sha256
from importlib.util import MAGIC_NUMBER
from pathlib import Path
from types import BuiltinFunctionType, CodeType, FunctionType
from typing import TYPE_CHECKING, Any, cast
from zipimport import ZipImportError, zipimporter

_LEGACY_BUILDERS = ("_init_fn", "_repr_fn", "_cmp_fn", "_frozen_get_del_attr", "_hash_fn")


def driver_functions() -> dict[str, Any]:
    """Return the exact CPython functions that the adapter clones."""
    if sys.implementation.name != "cpython" or sys.version_info[:2] not in {
        (3, 12),
        (3, 13),
    }:
        return {}
    functions = vars(dataclasses)
    try:
        drivers = {name: functions[name] for name in ("dataclass", "_process_class")}
        if sys.version_info[:2] == (3, 13):
            drivers["add_fns_to_class"] = functions["_FuncBuilder"].add_fns_to_class
        else:
            drivers.update(
                {name: functions[name] for name in (*_LEGACY_BUILDERS, "_create_fn", "_hash_add")}
            )
    except (AttributeError, KeyError):
        return {}
    if any(
        type(function) is not FunctionType or function.__globals__ is not functions
        for function in drivers.values()
    ):
        return {}
    return drivers


def driver_fingerprint() -> str | None:
    """Hash code shape without checkout paths and source line positions."""
    drivers = driver_functions()
    if not drivers:
        return None

    def code_shape(value: Any) -> Any:
        if isinstance(value, CodeType):
            return (
                value.co_code.hex(),
                value.co_names,
                value.co_varnames,
                value.co_flags,
                value.co_argcount,
                value.co_posonlyargcount,
                value.co_kwonlyargcount,
                value.co_cellvars,
                value.co_freevars,
                getattr(value, "co_exceptiontable", b"").hex(),
                tuple(code_shape(item) for item in value.co_consts),
            )
        if isinstance(value, frozenset):
            return tuple(sorted(map(repr, value)))
        return value

    functions = tuple((name, code_shape(function.__code__)) for name, function in drivers.items())
    return sha256(MAGIC_NUMBER + repr(functions).encode()).hexdigest()


def _clone(function: FunctionType, namespace: dict[str, Any]) -> FunctionType:
    clone = FunctionType(
        function.__code__, namespace, function.__name__, function.__defaults__, function.__closure__
    )
    clone.__kwdefaults__ = function.__kwdefaults__
    return clone


def _load_factories() -> dict[bytes, FunctionType]:
    if sys.flags.optimize or sys._xoptions or "exec" in vars(dataclasses):
        return {}
    if (
        type(_exec) is not BuiltinFunctionType
        or _exec.__name__ != "exec"
        or type(_compile) is not BuiltinFunctionType
        or _compile.__name__ != "compile"
    ):
        return {}
    fingerprint = driver_fingerprint()
    if fingerprint is None:
        return {}
    minor = sys.version_info.minor
    path = Path(__file__).with_name(f"_dataclass_factories_3{minor}-{fingerprint}.zip")
    if not path.is_file():
        return {}
    try:
        code = zipimporter(str(path)).get_code("_dataclass_factories")
    except (EOFError, ValueError, ZipImportError):
        return {}
    # zipimport may compile the source member if the bytecode failed validation.
    if code is None or code.co_filename != "_agents_codex/_dataclass_factories.py":
        return {}
    generated: dict[str, Any] = {"__name__": f"{__package__}._dataclass_factories"}
    _exec(code, generated)
    factories = generated.get("FACTORIES")
    if type(factories) is not dict or any(
        type(key) is not bytes
        or len(key) != 32
        or type(factory) is not FunctionType
        or factory.__closure__ is not None
        for key, factory in factories.items()
    ):
        return {}
    return cast(dict[bytes, FunctionType], factories)


def _scoped_dataclass(factories: dict[bytes, FunctionType]) -> Callable[..., Any]:
    stock = vars(dataclasses)

    # Exact source keys cover the current field layout and generator inputs.
    # The stock builder still supplies the actual types and default values.
    def exec_factory(source: str, namespace: dict[str, Any], result: dict[str, Any]) -> None:
        execute = stock.get("exec", builtins.exec)
        if execute is not _exec:
            execute(source, namespace, result)
            return
        factory = factories.get(sha256(source.encode()).digest())
        if factory is None:
            _exec(_compile(source, "<string>", "exec", dont_inherit=True), namespace, result)
        else:
            result["__create_fn__"] = _clone(factory, namespace)

    if sys.version_info[:2] == (3, 13):
        builder: type[Any] = stock["_FuncBuilder"]

        class ScopedBuilder(builder):  # type: ignore[misc]
            add_fns_to_class = _clone(builder.add_fns_to_class, {**stock, "exec": exec_factory})

        process_globals = {**stock, "_FuncBuilder": ScopedBuilder}
    else:
        create = _clone(stock["_create_fn"], {**stock, "exec": exec_factory})
        builder_globals = {**stock, "_create_fn": create}
        builders = {name: _clone(stock[name], builder_globals) for name in _LEGACY_BUILDERS}
        stock_hash_add = stock["_hash_add"]
        hash_add = _clone(stock_hash_add, {**stock, **builders})
        actions = {
            key: hash_add if action is stock_hash_add else action
            for key, action in stock["_hash_action"].items()
        }
        process_globals = {**stock, **builders, "_hash_action": actions}

    process = _clone(stock["_process_class"], process_globals)
    return _clone(stock["dataclass"], {**stock, "_process_class": process})


if TYPE_CHECKING:
    from dataclasses import dataclass as dataclass
else:
    _factories = _load_factories()
    dataclass = _scoped_dataclass(_factories) if _factories else dataclasses.dataclass
