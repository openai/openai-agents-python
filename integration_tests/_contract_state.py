"""Exercise historical state readers, resume behavior, and redaction observations."""

from __future__ import annotations

import dataclasses
import json
import logging
import traceback
from collections.abc import Iterable, Mapping
from copy import deepcopy
from pathlib import Path
from types import TracebackType
from typing import Any, cast


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
    from agents.testing import ModelStep, ScriptedModel

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
    model = ScriptedModel(
        [ModelStep(output=turn, response_id="queued-fake-response") for turn in model_turns]
    )
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
