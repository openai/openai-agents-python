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

from pydantic import BaseModel


@dataclasses.dataclass(frozen=True)
class OptionalDependencyInstallation:
    dependency_module: str
    extra: str | None = None
    requirement: str | None = None
    unsupported_platforms: tuple[str, ...] = ()

    def is_supported_on_current_platform(self) -> bool:
        return sys.platform not in self.unsupported_platforms


@dataclasses.dataclass(frozen=True)
class SubmoduleExportPolicy:
    modules: dict[str, dict[str, dict[str, str]]]
    dependency_installations: tuple[OptionalDependencyInstallation, ...]
    canonical_imports: tuple[dict[str, str], ...] = ()
    public_properties: tuple[dict[str, Any], ...] = ()


def load_api_contract(path: Path) -> dict[str, Any]:
    contract = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    _add_legacy_literal_types(contract)
    return contract


def load_submodule_export_policy(path: Path) -> SubmoduleExportPolicy:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("submodule export policy must be an object")
    unknown_top_level_fields = sorted(
        set(value) - {"canonical_imports", "modules", "optional_dependencies", "public_properties"}
    )
    if unknown_top_level_fields:
        raise ValueError(
            f"submodule export policy has unknown fields: {unknown_top_level_fields!r}"
        )
    modules = value.get("modules")
    if not isinstance(modules, dict):
        raise ValueError("submodule export policy modules must be an object keyed by module name")
    policy: dict[str, dict[str, dict[str, str]]] = {}
    for module_name, declarations in modules.items():
        if type(module_name) is not str or not module_name:
            raise ValueError("submodule export policy module names must be non-empty strings")
        if not isinstance(declarations, dict):
            raise ValueError(f"submodule export policy for {module_name} must be an object")
        unknown_fields = sorted(set(declarations) - {"optional_bindings", "optional_exports"})
        if unknown_fields:
            raise ValueError(
                f"submodule export policy for {module_name} has unknown fields: {unknown_fields!r}"
            )
        policy[module_name] = {
            "optional_bindings": _optional_dependency_modules(
                declarations.get("optional_bindings", {}), field_name="optional_bindings"
            ),
            "optional_exports": _optional_dependency_modules(
                declarations.get("optional_exports", {}), field_name="optional_exports"
            ),
        }

    dependencies = value.get("optional_dependencies")
    if not isinstance(dependencies, dict):
        raise ValueError("submodule export policy optional_dependencies must be an object")
    dependency_installations: list[OptionalDependencyInstallation] = []
    for module_name, installation in dependencies.items():
        if type(module_name) is not str or not module_name:
            raise ValueError("optional dependency module names must be non-empty strings")
        if not isinstance(installation, dict):
            raise ValueError(
                f"optional dependency installation for {module_name} must be an object"
            )
        unknown_fields = sorted(
            set(installation) - {"extra", "requirement", "unsupported_platforms"}
        )
        if unknown_fields:
            raise ValueError(
                f"optional dependency installation for {module_name} has unknown fields: "
                f"{unknown_fields!r}"
            )
        configured = [field for field in ("extra", "requirement") if field in installation]
        if len(configured) != 1:
            raise ValueError(
                f"optional dependency installation for {module_name} must declare exactly one "
                "of extra or requirement"
            )
        field_name = configured[0]
        install_value = installation[field_name]
        if type(install_value) is not str or not install_value:
            raise ValueError(
                f"optional dependency installation {field_name} for {module_name} must be a "
                "non-empty string"
            )
        unsupported_platforms = installation.get("unsupported_platforms", [])
        if (
            not isinstance(unsupported_platforms, list)
            or not all(type(platform) is str and platform for platform in unsupported_platforms)
            or len(unsupported_platforms) != len(set(unsupported_platforms))
        ):
            raise ValueError(
                f"optional dependency installation unsupported_platforms for {module_name} "
                "must be a list of unique non-empty strings"
            )
        dependency_installations.append(
            OptionalDependencyInstallation(
                dependency_module=module_name,
                extra=install_value if field_name == "extra" else None,
                requirement=install_value if field_name == "requirement" else None,
                unsupported_platforms=tuple(unsupported_platforms),
            )
        )

    referenced_dependencies = {
        dependency
        for module_policy in policy.values()
        for declarations in module_policy.values()
        for dependency in declarations.values()
    }
    missing_installations = sorted(referenced_dependencies - set(dependencies))
    unused_installations = sorted(set(dependencies) - referenced_dependencies)
    if missing_installations:
        raise ValueError(
            "submodule export policy dependencies are missing installation declarations: "
            f"{missing_installations!r}"
        )
    if unused_installations:
        raise ValueError(
            "submodule export policy has unused dependency installation declarations: "
            f"{unused_installations!r}"
        )
    return SubmoduleExportPolicy(
        modules=policy,
        dependency_installations=tuple(
            sorted(
                dependency_installations, key=lambda installation: installation.dependency_module
            )
        ),
        canonical_imports=_canonical_import_policy(value.get("canonical_imports", [])),
        public_properties=_public_property_policy(value.get("public_properties", [])),
    )


def _canonical_import_policy(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError("submodule export policy canonical_imports must be a list")
    required_fields = {"canonical_module", "canonical_name", "module", "name"}
    entries: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != required_fields:
            raise ValueError(
                "submodule export policy canonical_imports entries must contain exactly "
                "canonical_module, canonical_name, module, and name"
            )
        if not all(type(entry[field]) is str and entry[field] for field in required_fields):
            raise ValueError(
                "submodule export policy canonical_imports values must be non-empty strings"
            )
        identity = (entry["module"], entry["name"])
        if identity in identities:
            raise ValueError(
                "submodule export policy canonical_imports must not repeat "
                f"{entry['module']}.{entry['name']}"
            )
        identities.add(identity)
        entries.append({field: entry[field] for field in sorted(required_fields)})
    return tuple(entries)


def _public_property_policy(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("submodule export policy public_properties must be a list")
    required_fields = {"class_name", "module", "names"}
    entries: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != required_fields:
            raise ValueError(
                "submodule export policy public_properties entries must contain exactly "
                "class_name, module, and names"
            )
        module_name = entry["module"]
        class_name = entry["class_name"]
        names = entry["names"]
        if type(module_name) is not str or not module_name:
            raise ValueError(
                "submodule export policy public_properties module must be a non-empty string"
            )
        if type(class_name) is not str or not class_name:
            raise ValueError(
                "submodule export policy public_properties class_name must be a non-empty string"
            )
        if (
            not isinstance(names, list)
            or not names
            or not all(type(name) is str and name for name in names)
            or len(names) != len(set(names))
        ):
            raise ValueError(
                "submodule export policy public_properties names must be a non-empty list of "
                "unique non-empty strings"
            )
        identity = (module_name, class_name)
        if identity in identities:
            raise ValueError(
                "submodule export policy public_properties must not repeat "
                f"{module_name}.{class_name}"
            )
        identities.add(identity)
        entries.append({"class_name": class_name, "module": module_name, "names": list(names)})
    return tuple(entries)


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
    seen: dict[int, object] = {}

    def visit_exception_state(value: object) -> None:
        value_id = id(value)
        if value_id in seen:
            return
        # Keep visited objects alive so a later temporary object cannot reuse an id and be
        # mistaken for a cycle. Traceback frame locals are materialized as temporary dicts.
        seen[value_id] = value

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


def _pydantic_model_field_contract(value: object) -> list[dict[str, object]] | None:
    if not (isinstance(value, type) and issubclass(value, BaseModel)):
        return None
    result: list[dict[str, object]] = []
    for name, field in value.model_fields.items():
        if name.startswith("_"):
            continue
        if field.is_required():
            default_contract: dict[str, object] = {"kind": "required"}
        elif field.default_factory is not None:
            factory = field.default_factory
            default_contract = {
                "kind": "factory",
                "factory": f"{factory.__module__}.{factory.__qualname__}",
            }
        else:
            default_contract = _default_contract(field.default)
        result.append({"name": name, "default": default_contract})
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
    model_fields = _pydantic_model_field_contract(value)
    if model_fields is not None:
        contract["model_fields"] = model_fields
    enum_members = _enum_member_contract(value)
    if enum_members is not None:
        contract["enum_members"] = enum_members
    if kind == "class":
        contract["members"] = _public_class_member_contract(value)
    return contract


def _merge_canonical_imports(
    existing: Iterable[Mapping[str, str]], promoted: Iterable[Mapping[str, str]]
) -> list[dict[str, str]]:
    result = [dict(entry) for entry in existing]
    by_identity = {(entry["module"], entry["name"]): entry for entry in result}
    for entry_value in promoted:
        entry = dict(entry_value)
        identity = (entry["module"], entry["name"])
        previous = by_identity.get(identity)
        if previous is not None:
            if previous != entry:
                raise ValueError(
                    "release policy canonical import conflicts with the released contract for "
                    f"{entry['module']}.{entry['name']}"
                )
            continue
        result.append(entry)
        by_identity[identity] = entry
    return result


def _merge_public_properties(
    existing: Iterable[Mapping[str, Any]], promoted: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    result = [deepcopy(dict(entry)) for entry in existing]
    by_identity = {(entry["module"], entry["class_name"]): entry for entry in result}
    for entry_value in promoted:
        entry = deepcopy(dict(entry_value))
        identity = (entry["module"], entry["class_name"])
        previous = by_identity.get(identity)
        if previous is None:
            result.append(entry)
            by_identity[identity] = entry
            continue
        previous_names = previous["names"]
        for name in entry["names"]:
            if name not in previous_names:
                previous_names.append(name)
    return result


def _optional_dependency_unsupported_platforms(
    contract: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    value = contract.get("optional_dependency_unsupported_platforms", {})
    if not isinstance(value, dict):
        raise ValueError("optional_dependency_unsupported_platforms must be an object")
    result: dict[str, tuple[str, ...]] = {}
    for dependency_module, platforms in value.items():
        if type(dependency_module) is not str or not dependency_module:
            raise ValueError(
                "optional_dependency_unsupported_platforms keys must be non-empty strings"
            )
        if (
            not isinstance(platforms, list)
            or not all(type(platform) is str and platform for platform in platforms)
            or len(platforms) != len(set(platforms))
        ):
            raise ValueError(
                "optional_dependency_unsupported_platforms values must be lists of unique "
                "non-empty strings"
            )
        result[dependency_module] = tuple(platforms)
    return result


def _optional_dependency_is_available_for_contract(
    dependency_module: str,
    unsupported_platforms: Mapping[str, tuple[str, ...]],
) -> bool:
    return not _optional_dependency_is_unsupported_for_contract(
        dependency_module, unsupported_platforms
    ) and _optional_dependency_is_available(dependency_module)


def _optional_dependency_is_unsupported_for_contract(
    dependency_module: str,
    unsupported_platforms: Mapping[str, tuple[str, ...]],
) -> bool:
    return sys.platform in unsupported_platforms.get(dependency_module, ())


def _optional_dependency_for_binding(
    contract: Mapping[str, Any], module_name: str, binding_name: str
) -> str | None:
    return _optional_dependency_for_binding_in_modules(
        contract.get("required_submodule_exports", {}), module_name, binding_name
    )


def _optional_dependency_for_binding_in_modules(
    modules: Mapping[str, Any], module_name: str, binding_name: str
) -> str | None:
    module_contract = modules.get(module_name, {})
    for field_name in ("optional_bindings", "optional_exports"):
        dependency_module = module_contract.get(field_name, {}).get(binding_name)
        if dependency_module is not None:
            return cast(str, dependency_module)
    return None


def _preserve_released_callable_for_promotion(
    contract: Mapping[str, Any],
    callables: dict[str, Any],
    qualified_name: str,
    *,
    fail_if_missing: bool,
    unavailable_reason: str,
) -> None:
    released_callable = contract["callables"].get(qualified_name)
    if released_callable is None:
        if not fail_if_missing:
            return
        raise ValueError(
            f"Cannot promote new canonical callable {qualified_name} because "
            f"{unavailable_reason}. Ensure the binding is available and exposes an inspectable "
            "signature on the release preparation host."
        )
    callables[qualified_name] = deepcopy(released_callable)


def build_released_api_contract(
    contract: dict[str, Any],
    *,
    baseline: str,
    baseline_commit: str,
    agents_module: Any | None = None,
    release_policy: SubmoduleExportPolicy | None = None,
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

    canonical_imports = _merge_canonical_imports(
        contract["canonical_imports"],
        release_policy.canonical_imports if release_policy is not None else (),
    )
    policy_unsupported_platforms = (
        {
            installation.dependency_module: installation.unsupported_platforms
            for installation in release_policy.dependency_installations
            if installation.unsupported_platforms
        }
        if release_policy is not None
        else {}
    )
    top_level_callable_ids = {
        id(getattr(agents, name)) for name in callables if not name.startswith("agents.")
    }
    for entry in canonical_imports:
        module_name = entry["module"]
        if module_name == "agents":
            continue
        qualified_name = f"{module_name}.{entry['name']}"
        is_new_canonical_import = entry not in contract["canonical_imports"]
        optional_dependency = (
            _optional_dependency_for_binding_in_modules(
                release_policy.modules, module_name, entry["name"]
            )
            if release_policy is not None
            else None
        )
        if optional_dependency is not None and not _optional_dependency_is_available_for_contract(
            optional_dependency, policy_unsupported_platforms
        ):
            if _optional_dependency_is_unsupported_for_contract(
                optional_dependency, policy_unsupported_platforms
            ):
                _preserve_released_callable_for_promotion(
                    contract,
                    callables,
                    qualified_name,
                    fail_if_missing=is_new_canonical_import,
                    unavailable_reason=(
                        f"optional dependency {optional_dependency!r} is unsupported on "
                        f"{sys.platform!r}"
                    ),
                )
            continue
        try:
            module = _import_contract_module(module_name, agents_module)
        except Exception as error:
            if _matches_platform_import_error(contract, module_name, error):
                _preserve_released_callable_for_promotion(
                    contract,
                    callables,
                    qualified_name,
                    fail_if_missing=is_new_canonical_import,
                    unavailable_reason=(
                        f"module {module_name!r} has a declared import error on {sys.platform!r}"
                    ),
                )
                continue
            raise
        value = getattr(module, entry["name"], None)
        if value is None:
            try:
                _import_contract_module(entry["canonical_module"], agents_module)
            except Exception as error:
                if _matches_platform_import_error(contract, entry["canonical_module"], error):
                    _preserve_released_callable_for_promotion(
                        contract,
                        callables,
                        qualified_name,
                        fail_if_missing=is_new_canonical_import,
                        unavailable_reason=(
                            f"canonical module {entry['canonical_module']!r} has a declared "
                            f"import error on {sys.platform!r}"
                        ),
                    )
                    continue
                raise
            continue
        if id(value) in top_level_callable_ids:
            continue
        kind = _callable_kind(value)
        if kind is None:
            continue
        try:
            _signature(value)
        except (TypeError, ValueError) as error:
            _preserve_released_callable_for_promotion(
                contract,
                callables,
                qualified_name,
                fail_if_missing=is_new_canonical_import,
                unavailable_reason=f"its signature cannot be inspected: {error!r}",
            )
            continue
        callables[qualified_name] = _callable_contract(value)

    updated = deepcopy(contract)
    updated["baseline"] = baseline
    updated["required_top_level_exports"] = ordered_exports
    updated["callables"] = callables
    updated["canonical_imports"] = canonical_imports
    updated["public_properties"] = _merge_public_properties(
        contract.get("public_properties", []),
        release_policy.public_properties if release_policy is not None else (),
    )
    if release_policy is not None:
        updated["optional_dependency_unsupported_platforms"] = {
            dependency_module: list(platforms)
            for dependency_module, platforms in policy_unsupported_platforms.items()
        }
    excluded_submodule_exports = set(contract.get("submodule_export_exclusions", []))
    public_modules = list(contract["public_modules"])
    submodule_export_policy = release_policy.modules if release_policy is not None else None
    if submodule_export_policy is not None:
        invalid_policy_modules = sorted(
            module_name
            for module_name in submodule_export_policy
            if not module_name.startswith("agents.")
        )
        if invalid_policy_modules:
            raise ValueError(
                "new submodule export policy modules must be under the agents package: "
                f"{invalid_policy_modules!r}"
            )
        released_public_modules = set(public_modules)
        public_modules.extend(sorted(set(submodule_export_policy) - released_public_modules))
        unavailable_policy_dependencies = sorted(
            {
                dependency_module
                for module_policy in submodule_export_policy.values()
                for field_name in ("optional_bindings", "optional_exports")
                for dependency_module in _optional_dependency_modules(
                    dict(module_policy.get(field_name, {})), field_name=field_name
                ).values()
                if not _optional_dependency_is_unsupported_for_contract(
                    dependency_module, policy_unsupported_platforms
                )
                and not _optional_dependency_is_available(dependency_module)
            }
        )
        if unavailable_policy_dependencies:
            raise ValueError(
                "submodule export policy dependency modules are unavailable: "
                f"{unavailable_policy_dependencies!r}. Run `make sync` to install all "
                "optional dependencies, or correct the dependency module names."
            )
    updated["public_modules"] = public_modules
    required_submodule_exports: dict[str, dict[str, Any]] = {}
    for module_name in public_modules:
        if module_name == "agents" or module_name in excluded_submodule_exports:
            continue
        try:
            module = _import_contract_module(module_name, agents_module)
        except Exception as error:
            if _matches_platform_import_error(contract, module_name, error):
                continue
            if submodule_export_policy is not None and module_name in submodule_export_policy:
                raise ValueError(
                    f"Cannot import submodule export policy module {module_name}: {error!r}"
                ) from None
            raise
        if submodule_export_policy is None:
            module_policy = contract.get("required_submodule_exports", {}).get(module_name, {})
        else:
            module_policy = submodule_export_policy.get(module_name, {})
        allowed_missing_optional_exports = {
            name
            for name, dependency_module in _optional_dependency_modules(
                dict(module_policy.get("optional_exports", {})),
                field_name="optional_exports",
            ).items()
            if _optional_dependency_is_unsupported_for_contract(
                dependency_module, policy_unsupported_platforms
            )
        }
        module_contract = _submodule_export_contract(
            module,
            optional_bindings=module_policy.get("optional_bindings", {}),
            optional_exports=module_policy.get("optional_exports", {}),
            allowed_missing_optional_exports=allowed_missing_optional_exports,
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
        "optional_dependency_unsupported_platforms",
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


def _validate_pydantic_model_field_contract(
    name: str,
    released: list[dict[str, object]],
    current: list[dict[str, object]] | None,
) -> list[str]:
    errors: list[str] = []
    current_by_name = {cast(str, entry["name"]): entry for entry in current or []}
    for entry in released:
        current_entry = current_by_name.get(cast(str, entry["name"]))
        if current_entry != entry:
            errors.append(
                f"{name}.{entry['name']} changed its released Pydantic model field contract: "
                f"expected {entry!r}, got {current_entry!r}"
            )
    released_names = {entry["name"] for entry in released}
    for entry in current or []:
        if entry["name"] in released_names:
            continue
        default = entry["default"]
        if isinstance(default, dict) and default.get("kind") == "required":
            errors.append(f"{name}.{entry['name']} added a required Pydantic model field")
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
    allowed_missing_optional_exports: Iterable[str] = (),
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
    allowed_missing_names = set(allowed_missing_optional_exports)
    unknown_optional_names = sorted(
        (optional_binding_names | optional_export_names) - set(names) - allowed_missing_names
    )
    if unknown_optional_names:
        raise ValueError(
            f"optional submodule bindings are not exported: {unknown_optional_names!r}"
        )
    names.extend(
        name
        for name in optional_export_modules
        if name in allowed_missing_names and name not in names
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
) -> list[str]:
    agents = agents_module or importlib.import_module("agents")
    errors: list[str] = []

    try:
        unsupported_platforms = _optional_dependency_unsupported_platforms(contract)
    except ValueError as error:
        errors.append(f"Invalid released optional dependency platform declarations: {error}")
        unsupported_platforms = {}

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
            unsupported_optional_exports = {
                name
                for name, dependency_module in optional_exports.items()
                if _optional_dependency_is_unsupported_for_contract(
                    dependency_module, unsupported_platforms
                )
            }
            unsupported_optional_bindings = {
                name
                for name, dependency_module in (optional_bindings | optional_exports).items()
                if _optional_dependency_is_unsupported_for_contract(
                    dependency_module, unsupported_platforms
                )
            }
            unavailable_optional_exports = {
                name
                for name, dependency_module in optional_exports.items()
                if not _optional_dependency_is_available_for_contract(
                    dependency_module, unsupported_platforms
                )
            }
            unavailable_optional_bindings = {
                name
                for name, dependency_module in (optional_bindings | optional_exports).items()
                if not _optional_dependency_is_available_for_contract(
                    dependency_module, unsupported_platforms
                )
            }
        except (AttributeError, ImportError, ValueError) as error:
            errors.append(
                f"Unable to inspect released {module_name} optional dependencies: {error!r}"
            )
            continue
        current_names = set(current["names"])
        for name in sorted(unavailable_optional_exports & current_names):
            try:
                getattr(module, name)
            except (AttributeError, ImportError):
                if name in unsupported_optional_exports:
                    errors.append(
                        f"Invalid released {module_name} optional dependency declaration: "
                        f"{name!r} remains in __all__ on an unsupported platform but its "
                        "binding is unavailable"
                    )
                else:
                    errors.append(
                        f"Invalid released {module_name} optional dependency declaration: "
                        f"{name!r} remains in __all__ but its binding is unavailable; "
                        "declare it in optional_bindings instead of optional_exports"
                    )
            else:
                if name not in unsupported_optional_exports:
                    errors.append(
                        f"Invalid released {module_name} optional dependency declaration: "
                        f"{name!r} remains in __all__ and its binding resolves; remove its "
                        "optional declaration or correct its dependency module"
                    )
        binding_only_names = set(optional_bindings) - set(optional_exports)
        for name in sorted(unavailable_optional_bindings & binding_only_names):
            if name not in current_names:
                errors.append(
                    f"Invalid released {module_name} optional dependency declaration: "
                    f"{name!r} is absent from __all__; declare it in optional_exports "
                    "instead of optional_bindings"
                )
                continue
            try:
                getattr(module, name)
            except (AttributeError, ImportError):
                if name in unsupported_optional_bindings:
                    errors.append(
                        f"Invalid released {module_name} optional dependency declaration: "
                        f"{name!r} remains in __all__ on an unsupported platform but its "
                        "binding is unavailable"
                    )
            else:
                if name not in unsupported_optional_bindings:
                    errors.append(
                        f"Invalid released {module_name} optional dependency declaration: "
                        f"{name!r} remains in __all__ and its binding resolves; remove its "
                        "optional declaration or correct its dependency module"
                    )
        missing_names = sorted(
            set(released["names"]) - unavailable_optional_exports - current_names
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
        optional_dependency = _optional_dependency_for_binding(
            contract, entry["module"], entry["name"]
        )
        if optional_dependency is not None and not _optional_dependency_is_available_for_contract(
            optional_dependency, unsupported_platforms
        ):
            continue
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
            optional_dependency = _optional_dependency_for_binding(
                contract, module_name, binding_name
            )
            if optional_dependency is not None and not (
                _optional_dependency_is_available_for_contract(
                    optional_dependency, unsupported_platforms
                )
            ):
                continue
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
        released_model_fields = released.get("model_fields")
        if released_model_fields is not None:
            errors.extend(
                _validate_pydantic_model_field_contract(
                    name,
                    cast(list[dict[str, object]], released_model_fields),
                    _pydantic_model_field_contract(value),
                )
            )
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
