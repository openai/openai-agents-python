from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast


def load_api_contract(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _default_contract(value: object) -> dict[str, object]:
    if value is inspect.Parameter.empty or value is dataclasses.MISSING:
        return {"kind": "required"}
    if value.__class__.__name__ == "_HAS_DEFAULT_FACTORY_CLASS":
        return {"kind": "factory"}
    if value is None or isinstance(value, bool | int | float | str):
        return {"kind": "literal", "value": value}
    return {
        "kind": "typed",
        "type": f"{type(value).__module__}.{type(value).__qualname__}",
    }


def _parameter_contract(value: Callable[..., Any]) -> list[dict[str, object]]:
    return [
        {
            "name": parameter.name,
            "kind": parameter.kind.name,
            "default": _default_contract(parameter.default),
        }
        for parameter in inspect.signature(value).parameters.values()
    ]


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


def validate_released_api_contract(contract: dict[str, Any]) -> list[str]:
    agents = importlib.import_module("agents")
    errors: list[str] = []

    missing_exports = sorted(set(contract["required_top_level_exports"]) - set(agents.__all__))
    if missing_exports:
        errors.append(f"Missing released top-level exports: {missing_exports!r}")
    missing_bindings = sorted(
        name for name in contract["required_top_level_exports"] if not hasattr(agents, name)
    )
    if missing_bindings:
        errors.append(f"Missing released top-level bindings: {missing_bindings!r}")

    for module_name in contract["public_modules"]:
        try:
            importlib.import_module(module_name)
        except Exception as error:
            errors.append(f"Failed to import released module {module_name}: {error!r}")

    for entry in contract["canonical_imports"]:
        module = importlib.import_module(entry["module"])
        canonical = importlib.import_module(entry["canonical_module"])
        actual = getattr(module, entry["name"], None)
        expected = getattr(canonical, entry["canonical_name"], None)
        if actual is not expected:
            errors.append(
                f"{entry['module']}.{entry['name']} no longer resolves to "
                f"{entry['canonical_module']}.{entry['canonical_name']}"
            )

    for name, released in contract["constructors"].items():
        value = getattr(agents, name, None)
        if value is None:
            errors.append(f"Missing released constructor agents.{name}")
            continue
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

    return errors


def _semantic_projection(payload: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for key in (
        "current_agent",
        "original_input",
        "generated_items",
        "session_items",
        "current_step",
        "model_responses",
        "max_turns",
        "generated_prompt_cache_key",
        "reasoning_item_id_policy",
        "nested_history_owned_session_item_refs",
        "sandbox",
        "trace",
    ):
        if key in payload:
            projected[key] = payload[key]

    context = payload.get("context")
    if isinstance(context, dict):
        projected["context"] = {
            key: context[key]
            for key in (
                "usage",
                "approvals",
                "hosted_mcp_approvals",
                "tool_invocations",
            )
            if key in context
        }
    return projected


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
    original_version = payload.get("$schemaVersion")
    agent = _restore_agent(payload)
    restored = await RunState.from_json(agent, payload)
    canonical = restored.to_json()

    if canonical.get("$schemaVersion") != CURRENT_SCHEMA_VERSION:
        errors.append(
            f"{path.name} rewrote as {canonical.get('$schemaVersion')!r}, "
            f"expected {CURRENT_SCHEMA_VERSION!r}"
        )
    semantic_errors = _find_subset_errors(
        _semantic_projection(payload),
        _semantic_projection(canonical),
    )
    errors.extend(f"{path.name}: {error}" for error in semantic_errors)

    rerestored = await RunState.from_json(agent, canonical)
    recanonical = rerestored.to_json()
    if recanonical != canonical:
        errors.append(
            f"{path.name} was not idempotent after rewriting schema {original_version!r} "
            f"to {CURRENT_SCHEMA_VERSION!r}"
        )
    return errors
