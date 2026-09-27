"""Compare released API contracts with current public surface descriptions."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Mapping
from typing import Any, cast, get_type_hints

import typing_extensions

from integration_tests import _contract_surface as surface


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


def _validate_public_property_contract(
    contract: dict[str, Any],
    agents_module: Any | None,
    *,
    unsupported_platforms: Mapping[str, tuple[str, ...]] | None = None,
) -> list[str]:
    errors: list[str] = []
    unsupported_platforms = unsupported_platforms or {}
    for entry in contract.get("public_properties", []):
        module_name = entry["module"]
        owner_name = entry.get("class_name", entry.get("factory_name"))
        optional_dependency = surface._optional_dependency_for_binding(
            contract, module_name, owner_name
        )
        if (
            optional_dependency is not None
            and not surface._optional_dependency_is_available_for_contract(
                optional_dependency, unsupported_platforms
            )
        ):
            continue
        try:
            module = surface._import_contract_module(module_name, agents_module)
        except Exception as error:
            errors.append(f"Failed to import released module {module_name}: {error!r}")
            continue
        if "class_name" in entry:
            class_value = getattr(module, owner_name, None)
            if not isinstance(class_value, type):
                errors.append(f"Missing released public class {module_name}.{owner_name}")
                continue
        else:
            factory = getattr(module, owner_name, None)
            if not callable(factory):
                errors.append(f"Missing released public factory {module_name}.{owner_name}")
                continue
            try:
                class_value = get_type_hints(factory)["return"]
            except (KeyError, NameError, TypeError) as error:
                errors.append(
                    f"Unable to resolve released public factory return type "
                    f"{module_name}.{owner_name}: {error!r}"
                )
                continue
            if not isinstance(class_value, type):
                errors.append(
                    f"Released public factory {module_name}.{owner_name} no longer returns a class"
                )
                continue
        for property_name in entry["names"]:
            descriptor = inspect.getattr_static(class_value, property_name, None)
            if not isinstance(descriptor, property):
                errors.append(
                    f"{module_name}.{owner_name}.{property_name} "
                    "removed or changed a released public property"
                )
    return errors


def _validate_public_class_contract(
    contract: dict[str, Any],
    agents_module: Any | None,
    *,
    unsupported_platforms: Mapping[str, tuple[str, ...]] | None = None,
) -> list[str]:
    errors: list[str] = []
    unsupported_platforms = unsupported_platforms or {}
    for entry in contract.get("public_class_contracts", []):
        module_name = entry["module"]
        class_name = entry["class_name"]
        optional_dependency = surface._optional_dependency_for_binding(
            contract, module_name, class_name
        )
        if (
            optional_dependency is not None
            and not surface._optional_dependency_is_available_for_contract(
                optional_dependency, unsupported_platforms
            )
        ):
            continue
        try:
            module = surface._import_contract_module(module_name, agents_module)
        except Exception as error:
            errors.append(f"Failed to import released module {module_name}: {error!r}")
            continue
        class_value = getattr(module, class_name, None)
        if not isinstance(class_value, type):
            errors.append(f"Missing released public class {module_name}.{class_name}")
            continue
        if "abstract" in entry and inspect.isabstract(class_value) != entry["abstract"]:
            expected_state = "abstract" if entry["abstract"] else "concrete"
            current_state = "abstract" if inspect.isabstract(class_value) else "concrete"
            errors.append(
                f"{module_name}.{class_name} changed its released public class state: "
                f"expected {expected_state}, got {current_state}"
            )
        if "abstract_members" in entry:
            current_members = sorted(getattr(class_value, "__abstractmethods__", ()))
            if current_members != entry["abstract_members"]:
                errors.append(
                    f"{module_name}.{class_name} changed its released public abstract members: "
                    f"expected {entry['abstract_members']!r}, got {current_members!r}"
                )
    return errors


def _validate_public_typed_dict_contract(
    contract: dict[str, Any],
    agents_module: Any | None,
    *,
    unsupported_platforms: Mapping[str, tuple[str, ...]] | None = None,
) -> list[str]:
    errors: list[str] = []
    unsupported_platforms = unsupported_platforms or {}
    for entry in contract.get("public_typed_dicts", []):
        module_name = entry["module"]
        class_name = entry["class_name"]
        optional_dependency = surface._optional_dependency_for_binding(
            contract, module_name, class_name
        )
        if (
            optional_dependency is not None
            and not surface._optional_dependency_is_available_for_contract(
                optional_dependency, unsupported_platforms
            )
        ):
            continue
        try:
            module = surface._import_contract_module(module_name, agents_module)
        except Exception as error:
            errors.append(f"Failed to import released module {module_name}: {error!r}")
            continue
        typed_dict = getattr(module, class_name, None)
        if not typing_extensions.is_typeddict(typed_dict):
            errors.append(f"Missing released public TypedDict {module_name}.{class_name}")
            continue
        for released_field in entry["fields"]:
            current_field = surface._typed_dict_field_contract(typed_dict, released_field["name"])
            if current_field != released_field:
                errors.append(
                    f"{module_name}.{class_name}.{released_field['name']} changed its released "
                    f"TypedDict field contract: expected {released_field!r}, got "
                    f"{current_field!r}"
                )
    return errors


def _validate_public_type_alias_contract(
    contract: dict[str, Any],
    agents_module: Any | None,
    *,
    unsupported_platforms: Mapping[str, tuple[str, ...]] | None = None,
) -> list[str]:
    errors: list[str] = []
    unsupported_platforms = unsupported_platforms or {}
    missing = object()
    for entry in contract.get("public_type_aliases", []):
        module_name = entry["module"]
        alias_name = entry["name"]
        optional_dependency = surface._optional_dependency_for_binding(
            contract, module_name, alias_name
        )
        if (
            optional_dependency is not None
            and not surface._optional_dependency_is_available_for_contract(
                optional_dependency, unsupported_platforms
            )
        ):
            continue
        try:
            module = surface._import_contract_module(module_name, agents_module)
        except Exception as error:
            errors.append(f"Failed to import released module {module_name}: {error!r}")
            continue
        alias = getattr(module, alias_name, missing)
        if alias is missing:
            errors.append(f"Missing released public type alias {module_name}.{alias_name}")
            continue
        try:
            current_definition = surface._type_alias_definition(alias)
        except TypeError as error:
            errors.append(
                f"{module_name}.{alias_name} no longer has a supported released public type "
                f"alias definition: {error}"
            )
            continue
        if current_definition != entry["definition"]:
            errors.append(
                f"{module_name}.{alias_name} changed its released public type alias: "
                f"expected {entry['definition']!r}, got {current_definition!r}"
            )
    return errors


def validate_released_api_contract(
    contract: dict[str, Any],
    *,
    agents_module: Any | None = None,
) -> list[str]:
    agents = agents_module or importlib.import_module("agents")
    errors: list[str] = []

    try:
        unsupported_platforms = surface._optional_dependency_unsupported_platforms(contract)
    except ValueError as error:
        errors.append(f"Invalid released optional dependency platform declarations: {error}")
        unsupported_platforms = {}

    errors.extend(
        _validate_public_class_contract(
            contract,
            agents_module,
            unsupported_platforms=unsupported_platforms,
        )
    )
    errors.extend(
        _validate_public_property_contract(
            contract,
            agents_module,
            unsupported_platforms=unsupported_platforms,
        )
    )
    errors.extend(
        _validate_public_type_alias_contract(
            contract,
            agents_module,
            unsupported_platforms=unsupported_platforms,
        )
    )
    errors.extend(
        _validate_public_typed_dict_contract(
            contract,
            agents_module,
            unsupported_platforms=unsupported_platforms,
        )
    )

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
            imported_modules[module_name] = surface._import_contract_module(
                module_name, agents_module
            )
        except Exception as error:
            if surface._matches_platform_import_error(contract, module_name, error):
                continue
            optional_dependency = surface._optional_dependency_for_module_import(
                contract, module_name
            )
            if optional_dependency is not None and not (
                surface._optional_dependency_is_available_for_contract(
                    optional_dependency, unsupported_platforms
                )
            ):
                continue
            errors.append(f"Failed to import released module {module_name}: {error!r}")

    for module_name, released in contract.get("required_submodule_exports", {}).items():
        module = imported_modules.get(module_name)
        if module is None:
            continue
        try:
            current = surface._submodule_export_contract(module)
        except ValueError as error:
            errors.append(f"Invalid released module exports for {module_name}: {error}")
            continue
        if current is None:
            errors.append(f"Released module {module_name} no longer defines __all__")
            continue
        try:
            optional_exports = surface._optional_dependency_modules(
                released.get("optional_exports", {}), field_name="optional_exports"
            )
            optional_bindings = surface._optional_dependency_modules(
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
                if surface._optional_dependency_is_unsupported_for_contract(
                    dependency_module, unsupported_platforms
                )
            }
            unsupported_optional_bindings = {
                name
                for name, dependency_module in (optional_bindings | optional_exports).items()
                if surface._optional_dependency_is_unsupported_for_contract(
                    dependency_module, unsupported_platforms
                )
            }
            unavailable_optional_exports = {
                name
                for name, dependency_module in optional_exports.items()
                if not surface._optional_dependency_is_available_for_contract(
                    dependency_module, unsupported_platforms
                )
            }
            unavailable_optional_bindings = {
                name
                for name, dependency_module in (optional_bindings | optional_exports).items()
                if not surface._optional_dependency_is_available_for_contract(
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
        optional_dependency = surface._optional_dependency_for_binding(
            contract, entry["module"], entry["name"]
        )
        if (
            optional_dependency is not None
            and not surface._optional_dependency_is_available_for_contract(
                optional_dependency, unsupported_platforms
            )
        ):
            continue
        try:
            module = surface._import_contract_module(entry["module"], agents_module)
        except Exception as error:
            if surface._matches_platform_import_error(contract, entry["module"], error):
                continue
            errors.append(f"Failed to import released module {entry['module']}: {error!r}")
            continue
        try:
            canonical = surface._import_contract_module(entry["canonical_module"], agents_module)
        except Exception as error:
            if surface._matches_platform_import_error(contract, entry["canonical_module"], error):
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
            optional_dependency = surface._optional_dependency_for_binding(
                contract, module_name, binding_name
            )
            if optional_dependency is not None and not (
                surface._optional_dependency_is_available_for_contract(
                    optional_dependency, unsupported_platforms
                )
            ):
                continue
            try:
                module = surface._import_contract_module(module_name, agents_module)
            except Exception as error:
                if surface._matches_platform_import_error(contract, module_name, error):
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
                        surface._import_contract_module(
                            canonical_entry["canonical_module"], agents_module
                        )
                    except Exception as error:
                        if surface._matches_platform_import_error(
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
        current_kind = surface._callable_kind(value)
        if current_kind != released["kind"]:
            errors.append(
                f"Released callable {module_name}.{binding_name} changed kind from "
                f"{released['kind']} to {current_kind or type(value).__name__}"
            )
            continue
        released_execution_kind = released.get("execution_kind")
        if released_execution_kind is not None:
            current_execution_kind = surface._function_execution_kind(value)
            if current_execution_kind != released_execution_kind:
                errors.append(
                    f"{name} changed execution from "
                    f"{released_execution_kind} to {current_execution_kind}"
                )
        current_parameters = surface._parameter_contract(value)
        errors.extend(
            _validate_parameter_contract(name, released["parameters"], current_parameters)
        )
        current_fields = surface._dataclass_field_contract(value)
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
                    surface._pydantic_model_field_contract(value),
                )
            )
        for member_name, released_member in released.get("members", {}).items():
            descriptor = surface._sdk_public_class_descriptor(value, member_name)
            current_member = surface._class_member_contract(descriptor)
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
            current_enum_members = surface._enum_member_contract(value)
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
