"""Parse release policy and describe public API surfaces for generation and validation."""

from __future__ import annotations

import ast
import dataclasses
import enum
import importlib
import inspect
import json
import sys
import typing
from collections.abc import Callable, Iterable, Mapping
from importlib.util import find_spec
from pathlib import Path
from types import FunctionType, ModuleType, UnionType
from typing import (
    Any,
    ForwardRef,
    Literal,
    TypeAlias,
    Union,
    cast,
    get_args,
    get_origin,
)

import typing_extensions
from pydantic import BaseModel
from typing_extensions import NotRequired, Required


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
    public_class_contracts: tuple[dict[str, Any], ...] = ()
    public_properties: tuple[dict[str, Any], ...] = ()
    public_type_aliases: tuple[dict[str, str], ...] = ()
    public_typed_dicts: tuple[dict[str, Any], ...] = ()


def load_api_contract(path: Path) -> dict[str, Any]:
    contract = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    _add_legacy_literal_types(contract)
    return contract


def load_submodule_export_policy(path: Path) -> SubmoduleExportPolicy:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("submodule export policy must be an object")
    unknown_top_level_fields = sorted(
        set(value)
        - {
            "canonical_imports",
            "modules",
            "optional_dependencies",
            "public_class_contracts",
            "public_properties",
            "public_type_aliases",
            "public_typed_dicts",
        }
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
        public_class_contracts=_public_class_contract_policy(
            value.get("public_class_contracts", [])
        ),
        public_properties=_public_property_policy(value.get("public_properties", [])),
        public_type_aliases=_public_type_alias_policy(value.get("public_type_aliases", [])),
        public_typed_dicts=_public_typed_dict_policy(value.get("public_typed_dicts", [])),
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
    entries: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("submodule export policy public_properties entries must be objects")
        owner_fields = {"class_name", "factory_name"} & set(entry)
        required_fields = {"module", "names", *owner_fields}
        if len(owner_fields) != 1 or set(entry) != required_fields:
            raise ValueError(
                "submodule export policy public_properties entries must contain exactly "
                "module, names, and one of class_name or factory_name"
            )
        owner_field = next(iter(owner_fields))
        module_name = entry["module"]
        owner_name = entry[owner_field]
        names = entry["names"]
        if type(module_name) is not str or not module_name:
            raise ValueError(
                "submodule export policy public_properties module must be a non-empty string"
            )
        if type(owner_name) is not str or not owner_name:
            raise ValueError(
                f"submodule export policy public_properties {owner_field} must be a non-empty "
                "string"
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
        identity = (owner_field, module_name, owner_name)
        if identity in identities:
            raise ValueError(
                "submodule export policy public_properties must not repeat "
                f"{module_name}.{owner_name}"
            )
        identities.add(identity)
        normalized_entry = {
            owner_field: owner_name,
            "module": module_name,
            "names": list(names),
        }
        entries.append(normalized_entry)
    return tuple(entries)


def _public_class_contract_policy(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("submodule export policy public_class_contracts must be a list")
    required_fields = {"class_name", "module"}
    contract_fields = {"abstract", "abstract_members"}
    entries: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for entry in value:
        if (
            not isinstance(entry, dict)
            or not required_fields.issubset(entry)
            or not set(entry).issubset(required_fields | contract_fields)
            or not (set(entry) & contract_fields)
        ):
            raise ValueError(
                "submodule export policy public_class_contracts entries must contain exactly "
                "module, class_name, and at least one of abstract or abstract_members"
            )
        module_name = entry["module"]
        class_name = entry["class_name"]
        if type(module_name) is not str or not module_name:
            raise ValueError(
                "submodule export policy public_class_contracts module must be a non-empty string"
            )
        if type(class_name) is not str or not class_name:
            raise ValueError(
                "submodule export policy public_class_contracts class_name must be a non-empty "
                "string"
            )
        if "abstract" in entry and type(entry["abstract"]) is not bool:
            raise ValueError(
                "submodule export policy public_class_contracts abstract must be a boolean"
            )
        abstract_members = entry.get("abstract_members")
        if "abstract_members" in entry and (
            not isinstance(abstract_members, list)
            or not abstract_members
            or not all(type(name) is str and name for name in abstract_members)
            or len(abstract_members) != len(set(abstract_members))
        ):
            raise ValueError(
                "submodule export policy public_class_contracts abstract_members must be a "
                "non-empty list of unique non-empty strings"
            )
        identity = (module_name, class_name)
        if identity in identities:
            raise ValueError(
                "submodule export policy public_class_contracts must not repeat "
                f"{module_name}.{class_name}"
            )
        identities.add(identity)
        normalized_entry: dict[str, Any] = {
            "class_name": class_name,
            "module": module_name,
        }
        if "abstract" in entry:
            normalized_entry["abstract"] = entry["abstract"]
        if "abstract_members" in entry:
            normalized_entry["abstract_members"] = sorted(abstract_members)
        entries.append(normalized_entry)
    return tuple(entries)


def _public_typed_dict_policy(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("submodule export policy public_typed_dicts must be a list")
    required_fields = {"class_name", "module", "names"}
    entries: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != required_fields:
            raise ValueError(
                "submodule export policy public_typed_dicts entries must contain exactly "
                "class_name, module, and names"
            )
        module_name = entry["module"]
        class_name = entry["class_name"]
        names = entry["names"]
        if type(module_name) is not str or not module_name:
            raise ValueError(
                "submodule export policy public_typed_dicts module must be a non-empty string"
            )
        if type(class_name) is not str or not class_name:
            raise ValueError(
                "submodule export policy public_typed_dicts class_name must be a non-empty string"
            )
        if (
            not isinstance(names, list)
            or not names
            or not all(type(name) is str and name for name in names)
            or len(names) != len(set(names))
        ):
            raise ValueError(
                "submodule export policy public_typed_dicts names must be a non-empty list of "
                "unique non-empty strings"
            )
        identity = (module_name, class_name)
        if identity in identities:
            raise ValueError(
                "submodule export policy public_typed_dicts must not repeat "
                f"{module_name}.{class_name}"
            )
        identities.add(identity)
        entries.append({"class_name": class_name, "module": module_name, "names": list(names)})
    return tuple(entries)


def _public_type_alias_policy(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError("submodule export policy public_type_aliases must be a list")
    required_fields = {"module", "name"}
    entries: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != required_fields:
            raise ValueError(
                "submodule export policy public_type_aliases entries must contain exactly "
                "module and name"
            )
        if not all(type(entry[field]) is str and entry[field] for field in required_fields):
            raise ValueError(
                "submodule export policy public_type_aliases values must be non-empty strings"
            )
        identity = (entry["module"], entry["name"])
        if identity in identities:
            raise ValueError(
                "submodule export policy public_type_aliases must not repeat "
                f"{entry['module']}.{entry['name']}"
            )
        identities.add(identity)
        entries.append({"module": entry["module"], "name": entry["name"]})
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
    voice_testing = sys.modules.get("agents.voice.testing")
    if voice_testing is not None and value is getattr(voice_testing, "_START_NOT_CONFIGURED", None):
        return {
            "kind": "sentinel",
            "identity": "agents.voice.testing._START_NOT_CONFIGURED",
        }
    value_type = f"{type(value).__module__}.{type(value).__qualname__}"
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
    if value_type == "pydantic.fields.FieldInfo":
        return {"kind": "repr", "type": value_type, "value": repr(value)}
    if isinstance(value, enum.Enum):
        return {
            "kind": "enum",
            "type": value_type,
            "name": value.name,
            "value": _default_contract(value.value),
        }
    if isinstance(value, type):
        return {
            "kind": "type",
            "identity": f"{value.__module__}.{value.__qualname__}",
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


def _is_sdk_owned_callable(value: object) -> bool:
    module_name = getattr(value, "__module__", None)
    return (
        _callable_kind(cast(Callable[..., Any], value)) is not None
        and isinstance(module_name, str)
        and (module_name == "agents" or module_name.startswith("agents."))
    )


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


def _public_property_identity(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    if "class_name" in entry:
        return ("class_name", cast(str, entry["module"]), cast(str, entry["class_name"]))
    return ("factory_name", cast(str, entry["module"]), cast(str, entry["factory_name"]))


def _annotation_contract(annotation: object) -> str:
    if isinstance(annotation, ForwardRef):
        annotation_text = annotation.__forward_arg__
    elif isinstance(annotation, str):
        annotation_text = annotation
    else:
        annotation_text = inspect.formatannotation(annotation)
    for wrapper_name in ("Required", "NotRequired"):
        for module_name in ("typing", "typing_extensions"):
            qualified_prefix = f"{module_name}.{wrapper_name}["
            if annotation_text.startswith(qualified_prefix):
                return f"{wrapper_name}[{annotation_text.removeprefix(qualified_prefix)}"
    return annotation_text


def _sorted_type_alias_members(members: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        members,
        key=lambda member: (
            cast(str, member["kind"]),
            json.dumps(member, sort_keys=True, separators=(",", ":")),
        ),
    )


def _is_type_alias_type(value: object) -> bool:
    native_type_alias_type = getattr(typing, "TypeAliasType", typing_extensions.TypeAliasType)
    return isinstance(value, typing_extensions.TypeAliasType | native_type_alias_type)


def _is_type_alias_annotation(annotation: object, module: object) -> bool:
    if annotation is TypeAlias or annotation is typing_extensions.TypeAlias:
        return True
    if not isinstance(annotation, str):
        return False
    reference_parts = annotation.split(".")
    if not reference_parts or not all(part.isidentifier() for part in reference_parts):
        return False
    missing = object()
    resolved = getattr(module, reference_parts[0], missing)
    for part in reference_parts[1:]:
        if resolved is missing:
            break
        resolved = getattr(resolved, part, missing)
    return resolved is TypeAlias or resolved is typing_extensions.TypeAlias


def _module_declares_type_alias(module: object, alias_name: str, value: object) -> bool:
    annotations = getattr(module, "__annotations__", {})
    if not isinstance(annotations, Mapping) or alias_name not in annotations:
        return False
    missing = object()
    return (
        _is_type_alias_annotation(annotations[alias_name], module)
        and getattr(module, alias_name, missing) is value
    )


class _ModuleBindingVisitor(ast.NodeVisitor):
    def __init__(self, name: str):
        self.name = name
        self.count = 0
        self.has_wildcard_import = False
        self.from_imports: list[tuple[ast.ImportFrom, str]] = []
        self._bindings_target_module = True

    def _count(self, name: str | None) -> None:
        if self._bindings_target_module:
            self.count += name == self.name

    def _visit_nested_scope(self, body: list[ast.stmt]) -> None:
        bindings_target_module = _scope_declares_global(body, self.name)
        previous_bindings_target_module = self._bindings_target_module
        self._bindings_target_module = bindings_target_module
        for statement in body:
            self.visit(statement)
        self._bindings_target_module = previous_bindings_target_module

    def _visit_arguments(self, arguments: ast.arguments) -> None:
        all_arguments = [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
        if arguments.vararg is not None:
            all_arguments.append(arguments.vararg)
        if arguments.kwarg is not None:
            all_arguments.append(arguments.kwarg)
        for argument in all_arguments:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        for default in [*arguments.defaults, *arguments.kw_defaults]:
            if default is not None:
                self.visit(default)

    def _visit_function_definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._count(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._visit_arguments(node.args)
        if node.returns is not None:
            self.visit(node.returns)

    def _visit_comprehension(
        self, generators: list[ast.comprehension], values: list[ast.expr]
    ) -> None:
        for generator in generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self._count(node.id)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_definition(node)
        self._visit_nested_scope(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_definition(node)
        self._visit_nested_scope(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._count(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._visit_nested_scope(node.body)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_arguments(node.args)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._count(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        self._count(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        self._count(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        self._count(node.rest)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for imported in node.names:
            self._count(imported.asname or imported.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not self._bindings_target_module:
            return
        for imported in node.names:
            if imported.name == "*":
                self.has_wildcard_import = True
                continue
            binding_name = imported.asname or imported.name
            self._count(binding_name)
            if binding_name == self.name:
                self.from_imports.append((node, imported.name))


def _scope_declares_global(nodes: Iterable[ast.AST], name: str) -> bool:
    for node in nodes:
        if isinstance(node, ast.Global):
            if name in node.names:
                return True
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        if _scope_declares_global(ast.iter_child_nodes(node), name):
            return True
    return False


def _direct_import_source(
    module: object, export_name: str, *, package_root: str
) -> tuple[ModuleType, str] | None:
    module_name = getattr(module, "__name__", None)
    package_name = getattr(module, "__package__", None)
    if not isinstance(module_name, str) or not isinstance(package_name, str):
        return None
    try:
        module_tree = ast.parse(inspect.getsource(module))
    except (OSError, SyntaxError, TypeError):
        return None

    bindings = _ModuleBindingVisitor(export_name)
    bindings.visit(module_tree)
    if bindings.count != 1 or bindings.has_wildcard_import or len(bindings.from_imports) != 1:
        return None
    statement, source_name = bindings.from_imports[0]
    if statement.level:
        relative_name = "." * statement.level + (statement.module or "")
        try:
            source_module_name = importlib.util.resolve_name(relative_name, package_name)
        except ImportError:
            return None
    else:
        source_module_name = statement.module
    if source_module_name is None or not (
        source_module_name == package_root or source_module_name.startswith(f"{package_root}.")
    ):
        return None
    source_module = sys.modules.get(source_module_name)
    if not isinstance(source_module, ModuleType):
        return None
    return source_module, source_name


def _has_explicit_type_alias_declaration(
    agents_module: object, export_name: str, value: object
) -> bool:
    package_root = getattr(agents_module, "__name__", None)
    module, alias_name = agents_module, export_name
    visited_bindings: set[tuple[int, str]] = set()
    missing = object()
    while (id(module), alias_name) not in visited_bindings:
        visited_bindings.add((id(module), alias_name))
        if getattr(module, alias_name, missing) is not value:
            return False
        if _module_declares_type_alias(module, alias_name, value):
            return True
        if not isinstance(package_root, str):
            return False
        import_source = _direct_import_source(module, alias_name, package_root=package_root)
        if import_source is None:
            return False
        module, alias_name = import_source
    return False


def _is_public_type_alias(agents_module: object, export_name: str, value: object) -> bool:
    return (
        get_origin(value) is not None
        or _is_type_alias_type(value)
        or _has_explicit_type_alias_declaration(agents_module, export_name, value)
    )


def _type_alias_definition(
    value: object, *, visited_alias_ids: frozenset[int] = frozenset()
) -> dict[str, object]:
    if value is Any:
        return {"kind": "any"}
    if _is_type_alias_type(value):
        if value.__type_params__:
            raise TypeError(f"generic public type alias is unsupported: {value.__name__}")
        alias_id = id(value)
        if alias_id in visited_alias_ids:
            alias_name = getattr(value, "__name__", repr(value))
            raise TypeError(f"recursive public type alias is unsupported: {alias_name}")
        try:
            alias_value = value.__value__
        except Exception as error:
            raise TypeError(
                f"cannot resolve public type alias {value.__name__} at runtime: "
                f"{type(error).__name__}: {error}"
            ) from None
        return _type_alias_definition(alias_value, visited_alias_ids=visited_alias_ids | {alias_id})
    origin = get_origin(value)
    if origin is Literal:
        literal_values: list[dict[str, object]] = []
        for literal_value in get_args(value):
            literal_contract = _default_contract(literal_value)
            if literal_contract["kind"] not in {"literal", "enum"}:
                raise TypeError(
                    "public type alias Literal members must use supported literal or enum values"
                )
            literal_values.append(literal_contract)
        return {
            "kind": "literal",
            "values": _sorted_type_alias_members(literal_values),
        }
    if origin in {Union, UnionType}:
        members = [
            _type_alias_definition(member, visited_alias_ids=visited_alias_ids)
            for member in get_args(value)
        ]
        return {
            "kind": "union",
            "members": _sorted_type_alias_members(members),
        }
    if origin is Callable:
        callable_args = get_args(value)
        if len(callable_args) != 2:
            raise TypeError(
                "public Callable type aliases must declare parameters and a return type"
            )
        parameter_types, return_type = callable_args
        if parameter_types is Ellipsis or not isinstance(parameter_types, list | tuple):
            raise TypeError("public Callable type aliases must declare explicit parameter types")
        return {
            "kind": "callable",
            "parameters": [
                _type_alias_definition(parameter_type, visited_alias_ids=visited_alias_ids)
                for parameter_type in parameter_types
            ],
            "return": _type_alias_definition(return_type, visited_alias_ids=visited_alias_ids),
        }
    if origin is not None:
        if not isinstance(origin, type) or not (
            origin.__module__ == "agents" or origin.__module__.startswith("agents.")
        ):
            raise TypeError(f"unsupported public generic type alias origin: {origin!r}")
        return {
            "kind": "generic",
            "origin": f"{origin.__module__}.{origin.__qualname__}",
            "arguments": [
                _type_alias_definition(argument, visited_alias_ids=visited_alias_ids)
                for argument in get_args(value)
            ],
        }
    if isinstance(value, type) and (
        value.__module__ == "builtins"
        or value.__module__ == "agents"
        or value.__module__.startswith("agents.")
    ):
        return {
            "kind": "type",
            "identity": f"{value.__module__}.{value.__qualname__}",
        }
    raise TypeError(f"unsupported public type alias member: {value!r}")


def _public_type_alias_contract(
    policy_entries: Iterable[Mapping[str, str]],
    agents_module: Any | None,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    missing = object()
    for policy_entry in policy_entries:
        module_name = policy_entry["module"]
        alias_name = policy_entry["name"]
        module = _import_contract_module(module_name, agents_module)
        alias = getattr(module, alias_name, missing)
        if alias is missing:
            raise ValueError(
                f"Cannot promote public type alias {module_name}.{alias_name} because it is missing"
            )
        try:
            definition = _type_alias_definition(alias)
        except TypeError as error:
            raise ValueError(
                f"Cannot promote public type alias {module_name}.{alias_name}: {error}"
            ) from None
        entries.append({"definition": definition, "module": module_name, "name": alias_name})
    return entries


def _typed_dict_field_is_required(typed_dict: type, name: str, annotation: object) -> bool:
    if isinstance(annotation, ForwardRef):
        annotation_text = annotation.__forward_arg__
        if annotation_text.startswith(
            ("Required[", "typing.Required[", "typing_extensions.Required[")
        ):
            return True
        if annotation_text.startswith(
            ("NotRequired[", "typing.NotRequired[", "typing_extensions.NotRequired[")
        ):
            return False
    origin = get_origin(annotation)
    if origin is Required:
        return True
    if origin is NotRequired:
        return False
    required_keys = getattr(typed_dict, "__required_keys__", frozenset())
    optional_keys = getattr(typed_dict, "__optional_keys__", frozenset())
    if name in required_keys:
        return True
    if name in optional_keys:
        return False
    return bool(getattr(typed_dict, "__total__", True))


def _typed_dict_field_contract(typed_dict: type, name: str) -> dict[str, object] | None:
    annotation = getattr(typed_dict, "__annotations__", {}).get(name)
    if annotation is None:
        return None
    return {
        "name": name,
        "required": _typed_dict_field_is_required(typed_dict, name, annotation),
        "annotation": _annotation_contract(annotation),
    }


def _public_typed_dict_contract(
    policy_entries: Iterable[Mapping[str, Any]],
    agents_module: Any | None,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for policy_entry in policy_entries:
        module_name = cast(str, policy_entry["module"])
        class_name = cast(str, policy_entry["class_name"])
        module = _import_contract_module(module_name, agents_module)
        typed_dict = getattr(module, class_name, None)
        if not typing_extensions.is_typeddict(typed_dict):
            raise ValueError(
                f"Cannot promote public TypedDict {module_name}.{class_name} because it is "
                "missing or no longer a TypedDict"
            )
        fields: list[dict[str, object]] = []
        for name in policy_entry["names"]:
            field = _typed_dict_field_contract(typed_dict, name)
            if field is None:
                raise ValueError(
                    f"Cannot promote public TypedDict field {module_name}.{class_name}.{name} "
                    "because it is missing"
                )
            fields.append(field)
        entries.append({"class_name": class_name, "fields": fields, "module": module_name})
    return entries


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
    dependency = _optional_dependency_for_binding_in_modules(
        contract.get("required_submodule_exports", {}), module_name, binding_name
    )
    if dependency is not None:
        return dependency
    canonical_dependencies = {
        _optional_dependency_for_binding_in_modules(
            contract.get("required_submodule_exports", {}), entry["module"], entry["name"]
        )
        for entry in contract.get("canonical_imports", [])
        if entry["canonical_module"] == module_name and entry["canonical_name"] == binding_name
    }
    if canonical_dependencies and len(canonical_dependencies) == 1:
        return next(iter(canonical_dependencies))
    return None


def _optional_dependency_for_binding_in_modules(
    modules: Mapping[str, Any], module_name: str, binding_name: str
) -> str | None:
    module_contract = modules.get(module_name, {})
    for field_name in ("optional_bindings", "optional_exports"):
        dependency_module = module_contract.get(field_name, {}).get(binding_name)
        if dependency_module is not None:
            return cast(str, dependency_module)
    return None


def _optional_dependency_for_module_import(
    contract: Mapping[str, Any], module_name: str
) -> str | None:
    modules = contract.get("required_submodule_exports", {})
    module_contract = modules.get(module_name, {})
    names = module_contract.get("names", [])
    try:
        optional_bindings = _optional_dependency_modules(
            module_contract.get("optional_bindings", {}), field_name="optional_bindings"
        )
        optional_exports = _optional_dependency_modules(
            module_contract.get("optional_exports", {}), field_name="optional_exports"
        )
    except ValueError:
        return None
    dependencies = {optional_bindings.get(name) or optional_exports.get(name) for name in names}
    if names and len(dependencies) == 1 and None not in dependencies:
        return cast(str, next(iter(dependencies)))
    if names:
        return None
    canonical_dependencies = {
        _optional_dependency_for_binding(contract, entry["module"], entry["name"])
        for entry in contract.get("canonical_imports", [])
        if entry["canonical_module"] == module_name
    }
    if canonical_dependencies and len(canonical_dependencies) == 1:
        dependency = next(iter(canonical_dependencies))
        if dependency is not None:
            return dependency
    return None


def _import_contract_module(module_name: str, agents_module: Any | None) -> Any:
    if module_name == "agents" and agents_module is not None:
        return agents_module
    return importlib.import_module(module_name)


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
