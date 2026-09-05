"""Promote the public API contract using shared surface descriptions and validation."""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from integration_tests import _contract_surface as surface, _contract_validation as validation


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
    by_identity = {surface._public_property_identity(entry): entry for entry in result}
    for entry_value in promoted:
        entry = deepcopy(dict(entry_value))
        identity = surface._public_property_identity(entry)
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


def _merge_public_class_contracts(
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
        for field_name in ("abstract", "abstract_members"):
            if field_name not in entry:
                continue
            previous_value = previous.setdefault(field_name, entry[field_name])
            if previous_value != entry[field_name]:
                raise ValueError(
                    "release policy public class contract conflicts with the released contract "
                    f"for {entry['module']}.{entry['class_name']} field {field_name}"
                )
    return result


def _validate_voice_public_class_contract_policy(
    release_policy: surface.SubmoduleExportPolicy,
    agents_module: Any | None,
) -> None:
    voice_class_exports: list[tuple[Mapping[str, str], type[Any]]] = []
    for entry in release_policy.canonical_imports:
        if entry["module"] != "agents.voice":
            continue
        canonical_module = surface._import_contract_module(entry["canonical_module"], agents_module)
        value = getattr(canonical_module, entry["canonical_name"], None)
        if isinstance(value, type):
            voice_class_exports.append((entry, value))

    abstract_bases = {
        class_value for _, class_value in voice_class_exports if inspect.isabstract(class_value)
    }
    policy_by_identity = {
        (entry["module"], entry["class_name"]): entry
        for entry in release_policy.public_class_contracts
    }
    missing_entries: list[dict[str, object]] = []
    for canonical_import, class_value in voice_class_exports:
        is_abstract = inspect.isabstract(class_value)
        if not is_abstract and not any(
            issubclass(class_value, abstract_base) for abstract_base in abstract_bases
        ):
            continue

        identity = (
            canonical_import["canonical_module"],
            canonical_import["canonical_name"],
        )
        policy_entry = policy_by_identity.get(identity)
        has_explicit_state = policy_entry is not None and (
            (is_abstract and ("abstract" in policy_entry or "abstract_members" in policy_entry))
            or (not is_abstract and policy_entry.get("abstract") is False)
        )
        if not has_explicit_state:
            missing_entries.append(
                {
                    "abstract": is_abstract,
                    "class_name": canonical_import["canonical_name"],
                    "module": canonical_import["canonical_module"],
                }
            )

    if missing_entries:
        raise ValueError(
            "Cannot promote the public Voice API without explicit public_class_contracts "
            "coverage for its abstract bases and concrete implementations. Add or correct "
            f"these policy entries: {missing_entries!r}. Required classes are derived from "
            "canonical agents.voice imports and their public abstract-base relationships."
        )


def _merge_public_type_aliases(
    existing: Iterable[Mapping[str, Any]], promoted: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    result = [deepcopy(dict(entry)) for entry in existing]
    identities = {(entry["module"], entry["name"]) for entry in result}
    for entry_value in promoted:
        entry = deepcopy(dict(entry_value))
        identity = (entry["module"], entry["name"])
        if identity not in identities:
            result.append(entry)
            identities.add(identity)
    return result


def _merge_public_typed_dicts(
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
        previous_by_name = {field["name"]: field for field in previous["fields"]}
        for field in entry["fields"]:
            existing_field = previous_by_name.get(field["name"])
            if existing_field is not None and existing_field != field:
                raise ValueError(
                    "release policy public TypedDict field conflicts with the released contract "
                    f"for {entry['module']}.{entry['class_name']}.{field['name']}"
                )
            if existing_field is None:
                previous["fields"].append(field)
                previous_by_name[field["name"]] = field
    return result


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


def _preserve_released_submodule_callables(
    contract: Mapping[str, Any], callables: dict[str, Any], module_name: str
) -> None:
    for qualified_name, released_callable in contract["callables"].items():
        callable_module, _, _ = qualified_name.rpartition(".")
        if callable_module == module_name:
            callables.setdefault(qualified_name, deepcopy(released_callable))


def build_released_api_contract(
    contract: dict[str, Any],
    *,
    baseline: str,
    baseline_commit: str,
    agents_module: Any | None = None,
    release_policy: surface.SubmoduleExportPolicy | None = None,
) -> dict[str, Any]:
    """Build the next rolling release contract from the current public surface."""
    agents = agents_module or importlib.import_module("agents")
    if release_policy is not None:
        _validate_voice_public_class_contract_policy(release_policy, agents_module)

    compatibility_errors = validation.validate_released_api_contract(contract, agents_module=agents)
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
    if release_policy is not None:
        promoted_top_level_type_aliases = {
            entry["name"]
            for entry in release_policy.public_type_aliases
            if entry["module"] == "agents"
        }
        missing_top_level_type_aliases = sorted(
            name
            for name in current_export_names - released_exports
            if surface._is_public_type_alias(agents, name, getattr(agents, name))
            and name not in promoted_top_level_type_aliases
        )
        if missing_top_level_type_aliases:
            raise ValueError(
                "Cannot promote new top-level type aliases without public_type_aliases policy "
                "entries for module 'agents': "
                f"{missing_top_level_type_aliases!r}"
            )
    ordered_exports = [name for name in released_export_order if name in current_export_names]
    ordered_exports.extend(name for name in current_exports if name not in released_exports)
    tracked_callables = set(contract["callables"])
    callables: dict[str, Any] = {}
    for name in ordered_exports:
        value = getattr(agents, name)
        kind = surface._callable_kind(value)
        should_track = name in tracked_callables
        if not should_track and kind is not None:
            try:
                surface._signature(value)
            except (TypeError, ValueError):
                continue
            should_track = True
        if should_track:
            callables[name] = surface._callable_contract(value)

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
            surface._optional_dependency_for_binding_in_modules(
                release_policy.modules, module_name, entry["name"]
            )
            if release_policy is not None
            else None
        )
        if (
            optional_dependency is not None
            and not surface._optional_dependency_is_available_for_contract(
                optional_dependency, policy_unsupported_platforms
            )
        ):
            if surface._optional_dependency_is_unsupported_for_contract(
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
            module = surface._import_contract_module(module_name, agents_module)
        except Exception as error:
            if surface._matches_platform_import_error(contract, module_name, error):
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
                surface._import_contract_module(entry["canonical_module"], agents_module)
            except Exception as error:
                if surface._matches_platform_import_error(
                    contract, entry["canonical_module"], error
                ):
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
        kind = surface._callable_kind(value)
        if kind is None:
            continue
        try:
            surface._signature(value)
        except (TypeError, ValueError):
            continue
        callables[qualified_name] = surface._callable_contract(value)

    updated = deepcopy(contract)
    updated["baseline"] = baseline
    updated["required_top_level_exports"] = ordered_exports
    updated["callables"] = callables
    updated["canonical_imports"] = canonical_imports
    updated["public_class_contracts"] = _merge_public_class_contracts(
        contract.get("public_class_contracts", []),
        release_policy.public_class_contracts if release_policy is not None else (),
    )
    updated["public_properties"] = _merge_public_properties(
        contract.get("public_properties", []),
        release_policy.public_properties if release_policy is not None else (),
    )
    updated["public_type_aliases"] = _merge_public_type_aliases(
        contract.get("public_type_aliases", []),
        surface._public_type_alias_contract(release_policy.public_type_aliases, agents_module)
        if release_policy is not None
        else (),
    )
    updated["public_typed_dicts"] = _merge_public_typed_dicts(
        contract.get("public_typed_dicts", []),
        surface._public_typed_dict_contract(release_policy.public_typed_dicts, agents_module)
        if release_policy is not None
        else (),
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
                for dependency_module in surface._optional_dependency_modules(
                    dict(module_policy.get(field_name, {})), field_name=field_name
                ).values()
                if not surface._optional_dependency_is_unsupported_for_contract(
                    dependency_module, policy_unsupported_platforms
                )
                and not surface._optional_dependency_is_available(dependency_module)
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
    released_submodule_exports = contract.get("required_submodule_exports", {})
    for module_name in public_modules:
        if module_name == "agents" or module_name in excluded_submodule_exports:
            continue
        try:
            module = surface._import_contract_module(module_name, agents_module)
        except Exception as error:
            if surface._matches_platform_import_error(contract, module_name, error):
                _preserve_released_submodule_callables(contract, callables, module_name)
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
            for name, dependency_module in surface._optional_dependency_modules(
                dict(module_policy.get("optional_exports", {})),
                field_name="optional_exports",
            ).items()
            if surface._optional_dependency_is_unsupported_for_contract(
                dependency_module, policy_unsupported_platforms
            )
        }
        module_contract = surface._submodule_export_contract(
            module,
            optional_bindings=module_policy.get("optional_bindings", {}),
            optional_exports=module_policy.get("optional_exports", {}),
            allowed_missing_optional_exports=allowed_missing_optional_exports,
        )
        if module_contract is not None:
            required_submodule_exports[module_name] = module_contract
            released_names = set(released_submodule_exports.get(module_name, {}).get("names", []))
            for name in module_contract["names"]:
                qualified_name = f"{module_name}.{name}"
                was_tracked = qualified_name in tracked_callables
                if name in released_names and not was_tracked:
                    continue
                optional_dependency = surface._optional_dependency_for_binding_in_modules(
                    {module_name: module_contract}, module_name, name
                )
                if optional_dependency is not None and not (
                    surface._optional_dependency_is_available_for_contract(
                        optional_dependency, policy_unsupported_platforms
                    )
                ):
                    if was_tracked:
                        callables[qualified_name] = deepcopy(contract["callables"][qualified_name])
                    continue
                value = getattr(module, name, None)
                if value is None:
                    continue
                if not was_tracked and not surface._is_sdk_owned_callable(value):
                    continue
                kind = surface._callable_kind(value)
                if kind is None:
                    continue
                try:
                    surface._signature(value)
                except (TypeError, ValueError):
                    if was_tracked:
                        callables[qualified_name] = deepcopy(contract["callables"][qualified_name])
                    continue
                callables[qualified_name] = surface._callable_contract(value)
    updated["required_submodule_exports"] = required_submodule_exports

    updated_errors = validation.validate_released_api_contract(updated, agents_module=agents)
    if updated_errors:
        details = "\n".join(f"- {error}" for error in updated_errors)
        raise ValueError(f"Cannot promote an invalid released API contract:\n{details}")

    surface_keys = (
        "canonical_imports",
        "callables",
        "optional_dependency_unsupported_platforms",
        "platform_import_errors",
        "public_class_contracts",
        "public_properties",
        "public_type_aliases",
        "public_typed_dicts",
        "public_modules",
        "required_submodule_exports",
        "required_top_level_exports",
        "submodule_export_exclusions",
    )
    surface_changed = any(updated.get(key) != contract.get(key) for key in surface_keys)
    if baseline != contract["baseline"] or surface_changed:
        updated["baseline_commit"] = baseline_commit
    return updated
