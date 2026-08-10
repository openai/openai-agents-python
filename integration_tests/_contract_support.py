from __future__ import annotations

import dataclasses
import enum
import importlib
import inspect
import json
import logging
import sys
import traceback
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from importlib.util import find_spec
from pathlib import Path
from types import FunctionType, TracebackType
from typing import Any, cast


def load_api_contract(path: Path) -> dict[str, Any]:
    contract = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    _add_legacy_literal_types(contract)
    return contract


def _add_legacy_literal_types(value: object) -> None:
    if isinstance(value, dict):
        if value.get("kind") == "literal" and "value" in value and "type" not in value:
            literal = value["value"]
            value["type"] = f"{type(literal).__module__}.{type(literal).__qualname__}"
        for child in value.values():
            _add_legacy_literal_types(child)
    elif isinstance(value, list):
        for child in value:
            _add_legacy_literal_types(child)


def _redaction_observables(
    error: BaseException | None,
    records: Iterable[logging.LogRecord],
) -> str:
    values: list[str] = []
    seen: set[int] = set()

    def visit_exception_state(value: object) -> None:
        value_id = id(value)
        if value_id in seen:
            return
        seen.add(value_id)

        if isinstance(value, BaseException):
            state = vars(value)
            values.append(repr(state))
            visit_exception_state(value.args)
            visit_exception_state(value.__cause__)
            visit_exception_state(value.__context__)
            visit_exception_state(value.__traceback__)
            visit_exception_state(state)
        elif isinstance(value, TracebackType):
            module_name = value.tb_frame.f_globals.get("__name__", "")
            if module_name == "agents" or module_name.startswith("agents."):
                visit_exception_state(value.tb_frame.f_locals)
            visit_exception_state(value.tb_next)
        elif isinstance(value, Mapping):
            for key, item in value.items():
                visit_exception_state(key)
                visit_exception_state(item)
        elif (
            dataclasses.is_dataclass(value)
            and not isinstance(value, type)
            and (type(value).__module__ == "agents" or type(value).__module__.startswith("agents."))
        ):
            for field in dataclasses.fields(value):
                visit_exception_state(getattr(value, field.name))
        elif isinstance(value, list | tuple | set | frozenset):
            for item in value:
                visit_exception_state(item)
        elif isinstance(value, str | bytes | int | float | bool | None):
            values.append(repr(value))

    if error is not None:
        values.extend(
            (
                str(error),
                repr(error),
                repr(error.__cause__),
                repr(error.__context__),
                "".join(traceback.format_exception(error)),
            )
        )
        visit_exception_state(error)
    for record in records:
        values.extend((record.getMessage(), repr(record.args), repr(record.__dict__)))
        visit_exception_state(record.__dict__)
        if record.exc_info is not None:
            values.append("".join(traceback.format_exception(*record.exc_info)))
            visit_exception_state(record.exc_info)
    return "\n".join(values)


def _deserialize_common_sandbox_session_state(payload: dict[str, object]) -> Any:
    from agents.sandbox.session import SandboxSessionState

    persisted_payload = deepcopy(payload)
    state = SandboxSessionState.model_validate(persisted_payload)
    return SandboxSessionState._mark_persisted_path_grants(state, payload=persisted_payload)


def _default_contract(value: object) -> dict[str, object]:
    if value is inspect.Parameter.empty or value is dataclasses.MISSING:
        return {"kind": "required"}
    if value.__class__.__name__ == "_HAS_DEFAULT_FACTORY_CLASS":
        return {"kind": "factory"}
    if value is None or isinstance(value, bool | int | float | str):
        return {
            "kind": "literal",
            "type": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value,
        }
    from agents.mcp.server import _UNSET as mcp_failure_error_unset
    from agents.retry import _UNSET as retry_unset
    from agents.tool import _UNSET_FAILURE_ERROR_FUNCTION as failure_error_function_unset
    from agents.tool_context import _MISSING as tool_context_missing

    sentinel_identities = (
        (retry_unset, "agents.retry._UNSET"),
        (mcp_failure_error_unset, "agents.mcp.server._UNSET"),
        (failure_error_function_unset, "agents.tool._UNSET_FAILURE_ERROR_FUNCTION"),
        (tool_context_missing, "agents.tool_context._MISSING"),
    )
    for sentinel, identity in sentinel_identities:
        if value is sentinel:
            return {"kind": "sentinel", "identity": identity}
    value_type = f"{type(value).__module__}.{type(value).__qualname__}"
    if value_type == "pydantic.fields.FieldInfo":
        return {"kind": "repr", "type": value_type, "value": repr(value)}
    if isinstance(value, enum.Enum):
        return {
            "kind": "enum",
            "type": value_type,
            "name": value.name,
            "value": _default_contract(value.value),
        }
    if isinstance(value, tuple | list):
        return {
            "kind": "sequence",
            "type": value_type,
            "items": [_default_contract(item) for item in value],
        }
    if isinstance(value, dict):
        return {
            "kind": "mapping",
            "type": value_type,
            "items": [
                [_default_contract(key), _default_contract(item)] for key, item in value.items()
            ],
        }
    if value_type.startswith("agents.") and callable(getattr(value, "model_dump", None)):
        dumped = value.model_dump(mode="python")  # type: ignore[attr-defined]
        return {
            "kind": "model",
            "type": value_type,
            "value": _default_contract(dumped),
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "kind": "dataclass",
            "type": value_type,
            "fields": [
                {"name": field.name, "value": _default_contract(getattr(value, field.name))}
                for field in dataclasses.fields(value)
            ],
        }
    if type(value) is FunctionType and value.__module__.startswith("agents."):
        return {
            "kind": "callable",
            "identity": f"{value.__module__}.{value.__qualname__}",
        }
    raise TypeError(f"Unsupported public API default value: {value_type}")


def _parameter_records(
    parameters: Iterable[inspect.Parameter],
) -> list[dict[str, object]]:
    return [
        {
            "name": parameter.name,
            "kind": parameter.kind.name,
            "default": _default_contract(parameter.default),
        }
        for parameter in parameters
    ]


def _signature(value: Callable[..., Any]) -> inspect.Signature:
    return inspect.signature(value)


def _parameter_contract(value: Callable[..., Any]) -> list[dict[str, object]]:
    parameters = list(_signature(value).parameters.values())
    if issubclass(type(value), type) and issubclass(cast(type, value), enum.Enum):
        parameters = list(_signature(value.__new__).parameters.values())[1:]
    return _parameter_records(parameters)


def _dataclass_field_contract(value: object) -> list[dict[str, object]]:
    if not dataclasses.is_dataclass(value):
        return []
    result: list[dict[str, object]] = []
    for field in dataclasses.fields(value):
        if field.name.startswith("_"):
            continue
        if field.default_factory is not dataclasses.MISSING:
            factory = cast(Callable[..., Any], field.default_factory)
            default_contract: dict[str, object] = {
                "kind": "factory",
                "factory": f"{factory.__module__}.{factory.__qualname__}",
            }
        else:
            default_contract = _default_contract(field.default)
        result.append(
            {
                "name": field.name,
                "init": field.init,
                "default": default_contract,
            }
        )
    return result


def _callable_kind(value: Callable[..., Any]) -> str | None:
    if issubclass(type(value), type):
        return "class"
    if type(value) is FunctionType:
        return "function"
    return None


def _enum_member_contract(value: object) -> list[dict[str, object]] | None:
    if not (issubclass(type(value), type) and issubclass(cast(type, value), enum.Enum)):
        return None
    enum_type = cast(type[enum.Enum], value)
    members: list[dict[str, object]] = []
    for name, member in enum_type.__members__.items():
        member_value = member.value
        if member_value is None or isinstance(member_value, bool | int | float | str):
            value_contract: dict[str, object] = {
                "kind": "literal",
                "type": f"{type(member_value).__module__}.{type(member_value).__qualname__}",
                "value": member_value,
            }
        else:
            raise TypeError(
                f"Unsupported public enum value for "
                f"{enum_type.__module__}.{enum_type.__qualname__}."
                f"{name}: {type(member_value).__module__}.{type(member_value).__qualname__}"
            )
        members.append({"name": name, "value": value_contract})
    return members


def _class_member_contract(descriptor: object) -> dict[str, object] | None:
    descriptor_type = type(descriptor)
    if descriptor_type is staticmethod:
        binding = "static"
        function = object.__getattribute__(descriptor, "__func__")
        skip_first = False
    elif descriptor_type is classmethod:
        binding = "class"
        function = object.__getattribute__(descriptor, "__func__")
        skip_first = True
    elif type(descriptor) is FunctionType:
        binding = "instance"
        function = descriptor
        skip_first = True
    else:
        return None
    if type(function) is not FunctionType:
        return None
    try:
        parameters = list(_signature(function).parameters.values())
    except (TypeError, ValueError):
        return None
    if skip_first:
        if not parameters:
            return None
        parameters = parameters[1:]
    return {
        "binding": binding,
        "execution_kind": _function_execution_kind(function),
        "parameters": _parameter_records(parameters),
    }


def _function_execution_kind(value: object) -> str:
    if inspect.isasyncgenfunction(value):
        return "async_generator"
    if inspect.iscoroutinefunction(value):
        return "coroutine"
    if inspect.isgeneratorfunction(value):
        return "generator"
    return "sync"


def _sdk_public_class_descriptor(value: type, name: str) -> object | None:
    for owner in value.__mro__:
        namespace = vars(owner)
        if name not in namespace:
            continue
        owner_module = owner.__module__
        if owner is value or (
            isinstance(owner_module, str)
            and (owner_module == "agents" or owner_module.startswith("agents."))
        ):
            return cast(object, inspect.getattr_static(value, name))
        return None
    return None


def _public_class_member_contract(value: object) -> dict[str, dict[str, object]]:
    if not issubclass(type(value), type):
        return {}
    class_value = cast(type, value)
    value_identity = f"{class_value.__module__}.{class_value.__qualname__}"
    candidate_names: list[str] = []
    seen_names: set[str] = set()

    def add_candidate_names(namespace: Mapping[str, object]) -> None:
        for name in namespace:
            if name in seen_names:
                continue
            seen_names.add(name)
            candidate_names.append(name)

    add_candidate_names(vars(class_value))
    for base in class_value.__mro__[1:]:
        base_module = base.__module__
        if isinstance(base_module, str) and (
            base_module == "agents" or base_module.startswith("agents.")
        ):
            add_candidate_names(vars(base))
    members: dict[str, dict[str, object]] = {}
    for name in candidate_names:
        if name.startswith("_"):
            continue
        descriptor = _sdk_public_class_descriptor(class_value, name)
        if descriptor is None:
            continue
        try:
            member = _class_member_contract(descriptor)
        except TypeError as error:
            raise TypeError(
                f"Unable to contract public method {value_identity}.{name}: {error}"
            ) from None
        if member is not None:
            members[name] = member
    return members


def _callable_contract(value: Callable[..., Any]) -> dict[str, Any]:
    kind = _callable_kind(value)
    if kind is None:
        raise TypeError(f"Unsupported public callable type: {type(value)!r}")
    contract: dict[str, Any] = {
        "kind": kind,
        "parameters": _parameter_contract(value),
        "dataclass_fields": _dataclass_field_contract(value),
    }
    if kind == "function":
        contract["execution_kind"] = _function_execution_kind(value)
    enum_members = _enum_member_contract(value)
    if enum_members is not None:
        contract["enum_members"] = enum_members
    if kind == "class":
        contract["members"] = _public_class_member_contract(value)
    return contract


def build_released_api_contract(
    contract: dict[str, Any],
    *,
    baseline: str,
    baseline_commit: str,
    agents_module: Any | None = None,
) -> dict[str, Any]:
    """Build the next rolling release contract from the current public surface."""
    agents = agents_module or importlib.import_module("agents")
    compatibility_errors = validate_released_api_contract(contract, agents_module=agents)
    if compatibility_errors:
        details = "\n".join(f"- {error}" for error in compatibility_errors)
        raise ValueError(f"Cannot promote an incompatible released API contract:\n{details}")

    current_exports = list(agents.__all__)
    if not all(type(name) is str for name in current_exports):
        raise ValueError("agents.__all__ must contain only strings")
    if len(current_exports) != len(set(current_exports)):
        raise ValueError("agents.__all__ must not contain duplicate exports")

    missing_bindings = [name for name in current_exports if not hasattr(agents, name)]
    if missing_bindings:
        raise ValueError(f"agents.__all__ contains missing bindings: {missing_bindings!r}")

    released_export_order = list(contract["required_top_level_exports"])
    released_exports = set(released_export_order)
    current_export_names = set(current_exports)
    ordered_exports = [name for name in released_export_order if name in current_export_names]
    ordered_exports.extend(name for name in current_exports if name not in released_exports)
    tracked_callables = set(contract["callables"])
    callables: dict[str, Any] = {}
    for name in ordered_exports:
        value = getattr(agents, name)
        kind = _callable_kind(value)
        should_track = name in tracked_callables
        if not should_track and kind is not None:
            try:
                _signature(value)
            except (TypeError, ValueError):
                continue
            should_track = True
        if should_track:
            callables[name] = _callable_contract(value)

    top_level_callable_ids = {
        id(getattr(agents, name)) for name in callables if not name.startswith("agents.")
    }
    for entry in contract["canonical_imports"]:
        module_name = entry["module"]
        if module_name == "agents":
            continue
        qualified_name = f"{module_name}.{entry['name']}"
        module = _import_contract_module(module_name, agents_module)
        value = getattr(module, entry["name"])
        if id(value) in top_level_callable_ids:
            continue
        kind = _callable_kind(value)
        if kind is None:
            continue
        try:
            _signature(value)
        except (TypeError, ValueError):
            continue
        callables[qualified_name] = _callable_contract(value)

    updated = deepcopy(contract)
    updated["baseline"] = baseline
    updated["required_top_level_exports"] = ordered_exports
    updated["callables"] = callables
    excluded_submodule_exports = set(contract.get("submodule_export_exclusions", []))
    required_submodule_exports: dict[str, dict[str, Any]] = {}
    for module_name in contract["public_modules"]:
        if module_name == "agents" or module_name in excluded_submodule_exports:
            continue
        try:
            module = _import_contract_module(module_name, agents_module)
        except Exception as error:
            if _matches_platform_import_error(contract, module_name, error):
                continue
            raise
        previous_module_contract = contract.get("required_submodule_exports", {}).get(
            module_name, {}
        )
        module_contract = _submodule_export_contract(
            module,
            optional_bindings=previous_module_contract.get("optional_bindings", {}),
            optional_exports=previous_module_contract.get("optional_exports", {}),
        )
        if module_contract is not None:
            required_submodule_exports[module_name] = module_contract
    updated["required_submodule_exports"] = required_submodule_exports

    updated_errors = validate_released_api_contract(updated, agents_module=agents)
    if updated_errors:
        details = "\n".join(f"- {error}" for error in updated_errors)
        raise ValueError(f"Cannot promote an invalid released API contract:\n{details}")

    surface_keys = (
        "canonical_imports",
        "callables",
        "platform_import_errors",
        "public_properties",
        "public_modules",
        "required_submodule_exports",
        "required_top_level_exports",
        "submodule_export_exclusions",
    )
    surface_changed = any(updated.get(key) != contract.get(key) for key in surface_keys)
    if baseline != contract["baseline"] or surface_changed:
        updated["baseline_commit"] = baseline_commit
    return updated


def _validate_parameter_contract(
    name: str,
    released: list[dict[str, object]],
    current: list[dict[str, object]],
) -> list[str]:
    errors: list[str] = []
    positional_kinds = {"POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD"}
    released_positional = [entry for entry in released if entry["kind"] in positional_kinds]
    current_positional = [entry for entry in current if entry["kind"] in positional_kinds]
    if current_positional[: len(released_positional)] != released_positional:
        errors.append(
            f"{name} changed its released positional parameter prefix: "
            f"expected {released_positional!r}, got {current_positional!r}"
        )
    elif any(entry["kind"] == "VAR_POSITIONAL" for entry in released) and len(
        current_positional
    ) != len(released_positional):
        added = current_positional[len(released_positional) :]
        errors.append(
            f"{name} added positional parameters before its released variadic parameter: {added!r}"
        )

    current_by_name = {entry["name"]: entry for entry in current}
    for entry in released:
        if entry["kind"] in positional_kinds:
            continue
        current_entry = current_by_name.get(entry["name"])
        if current_entry != entry:
            errors.append(
                f"{name}.{entry['name']} changed its released parameter contract: "
                f"expected {entry!r}, got {current_entry!r}"
            )
    released_names = {entry["name"] for entry in released}
    for entry in current:
        if entry["name"] in released_names:
            continue
        if entry["kind"] in {"VAR_POSITIONAL", "VAR_KEYWORD"}:
            continue
        default = entry["default"]
        if isinstance(default, dict) and default.get("kind") == "required":
            errors.append(f"{name}.{entry['name']} added a required parameter")
    return errors


def _import_contract_module(module_name: str, agents_module: Any | None) -> Any:
    if module_name == "agents" and agents_module is not None:
        return agents_module
    return importlib.import_module(module_name)


def _validate_public_property_contract(
    contract: dict[str, Any],
    agents_module: Any | None,
) -> list[str]:
    errors: list[str] = []
    for entry in contract.get("public_properties", []):
        module_name = entry["module"]
        class_name = entry["class_name"]
        try:
            module = _import_contract_module(module_name, agents_module)
        except Exception as error:
            errors.append(f"Failed to import released module {module_name}: {error!r}")
            continue
        class_value = getattr(module, class_name, None)
        if not isinstance(class_value, type):
            errors.append(f"Missing released public class {module_name}.{class_name}")
            continue
        for property_name in entry["names"]:
            descriptor = inspect.getattr_static(class_value, property_name, None)
            if not isinstance(descriptor, property):
                errors.append(
                    f"{module_name}.{class_name}.{property_name} "
                    "removed or changed a released public property"
                )
    return errors


def _submodule_export_contract(
    module: object,
    *,
    optional_bindings: Mapping[str, str] | None = None,
    optional_exports: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    exports = getattr(module, "__all__", None)
    if exports is None:
        return None
    if not isinstance(exports, list | tuple) or not all(type(name) is str for name in exports):
        raise ValueError("public module __all__ must contain only strings")
    names = list(exports)
    if len(names) != len(set(names)):
        raise ValueError("public module __all__ must not contain duplicate exports")
    optional_binding_modules = _optional_dependency_modules(
        dict(optional_bindings or {}), field_name="optional_bindings"
    )
    optional_export_modules = _optional_dependency_modules(
        dict(optional_exports or {}), field_name="optional_exports"
    )
    optional_binding_names = set(optional_binding_modules)
    optional_export_names = set(optional_export_modules)
    unknown_optional_names = sorted((optional_binding_names | optional_export_names) - set(names))
    if unknown_optional_names:
        raise ValueError(
            f"optional submodule bindings are not exported: {unknown_optional_names!r}"
        )
    return {
        "names": names,
        "optional_bindings": {
            name: optional_binding_modules[name] for name in names if name in optional_binding_names
        },
        "optional_exports": {
            name: optional_export_modules[name] for name in names if name in optional_export_names
        },
    }


def _optional_dependency_modules(value: object, *, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(
            f"{field_name} must be an object mapping export names to dependency modules"
        )
    modules: dict[str, str] = {}
    for name, module_name in value.items():
        if type(name) is not str or not name:
            raise ValueError(f"{field_name} export names must be non-empty strings")
        if type(module_name) is not str or not module_name.strip():
            raise ValueError(f"{field_name} dependency for {name!r} must be a non-empty string")
        modules[name] = module_name
    return modules


def _optional_dependency_is_available(module_name: str) -> bool:
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    return find_spec(module_name) is not None


def _matches_platform_import_error(
    contract: dict[str, Any], module_name: str, error: Exception
) -> bool:
    allowed_error_types = {"ImportError": ImportError}
    for entry in contract.get("platform_import_errors", []):
        if entry["module"] != module_name or sys.platform not in entry["platforms"]:
            continue
        expected_error_type = allowed_error_types.get(entry["error_type"])
        return (
            expected_error_type is not None
            and type(error) is expected_error_type
            and entry["message_contains"] in str(error)
        )
    return False


def validate_released_api_contract(
    contract: dict[str, Any],
    *,
    agents_module: Any | None = None,
    require_all_optional_exports: bool = False,
) -> list[str]:
    agents = agents_module or importlib.import_module("agents")
    errors: list[str] = []

    errors.extend(_validate_public_property_contract(contract, agents_module))

    missing_exports = sorted(set(contract["required_top_level_exports"]) - set(agents.__all__))
    if missing_exports:
        errors.append(f"Missing released top-level exports: {missing_exports!r}")
    missing_bindings = sorted(
        name for name in contract["required_top_level_exports"] if not hasattr(agents, name)
    )
    if missing_bindings:
        errors.append(f"Missing released top-level bindings: {missing_bindings!r}")

    imported_modules: dict[str, object] = {"agents": agents}
    for module_name in contract["public_modules"]:
        try:
            imported_modules[module_name] = _import_contract_module(module_name, agents_module)
        except Exception as error:
            if _matches_platform_import_error(contract, module_name, error):
                continue
            errors.append(f"Failed to import released module {module_name}: {error!r}")

    for module_name, released in contract.get("required_submodule_exports", {}).items():
        module = imported_modules.get(module_name)
        if module is None:
            continue
        try:
            current = _submodule_export_contract(module)
        except ValueError as error:
            errors.append(f"Invalid released module exports for {module_name}: {error}")
            continue
        if current is None:
            errors.append(f"Released module {module_name} no longer defines __all__")
            continue
        try:
            optional_exports = _optional_dependency_modules(
                released.get("optional_exports", {}), field_name="optional_exports"
            )
            optional_bindings = _optional_dependency_modules(
                released.get("optional_bindings", {}), field_name="optional_bindings"
            )
        except ValueError as error:
            errors.append(
                f"Invalid released {module_name} optional dependency declarations: {error}"
            )
            continue
        unknown_optional_names = sorted(
            (set(optional_bindings) | set(optional_exports)) - set(released["names"])
        )
        if unknown_optional_names:
            errors.append(
                f"Invalid released {module_name} optional dependency declarations: "
                f"names are not exported: {unknown_optional_names!r}"
            )
            continue
        try:
            unavailable_optional_exports = {
                name
                for name, dependency_module in optional_exports.items()
                if not _optional_dependency_is_available(dependency_module)
            }
            unavailable_optional_bindings = {
                name
                for name, dependency_module in (optional_bindings | optional_exports).items()
                if not _optional_dependency_is_available(dependency_module)
            }
        except (AttributeError, ImportError, ValueError) as error:
            errors.append(
                f"Unable to inspect released {module_name} optional dependencies: {error!r}"
            )
            continue
        if require_all_optional_exports and unavailable_optional_exports:
            unavailable = sorted(
                f"{name} -> {optional_exports[name]}" for name in unavailable_optional_exports
            )
            errors.append(
                f"Required optional dependencies for released {module_name} "
                f"are unavailable: {unavailable!r}"
            )
            continue
        missing_names = sorted(
            set(released["names"]) - unavailable_optional_exports - set(current["names"])
        )
        if missing_names:
            errors.append(f"Missing released {module_name} exports: {missing_names!r}")
        missing_required_bindings = []
        for name in released["names"]:
            if name in unavailable_optional_bindings:
                continue
            try:
                getattr(module, name)
            except (AttributeError, ImportError):
                missing_required_bindings.append(name)
        if missing_required_bindings:
            errors.append(
                f"Missing released {module_name} bindings: {sorted(missing_required_bindings)!r}"
            )

    for entry in contract["canonical_imports"]:
        try:
            module = _import_contract_module(entry["module"], agents_module)
        except Exception as error:
            if _matches_platform_import_error(contract, entry["module"], error):
                continue
            errors.append(f"Failed to import released module {entry['module']}: {error!r}")
            continue
        try:
            canonical = _import_contract_module(entry["canonical_module"], agents_module)
        except Exception as error:
            if _matches_platform_import_error(contract, entry["canonical_module"], error):
                continue
            errors.append(
                f"Failed to import released module {entry['canonical_module']}: {error!r}"
            )
            continue
        missing = object()
        actual = getattr(module, entry["name"], missing)
        expected = getattr(canonical, entry["canonical_name"], missing)
        if actual is missing or expected is missing or actual is not expected:
            errors.append(
                f"{entry['module']}.{entry['name']} no longer resolves to "
                f"{entry['canonical_module']}.{entry['canonical_name']}"
            )

    for name, released in contract["callables"].items():
        if name.startswith("agents."):
            module_name, _, binding_name = name.rpartition(".")
            try:
                module = _import_contract_module(module_name, agents_module)
            except Exception as error:
                if _matches_platform_import_error(contract, module_name, error):
                    continue
                errors.append(f"Failed to import released module {module_name}: {error!r}")
                continue
            value = getattr(module, binding_name, None)
            if value is None:
                canonical_entry = next(
                    (
                        entry
                        for entry in contract["canonical_imports"]
                        if entry["module"] == module_name and entry["name"] == binding_name
                    ),
                    None,
                )
                if canonical_entry is not None:
                    try:
                        _import_contract_module(canonical_entry["canonical_module"], agents_module)
                    except Exception as error:
                        if _matches_platform_import_error(
                            contract, canonical_entry["canonical_module"], error
                        ):
                            continue
        else:
            module_name = "agents"
            binding_name = name
            value = getattr(agents, binding_name, None)
        if value is None:
            errors.append(f"Missing released callable {module_name}.{binding_name}")
            continue
        current_kind = _callable_kind(value)
        if current_kind != released["kind"]:
            errors.append(
                f"Released callable {module_name}.{binding_name} changed kind from "
                f"{released['kind']} to {current_kind or type(value).__name__}"
            )
            continue
        released_execution_kind = released.get("execution_kind")
        if released_execution_kind is not None:
            current_execution_kind = _function_execution_kind(value)
            if current_execution_kind != released_execution_kind:
                errors.append(
                    f"{name} changed execution from "
                    f"{released_execution_kind} to {current_execution_kind}"
                )
        current_parameters = _parameter_contract(value)
        errors.extend(
            _validate_parameter_contract(name, released["parameters"], current_parameters)
        )
        current_fields = _dataclass_field_contract(value)
        released_fields = released["dataclass_fields"]
        if current_fields[: len(released_fields)] != released_fields:
            errors.append(
                f"{name} changed its released dataclass field prefix: "
                f"expected {released_fields!r}, got {current_fields!r}"
            )
        for field in current_fields[len(released_fields) :]:
            default = field["default"]
            if field["init"] and isinstance(default, dict) and default.get("kind") == "required":
                errors.append(f"{name}.{field['name']} added a required dataclass field")
        for member_name, released_member in released.get("members", {}).items():
            descriptor = _sdk_public_class_descriptor(value, member_name)
            current_member = _class_member_contract(descriptor)
            if current_member is None:
                errors.append(f"{name}.{member_name} removed a released public method")
                continue
            if current_member["binding"] != released_member["binding"]:
                errors.append(
                    f"{name}.{member_name} changed binding from "
                    f"{released_member['binding']} to {current_member['binding']}"
                )
                continue
            released_execution_kind = released_member.get("execution_kind")
            if (
                released_execution_kind is not None
                and current_member["execution_kind"] != released_execution_kind
            ):
                errors.append(
                    f"{name}.{member_name} changed execution from "
                    f"{released_execution_kind} to {current_member['execution_kind']}"
                )
            errors.extend(
                _validate_parameter_contract(
                    f"{name}.{member_name}",
                    released_member["parameters"],
                    cast(list[dict[str, object]], current_member["parameters"]),
                )
            )
        released_enum_members = released.get("enum_members")
        if released_enum_members is not None:
            current_enum_members = _enum_member_contract(value)
            if current_enum_members is None:
                errors.append(f"{name} is no longer an enum")
                continue
            current_enum_members_by_name = {
                member["name"]: member["value"] for member in current_enum_members
            }
            for member in released_enum_members:
                member_name = member["name"]
                if member_name not in current_enum_members_by_name:
                    errors.append(f"{name}.{member_name} removed or renamed a released enum member")
                    continue
                current_value = current_enum_members_by_name[member_name]
                if current_value != member["value"]:
                    errors.append(
                        f"{name}.{member_name} changed its released enum value: "
                        f"expected {member['value']!r}, got {current_value!r}"
                    )

    return errors


def _normalized_durable_state(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(payload)
    normalized.pop("$schemaVersion", None)
    return normalized


def _normalize_legacy_mount_credentials(payload: dict[str, Any]) -> dict[str, Any]:
    from agents.sandbox._mount_security import REDACTED_MOUNT_AUTHORITY_KEY

    normalized = deepcopy(payload)
    sandbox = cast(dict[str, Any], normalized["sandbox"])
    session_states = [cast(dict[str, Any], sandbox["session_state"])]
    sessions_by_agent = cast(dict[str, dict[str, Any]], sandbox["sessions_by_agent"])
    session_states.extend(
        cast(dict[str, Any], entry["session_state"]) for entry in sessions_by_agent.values()
    )
    for session_state in session_states:
        manifest = cast(dict[str, Any], session_state["manifest"])
        entries = cast(dict[str, dict[str, Any]], manifest["entries"])
        mount = entries["remote"]
        mount["access_key_id"] = None
        mount["secret_access_key"] = None
        mount["session_token"] = None
        strategy = cast(dict[str, Any], mount["mount_strategy"])
        strategy["driver_options"] = {}
        session_state[REDACTED_MOUNT_AUTHORITY_KEY] = True
    return normalized


def _legacy_driver_option_errors(payload: dict[str, Any]) -> list[str]:
    sandbox = cast(dict[str, Any], payload["sandbox"])
    session_states = [("sandbox.session_state", cast(dict[str, Any], sandbox["session_state"]))]
    sessions_by_agent = cast(dict[str, dict[str, Any]], sandbox["sessions_by_agent"])
    session_states.extend(
        (
            f"sandbox.sessions_by_agent.{agent_id}.session_state",
            cast(dict[str, Any], entry["session_state"]),
        )
        for agent_id, entry in sessions_by_agent.items()
    )
    errors: list[str] = []
    for path, session_state in session_states:
        manifest = cast(dict[str, Any], session_state["manifest"])
        entries = cast(dict[str, dict[str, Any]], manifest["entries"])
        mount = entries["remote"]
        strategy = cast(dict[str, Any], mount["mount_strategy"])
        if strategy.get("driver_options") != {}:
            errors.append(f"{path}.manifest.entries.remote.mount_strategy.driver_options remained")
    return errors


def _find_subset_errors(expected: object, actual: object, path: str = "state") -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path} changed type from mapping to {type(actual).__name__}"]
        errors: list[str] = []
        for key, value in expected.items():
            if key not in actual:
                errors.append(f"{path}.{key} was dropped")
                continue
            errors.extend(_find_subset_errors(value, actual[key], f"{path}.{key}"))
        return errors
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path} changed type from list to {type(actual).__name__}"]
        if len(expected) != len(actual):
            return [f"{path} changed length from {len(expected)} to {len(actual)}"]
        errors = []
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=True)):
            errors.extend(_find_subset_errors(expected_item, actual_item, f"{path}[{index}]"))
        return errors
    if type(expected) is not type(actual):
        return [f"{path} changed type from {type(expected).__name__} to {type(actual).__name__}"]
    if expected != actual:
        return [f"{path} changed from {expected!r} to {actual!r}"]
    return []


def _restore_agent(payload: dict[str, Any]) -> Any:
    from agents import Agent, handoff

    current_agent = payload.get("current_agent")
    name = (
        current_agent.get("name", "compat-agent")
        if isinstance(current_agent, dict)
        else "compat-agent"
    )
    identity = current_agent.get("identity") if isinstance(current_agent, dict) else None
    if identity == f"{name}#2":
        duplicate = Agent(name=name)
        return Agent(name=name, handoffs=[handoff(duplicate)])
    return Agent(name=name)


async def validate_historical_run_state_fixture(path: Path) -> list[str]:
    from agents import RunState
    from agents.run_state import CURRENT_SCHEMA_VERSION

    errors: list[str] = []
    payload = json.loads(path.read_text(encoding="utf-8"))
    historical = deepcopy(payload)
    original_version = historical.get("$schemaVersion")
    agent = _restore_agent(historical)
    restored = await RunState.from_json(agent, payload)
    canonical = restored.to_json()

    if canonical.get("$schemaVersion") != CURRENT_SCHEMA_VERSION:
        errors.append(
            f"{path.name} rewrote as {canonical.get('$schemaVersion')!r}, "
            f"expected {CURRENT_SCHEMA_VERSION!r}"
        )
    semantic_errors = _find_subset_errors(
        _normalized_durable_state(historical),
        _normalized_durable_state(canonical),
    )
    errors.extend(f"{path.name}: {error}" for error in semantic_errors)

    expected_canonical = deepcopy(canonical)
    rerestored = await RunState.from_json(agent, deepcopy(canonical))
    recanonical = rerestored.to_json()
    if recanonical != expected_canonical:
        errors.append(
            f"{path.name} was not idempotent after rewriting schema {original_version!r} "
            f"to {CURRENT_SCHEMA_VERSION!r}"
        )
    return errors


async def validate_historical_resume_behavior(
    path: Path,
    *,
    feature: str,
    decision: str | None = None,
) -> list[str]:
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from agents import Agent, Runner, RunState, function_tool
    from agents.items import ToolCallOutputItem, TResponseOutputItem
    from integration_tests._fake_model import QueuedFakeModel

    invocation_count = 0
    if feature == "canonical_invocation_identity":

        def lookup_account(account_id: str) -> str:
            nonlocal invocation_count
            invocation_count += 1
            return f"approved:{account_id}"

        tool = function_tool(lookup_account, needs_approval=True)
        model_turns: list[list[TResponseOutputItem]] = [
            [
                ResponseFunctionToolCall(
                    type="function_call",
                    name="lookup_account",
                    call_id="function-request-1",
                    status="completed",
                    arguments='{"account_id":"account-1"}',
                )
            ]
        ]
        expected_invocations = 1
        expected_tool_output = "approved:account-1"
    elif feature == "pending_tool_approval":

        def historical_approval(account_id: str) -> str:
            nonlocal invocation_count
            invocation_count += 1
            return f"approved:{account_id}"

        tool = function_tool(historical_approval, needs_approval=True)
        model_turns = []
        if decision == "approve":
            expected_invocations = 1
            expected_tool_output = "approved:account-1"
        elif decision == "reject":
            expected_invocations = 0
            expected_tool_output = "Candidate rejected historical approval"
        else:
            raise ValueError("pending_tool_approval requires an approve or reject decision")
    else:
        raise ValueError(f"Unsupported historical resume feature: {feature}")

    final_message = ResponseOutputMessage(
        id="historical-resume-final",
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                type="output_text",
                text="resume complete",
                annotations=[],
                logprobs=[],
            )
        ],
    )
    model_turns.append([final_message])
    model = QueuedFakeModel(model_turns)
    agent = Agent(name="compat-agent", model=model, tools=[tool])
    payload = json.loads(path.read_text(encoding="utf-8"))
    restored = await RunState.from_json(agent, payload)
    if feature == "pending_tool_approval":
        interruptions = restored.get_interruptions()
        if len(interruptions) != 1:
            return [f"{path.name} did not restore its historical pending approval"]
        if decision == "approve":
            restored.approve(interruptions[0])
        else:
            restored.reject(
                interruptions[0],
                rejection_message="Candidate rejected historical approval",
            )
    result = await Runner.run(agent, restored)

    errors: list[str] = []
    if result.interruptions:
        errors.append(f"{path.name} interrupted instead of applying its historical decision")
    if invocation_count != expected_invocations:
        errors.append(
            f"{path.name} invoked its approval-controlled tool {invocation_count} times, "
            f"expected {expected_invocations}"
        )
    tool_outputs = [
        item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)
    ]
    if expected_tool_output not in tool_outputs:
        errors.append(
            f"{path.name} did not preserve the historical tool decision output "
            f"{expected_tool_output!r}"
        )
    if result.final_output != "resume complete":
        errors.append(f"{path.name} did not complete its resumed run")
    return errors


async def validate_legacy_credential_run_state_fixture(
    path: Path,
    *,
    sentinels: Iterable[str],
) -> list[str]:
    from agents import RunState
    from agents.run_state import CURRENT_SCHEMA_VERSION

    errors: list[str] = []
    payload = json.loads(path.read_text(encoding="utf-8"))
    historical = deepcopy(payload)
    agent = _restore_agent(payload)
    restored = await RunState.from_json(agent, payload)
    canonical = restored.to_json()

    if canonical.get("$schemaVersion") != CURRENT_SCHEMA_VERSION:
        errors.append(
            f"{path.name} rewrote as {canonical.get('$schemaVersion')!r}, "
            f"expected {CURRENT_SCHEMA_VERSION!r}"
        )
    semantic_errors = _find_subset_errors(
        _normalized_durable_state(_normalize_legacy_mount_credentials(historical)),
        _normalized_durable_state(canonical),
    )
    errors.extend(f"{path.name}: {error}" for error in semantic_errors)
    if not semantic_errors:
        errors.extend(f"{path.name}: {error}" for error in _legacy_driver_option_errors(canonical))

    serialized_observables = json.dumps(canonical, sort_keys=True) + repr(restored._sandbox)
    for sentinel in sentinels:
        if sentinel in serialized_observables:
            errors.append(f"{path.name} retained credential sentinel {sentinel!r}")

    expected_canonical = deepcopy(canonical)
    rerestored = await RunState.from_json(agent, deepcopy(canonical))
    if rerestored.to_json() != expected_canonical:
        errors.append(f"{path.name} was not idempotent after credential sanitization")
    return errors
