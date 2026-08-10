import asyncio
import builtins
import json
import logging
import sys
import types
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

import agents.run_state as run_state_module

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup
else:
    BaseExceptionGroup = builtins.BaseExceptionGroup

from agents import Agent, RunState, UserError
from agents.run_context import RunContextWrapper
from agents.run_state import SUPPORTED_SCHEMA_VERSIONS
from agents.sandbox.entries.mounts.patterns import FuseMountConfig
from integration_tests._contract_support import (
    _deserialize_common_sandbox_session_state,
    _find_subset_errors,
    _normalized_durable_state,
    _redaction_observables,
    validate_historical_resume_behavior,
    validate_historical_run_state_fixture,
    validate_legacy_credential_run_state_fixture,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "run_state"
SOURCES = json.loads((FIXTURE_ROOT / "sources.json").read_text(encoding="utf-8"))


def test_redaction_observables_include_nested_exception_owned_state() -> None:
    root_sentinel = "sentinel-root-exception-secret"
    nested_sentinel = "sentinel-nested-exception-secret"
    args_sentinel = "sentinel-exception-args-secret"
    group_sentinel = "sentinel-exception-group-secret"
    record_sentinel = "sentinel-log-record-secret"
    exc_info_sentinel = "sentinel-exc-info-secret"
    traceback_sentinel = "sentinel-traceback-local-secret"
    dataclass_sentinel = "sentinel-traceback-dataclass-secret"

    synthetic_globals: dict[str, Any] = {"__name__": "agents.synthetic_redaction_test"}
    exec(
        "def raise_with_sensitive_local(secret):\n"
        "    sensitive_payload = {'credential': secret}\n"
        "    if not sensitive_payload:\n"
        "        return\n"
        "    raise RuntimeError('sanitized traceback error')\n",
        synthetic_globals,
    )
    raise_with_sensitive_local = synthetic_globals["raise_with_sensitive_local"]
    synthetic_globals["FuseMountConfig"] = FuseMountConfig
    exec(
        "def raise_with_sensitive_config(config):\n"
        "    raise RuntimeError('sanitized config traceback error')\n",
        synthetic_globals,
    )
    raise_with_sensitive_config = synthetic_globals["raise_with_sensitive_config"]

    try:
        raise_with_sensitive_local(traceback_sentinel)
    except RuntimeError as caught:
        traceback_error = caught
    try:
        raise_with_sensitive_config(
            FuseMountConfig(
                account="account",
                container="container",
                endpoint=f"https://user:{dataclass_sentinel}@example.test",
                identity_client_id=None,
                account_key=None,
                mount_type="azure_blob_mount",
            )
        )
    except RuntimeError as caught:
        dataclass_traceback_error = caught
    nested = RuntimeError("sanitized nested error")
    nested.payload = {"credential": nested_sentinel}  # type: ignore[attr-defined]
    args_error = RuntimeError("sanitized args error")
    args_error.payload = {"credential": args_sentinel}  # type: ignore[attr-defined]
    group_error = RuntimeError("sanitized group error")
    group_error.payload = {"credential": group_sentinel}  # type: ignore[attr-defined]
    exception_group = BaseExceptionGroup("sanitized group", [group_error])
    error = ValueError("sanitized outer error")
    error.payload = {  # type: ignore[attr-defined]
        "credential": root_sentinel,
        "nested": [nested],
    }
    error.payload["cycle"] = error  # type: ignore[attr-defined]
    error.args = (
        "sanitized outer error",
        args_error,
        exception_group,
        traceback_error,
        dataclass_traceback_error,
    )
    record_error = RuntimeError("sanitized record error")
    record_error.payload = {"credential": record_sentinel}  # type: ignore[attr-defined]
    exc_info_error = RuntimeError("sanitized exc_info error")
    exc_info_error.payload = {"credential": exc_info_sentinel}  # type: ignore[attr-defined]
    record = logging.LogRecord(
        name="redaction-test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=0,
        msg="sanitized log",
        args=(),
        exc_info=(RuntimeError, exc_info_error, None),
    )
    record.nested_error = record_error

    observables = _redaction_observables(error, [record])

    assert root_sentinel in observables
    assert nested_sentinel in observables
    assert args_sentinel in observables
    assert group_sentinel in observables
    assert record_sentinel in observables
    assert exc_info_sentinel in observables
    assert traceback_sentinel in observables
    assert dataclass_sentinel in observables


@pytest.mark.parametrize(
    "field_name",
    [
        "no_active_agent_run",
        "last_model_response",
        "generated_session_item_indexes",
        "conversation_id",
        "input_guardrail_results",
        "tool_use_tracker",
    ],
)
def test_historical_state_comparison_covers_every_durable_field(field_name: str) -> None:
    historical = {"$schemaVersion": "1.0", field_name: {"value": "preserve-me"}}
    canonical = {"$schemaVersion": "1.15"}

    errors = _find_subset_errors(
        _normalized_durable_state(historical),
        _normalized_durable_state(canonical),
    )

    assert errors == [f"state.{field_name} was dropped"]


def test_historical_state_comparison_preserves_json_scalar_types() -> None:
    errors = _find_subset_errors(
        {"no_active_agent_run": True, "current_turn": 1},
        {"no_active_agent_run": 1, "current_turn": 1.0},
    )

    assert errors == [
        "state.no_active_agent_run changed type from bool to int",
        "state.current_turn changed type from int to float",
    ]


def test_historical_fixture_corpus_matches_supported_schema_versions() -> None:
    assert SOURCES["baseline"] == "v0.19.4"
    assert frozenset(SOURCES["versions"]) == SUPPORTED_SCHEMA_VERSIONS
    assert all(entry["commit"] for entry in SOURCES["versions"].values())
    assert {entry["version"] for entry in SOURCES["features"]} == {
        version for version in SUPPORTED_SCHEMA_VERSIONS if version not in {"1.0", "1.1"}
    }
    assert {entry["provenance"] for entry in SOURCES["features"]} == {
        "historical_writer",
        "canonical_compatibility",
    }


@pytest.mark.parametrize(
    ("schema_version", "entry"),
    sorted(SOURCES["versions"].items()),
)
async def test_historical_minimal_run_state_rewrites_idempotently(
    schema_version: str, entry: dict[str, str]
) -> None:
    fixture = FIXTURE_ROOT / entry["fixture"]
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    assert payload["$schemaVersion"] == schema_version
    assert await validate_historical_run_state_fixture(fixture) == []


@pytest.mark.parametrize("entry", SOURCES["features"], ids=lambda entry: entry["feature"])
async def test_historical_feature_run_state_rewrites_semantically(entry: dict[str, str]) -> None:
    fixture = FIXTURE_ROOT / entry["fixture"]
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    assert payload["$schemaVersion"] == entry["version"]
    assert await validate_historical_run_state_fixture(fixture) == []


@pytest.mark.parametrize(
    ("feature", "decision"),
    [
        ("pending_tool_approval", "approve"),
        ("pending_tool_approval", "reject"),
        ("canonical_invocation_identity", None),
    ],
)
async def test_historical_approval_decisions_control_resumed_runs(
    feature: str,
    decision: str | None,
) -> None:
    entry = (
        SOURCES["resume"]
        if feature == "pending_tool_approval"
        else next(entry for entry in SOURCES["features"] if entry["feature"] == feature)
    )
    fixture = FIXTURE_ROOT / entry["fixture"]

    assert (
        await validate_historical_resume_behavior(
            fixture,
            feature=feature,
            decision=decision,
        )
        == []
    )


async def test_historical_fixture_comparison_uses_immutable_expected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FIXTURE_ROOT / "features" / "v1_8_prompt_cache_key.json"
    original_from_json = RunState.from_json

    async def mutating_from_json(
        initial_agent: Agent[Any],
        state_json: dict[str, Any],
    ) -> Any:
        state_json.pop("generated_prompt_cache_key", None)
        return await original_from_json(initial_agent, state_json)

    monkeypatch.setattr(RunState, "from_json", staticmethod(mutating_from_json))

    errors = await validate_historical_run_state_fixture(fixture)

    assert any("state.generated_prompt_cache_key" in error for error in errors)


async def test_historical_fixture_idempotence_uses_immutable_expected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = FIXTURE_ROOT / "features" / "v1_8_prompt_cache_key.json"
    original_from_json = RunState.from_json
    call_count = 0

    async def mutating_second_read(
        initial_agent: Agent[Any],
        state_json: dict[str, Any],
    ) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            state_json["current_turn"] = 42
        return await original_from_json(initial_agent, state_json)

    monkeypatch.setattr(RunState, "from_json", staticmethod(mutating_second_read))

    errors = await validate_historical_run_state_fixture(fixture)

    assert any("was not idempotent" in error for error in errors)


async def test_credential_fixture_idempotence_uses_immutable_expected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = SOURCES["security"]
    fixture = FIXTURE_ROOT / entry["fixture"]
    original_from_json = RunState.from_json
    call_count = 0

    async def mutating_second_read(
        initial_agent: Agent[Any],
        state_json: dict[str, Any],
    ) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            state_json["current_turn"] = 42
        return await original_from_json(initial_agent, state_json)

    monkeypatch.setattr(RunState, "from_json", staticmethod(mutating_second_read))

    errors = await validate_legacy_credential_run_state_fixture(
        fixture,
        sentinels=entry["sentinels"],
    )

    assert any("was not idempotent" in error for error in errors)


async def test_credential_fixture_comparison_uses_immutable_expected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = SOURCES["security"]
    fixture = FIXTURE_ROOT / entry["fixture"]
    original_from_json = RunState.from_json
    call_count = 0

    async def mutating_first_read(
        initial_agent: Agent[Any],
        state_json: dict[str, Any],
    ) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            state_json["current_turn"] = 42
        return await original_from_json(initial_agent, state_json)

    monkeypatch.setattr(RunState, "from_json", staticmethod(mutating_first_read))

    errors = await validate_legacy_credential_run_state_fixture(
        fixture,
        sentinels=entry["sentinels"],
    )

    assert any("state.current_turn changed from 0 to 42" in error for error in errors)


@pytest.mark.parametrize(
    ("agent_id", "expected_path"),
    [
        (None, "sandbox.session_state"),
        ("compat-agent", "sandbox.sessions_by_agent.compat-agent.session_state"),
    ],
)
async def test_credential_fixture_requires_opaque_driver_options_to_be_removed(
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str | None,
    expected_path: str,
) -> None:
    entry = SOURCES["security"]
    fixture = FIXTURE_ROOT / entry["fixture"]
    original_to_json = RunState.to_json

    def retaining_to_json(state: RunState[Any, Any]) -> dict[str, Any]:
        payload = original_to_json(state)
        sandbox = payload["sandbox"]
        session_state = (
            sandbox["session_state"]
            if agent_id is None
            else sandbox["sessions_by_agent"][agent_id]["session_state"]
        )
        strategy = session_state["manifest"]["entries"]["remote"]["mount_strategy"]
        strategy["driver_options"] = {"vfs-cache-mode": "off"}
        return payload

    monkeypatch.setattr(RunState, "to_json", retaining_to_json)

    errors = await validate_legacy_credential_run_state_fixture(
        fixture,
        sentinels=entry["sentinels"],
    )

    assert errors == [
        f"{fixture.name}: {expected_path}.manifest.entries.remote."
        "mount_strategy.driver_options remained"
    ]


async def test_v0194_mount_credentials_are_sanitized_and_require_trusted_rebind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = SOURCES["security"]
    fixture = FIXTURE_ROOT / entry["fixture"]
    sentinels = entry["sentinels"]
    fixture_text = fixture.read_text(encoding="utf-8")

    assert entry["provenance"] == "historical_writer"
    assert entry["version"] == "1.13"
    assert all(sentinel in fixture_text for sentinel in sentinels)
    with caplog.at_level(logging.DEBUG):
        assert (
            await validate_legacy_credential_run_state_fixture(
                fixture,
                sentinels=sentinels,
            )
            == []
        )

        payload = json.loads(fixture_text)
        restored = await RunState.from_json(Agent(name="compat-agent"), payload)
        canonical = restored.to_json()
        sandbox = canonical["sandbox"]
        session_payload = sandbox["session_state"]
        session_state = _deserialize_common_sandbox_session_state(session_payload)
        assert session_state.mount_authority_redacted is True
        with pytest.raises(ValueError, match="requires a current trusted manifest") as exc_info:
            session_state.rebind_persisted_mount_authority(
                None,
                provider_backend_id="unix_local",
            )

    observables = json.dumps(canonical, sort_keys=True) + repr(restored._sandbox)
    observables += _redaction_observables(exc_info.value, caplog.records)
    assert all(sentinel not in observables for sentinel in sentinels)


@pytest.mark.parametrize(
    ("fixture_name", "message"),
    [
        ("missing_version.json", "missing schema version"),
        ("future_version.json", "schema version is not supported"),
        ("malformed_current_agent.json", "Run state agent not found in agent map"),
    ],
)
async def test_invalid_run_state_fixtures_fail_without_disclosing_values(
    fixture_name: str,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = FIXTURE_ROOT / "negative" / fixture_name
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_SECRET_SENTINEL_42"
    assert sentinel in json.dumps(payload)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception, match=message) as exc_info:
            await RunState.from_json(Agent(name="compat-agent"), payload)

    observables = _redaction_observables(exc_info.value, caplog.records)
    assert sentinel not in observables


async def test_run_state_cancellation_releases_payload_from_sdk_tracebacks() -> None:
    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_CANCEL_SENTINEL_42"
    payload["original_input"] = sentinel
    started = asyncio.Event()
    gate = asyncio.Event()

    async def wait_for_cancellation(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        started.set()
        await gate.wait()
        return []

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(wait_for_cancellation, agent)  # type: ignore[method-assign]
    task = asyncio.create_task(RunState.from_json(agent, payload))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_cleanup_survives_exception_rejecting_redaction_marker() -> None:
    class MarkerRejectingError(Exception):
        def __setattr__(self, name: str, value: object) -> None:
            if name == "_agents_data_redacted":
                raise RuntimeError("marker rejected")
            super().__setattr__(name, value)

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_MARKER_SENTINEL_42"
    payload["original_input"] = sentinel

    async def raise_hostile_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise MarkerRejectingError("safe restoration failure")

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_hostile_error, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(agent, payload)

    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_rejects_foreign_json_mappings_without_invoking_them() -> None:
    sentinel = "RUNSTATE_FOREIGN_JSON_MAPPING_SENTINEL_42"
    calls: list[str] = []

    class ForeignMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            calls.append(f"getitem:{key}")
            raise ValueError("RunState sandbox resume state has an invalid envelope")

        def __iter__(self) -> Iterator[str]:
            calls.append("iter")
            raise ValueError(sentinel)

        def __len__(self) -> int:
            calls.append("len")
            raise ValueError(sentinel)

    payload = json.loads((FIXTURE_ROOT / "minimal" / "v1_15.json").read_text())
    payload["sandbox"] = ForeignMapping()

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(Agent(name="compat-agent"), payload)

    assert calls == []
    assert sentinel not in _redaction_observables(exc_info.value, [])


@pytest.mark.parametrize("entry_point", ["context_override", "context_deserializer"])
async def test_run_state_rejects_context_wrapper_subclasses_before_restoration(
    entry_point: str,
) -> None:
    callbacks: list[str] = []

    class ForeignContextWrapper(RunContextWrapper[object]):
        def _rebuild_approvals(self, approvals: Any) -> None:
            _ = approvals
            callbacks.append("rebuild_approvals")
            raise AssertionError("Foreign restoration override executed")

    class ToolInputDescriptor:
        def __get__(self, instance: object, owner: type[object]) -> object:
            _ = (instance, owner)
            callbacks.append("get_tool_input")
            raise AssertionError("Foreign tool_input descriptor executed")

        def __set__(self, instance: object, value: object) -> None:
            _ = (instance, value)
            callbacks.append("set_tool_input")
            raise AssertionError("Foreign tool_input descriptor executed")

    context = ForeignContextWrapper(context={"custom": True})
    type.__setattr__(ForeignContextWrapper, "tool_input", ToolInputDescriptor())
    payload = json.loads(
        (FIXTURE_ROOT / "features" / "v1_15_canonical_invocation_identity.json").read_text()
    )
    payload["context"]["tool_input"] = {"durable": "expected"}
    kwargs: dict[str, object]
    if entry_point == "context_override":
        kwargs = {"context_override": context}
    else:
        kwargs = {"context_deserializer": lambda _payload: context}

    with pytest.raises(
        UserError,
        match="RunState restoration does not support RunContextWrapper subclasses",
    ) as exc_info:
        await RunState.from_json(Agent(name="compat-agent"), payload, **kwargs)  # type: ignore[arg-type]

    assert str(exc_info.value).endswith(
        "provide the custom context value directly or wrap it in RunContextWrapper."
    )
    assert callbacks == []
    assert context.usage.requests == 0
    assert context._approvals == {}


async def test_run_state_restores_an_exact_context_wrapper_without_replacing_it() -> None:
    context = RunContextWrapper(context={"custom": True})
    payload = json.loads(
        (FIXTURE_ROOT / "features" / "v1_15_canonical_invocation_identity.json").read_text()
    )
    payload["context"]["tool_input"] = {"durable": "expected"}

    restored = await RunState.from_json(
        Agent(name="compat-agent"),
        payload,
        context_deserializer=lambda _payload: context,
    )

    assert restored._context is context
    assert context.context == {"custom": True}
    assert context.tool_input == {"durable": "expected"}
    assert context._tool_invocations


@pytest.mark.parametrize("operation", ["from_json", "from_string"])
async def test_run_state_direct_base_exception_is_value_free(operation: str) -> None:
    class ProviderAbort(BaseException):
        pass

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = f"RUNSTATE_{operation.upper()}_BASE_EXCEPTION_SENTINEL_42"
    payload["original_input"] = sentinel
    source_error = ProviderAbort(sentinel)

    async def raise_source_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise source_error

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_source_error, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        if operation == "from_json":
            await RunState.from_json(agent, payload)
        else:
            await RunState.from_string(agent, json.dumps(payload))

    assert source_error.args == ()
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


def _system_exit_with_effective_code(
    argument_code: bool,
    effective_code: bool,
) -> SystemExit:
    error = SystemExit(argument_code)
    error.code = effective_code
    return error


@pytest.mark.parametrize(
    ("source_error", "expected_type", "expected_args"),
    [
        (SystemExit(7), SystemExit, (7,)),
        (SystemExit("sensitive exit detail"), SystemExit, (1,)),
        (SystemExit(False), SystemExit, (False,)),
        (SystemExit(True), SystemExit, (True,)),
        (_system_exit_with_effective_code(False, True), SystemExit, (True,)),
        (_system_exit_with_effective_code(True, False), SystemExit, (False,)),
        (SystemExit(), SystemExit, ()),
        (KeyboardInterrupt("sensitive interrupt detail"), KeyboardInterrupt, ()),
        (type("ProviderSystemExit", (SystemExit,), {})("sensitive exit detail"), SystemExit, (1,)),
        (type("ProviderFalseSystemExit", (SystemExit,), {})(False), SystemExit, (1,)),
        (type("ProviderTrueSystemExit", (SystemExit,), {})(True), SystemExit, (1,)),
        (
            type("ProviderKeyboardInterrupt", (KeyboardInterrupt,), {})(
                "sensitive interrupt detail"
            ),
            KeyboardInterrupt,
            (),
        ),
    ],
    ids=[
        "system-exit-integer",
        "system-exit-string",
        "system-exit-false",
        "system-exit-true",
        "system-exit-effective-true",
        "system-exit-effective-false",
        "system-exit-none",
        "keyboard-interrupt",
        "system-exit-subclass",
        "system-exit-false-subclass",
        "system-exit-true-subclass",
        "keyboard-interrupt-subclass",
    ],
)
async def test_run_state_preserves_value_free_process_control_exceptions(
    source_error: BaseException,
    expected_type: type[BaseException],
    expected_args: tuple[object, ...],
) -> None:
    payload = json.loads((FIXTURE_ROOT / "minimal" / "v1_15.json").read_text())
    payload["context"]["context_meta"] = {
        "omitted": False,
        "original_type": "custom.Context",
        "requires_deserializer": True,
        "serialized_via": "custom",
    }

    def deserialize_context(_payload: Mapping[str, Any]) -> object:
        raise source_error

    with pytest.raises(expected_type) as exc_info:
        await RunState.from_json(
            Agent(name="compat-agent"),
            payload,
            context_deserializer=deserialize_context,
        )

    assert type(exc_info.value) is expected_type
    assert exc_info.value.args == expected_args
    if expected_args:
        assert type(exc_info.value.args[0]) is type(expected_args[0])
    assert exc_info.value is not source_error
    assert source_error.args == ()
    assert source_error.__traceback__ is None
    if type(source_error) is SystemExit:
        assert source_error.code is None


async def test_run_state_rejects_a_foreign_error_with_a_copied_trusted_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "RUNSTATE_COPIED_TRUSTED_TRACEBACK_SECRET_42"
    payload = json.loads((FIXTURE_ROOT / "minimal" / "v1_15.json").read_text())
    invalid_payload = dict(payload, pending_input=sentinel)
    with pytest.raises(UserError) as trusted_exc_info:
        await run_state_module._build_run_state_from_json(
            Agent(name="compat-agent"), invalid_payload
        )

    source_error = UserError("Run state pending_input must be a list").with_traceback(
        trusted_exc_info.value.__traceback__
    )
    source_error.payload = sentinel  # type: ignore[attr-defined]

    def raise_foreign_error(_agent: Agent[Any]) -> dict[str, Agent[Any]]:
        raise source_error

    monkeypatch.setattr(run_state_module, "_build_agent_map", raise_foreign_error)

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(Agent(name="compat-agent"), payload)

    assert source_error.args == ()
    assert source_error.__dict__ == {}
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_rejects_a_foreign_error_with_a_forged_validation_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "RUNSTATE_FORGED_VALIDATION_MARKER_SECRET_42"
    message = "Run state pending_input must be a list"
    payload = json.loads((FIXTURE_ROOT / "minimal" / "v1_15.json").read_text())
    source_error = UserError(message)
    source_error.payload = sentinel  # type: ignore[attr-defined]
    source_error._agents_run_state_validation = (object(), message)  # type: ignore[attr-defined]

    def raise_foreign_error(_agent: Agent[Any]) -> dict[str, Agent[Any]]:
        raise source_error

    monkeypatch.setattr(run_state_module, "_build_agent_map", raise_foreign_error)

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(Agent(name="compat-agent"), payload)

    assert source_error.args == ()
    assert source_error.__dict__ == {}
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_rejects_a_mutated_trusted_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "RUNSTATE_MUTATED_TRUSTED_DIAGNOSTIC_SECRET_42"
    payload = json.loads((FIXTURE_ROOT / "minimal" / "v1_15.json").read_text())
    source_error: UserError | None = None

    def mutate_trusted_error(_agent: Agent[Any]) -> dict[str, Agent[Any]]:
        nonlocal source_error
        try:
            run_state_module._validate_run_state_schema_version({})
        except UserError as error:
            source_error = error
            error.args = (sentinel,)
            raise
        raise AssertionError("trusted error producer unexpectedly returned")

    agent = Agent(name="compat-agent")
    monkeypatch.setattr(run_state_module, "_build_agent_map", mutate_trusted_error)

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(agent, payload)

    assert source_error is not None
    assert source_error.args == ()
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


@pytest.mark.parametrize("operation", ["from_json", "from_string"])
async def test_run_state_rejects_a_diagnostic_from_the_wrong_trusted_producer(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    sentinel = "RUNSTATE_WRONG_TRUSTED_PRODUCER_SECRET_42"
    payload = json.loads((FIXTURE_ROOT / "minimal" / "v1_15.json").read_text())
    payload["original_input"] = sentinel
    source_error: UserError | None = None

    def substitute_trusted_diagnostic(_agent: Agent[Any]) -> dict[str, Agent[Any]]:
        nonlocal source_error
        try:
            run_state_module._validate_run_state_schema_version({})
        except UserError as error:
            source_error = error
            error.args = ("Run state pending_input must be a list",)
            raise
        raise AssertionError("trusted error producer unexpectedly returned")

    agent = Agent(name="compat-agent")
    monkeypatch.setattr(run_state_module, "_build_agent_map", substitute_trusted_diagnostic)

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        if operation == "from_json":
            await RunState.from_json(agent, payload)
        else:
            await RunState.from_string(agent, json.dumps(payload))

    assert source_error is not None
    assert source_error.args == ()
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


@pytest.mark.parametrize("operation", ["from_json", "from_string"])
@pytest.mark.parametrize(
    "carrier",
    ["agent_name", "agent_identity", "context_type", "completed_call_id"],
)
async def test_run_state_payload_derived_restoration_errors_are_value_free(
    operation: str,
    carrier: str,
) -> None:
    sentinel = f"RUNSTATE_{carrier.upper()}_SECRET_42"
    fixture_name = (
        "features/v1_15_canonical_invocation_identity.json"
        if carrier == "completed_call_id"
        else "minimal/v1_15.json"
    )
    payload = json.loads((FIXTURE_ROOT / fixture_name).read_text(encoding="utf-8"))
    strict_context = False
    if carrier == "agent_name":
        payload["current_agent"] = {"name": sentinel}
    elif carrier == "agent_identity":
        payload["current_agent"] = {"name": "compat-agent", "identity": sentinel}
    elif carrier == "context_type":
        payload["context"]["context_meta"] = {
            "original_type": sentinel,
            "requires_deserializer": True,
        }
        strict_context = True
    else:
        invocation = next(iter(payload["context"]["tool_invocations"].values()))
        invocation["completed"] = True
        payload["context"]["tool_invocations"] = {sentinel: invocation}

    agent = Agent(name="compat-agent")
    expected_message = {
        "agent_name": "Run state agent not found in agent map",
        "agent_identity": "agent identity",
        "context_type": "requires explicit restoration",
        "completed_call_id": "invalid lifecycle data",
    }[carrier]
    with pytest.raises(UserError, match=expected_message) as exc_info:
        if operation == "from_json":
            await RunState.from_json(agent, payload, strict_context=strict_context)
        else:
            await RunState.from_string(
                agent,
                json.dumps(payload),
                strict_context=strict_context,
            )

    assert type(exc_info.value) is UserError
    assert sentinel not in _redaction_observables(exc_info.value, [])


@pytest.mark.parametrize("operation", ["from_json", "from_string"])
async def test_sdk_owned_run_state_validation_preserves_sanitized_user_error(
    operation: str,
) -> None:
    sentinel = "RUNSTATE_INVALID_PENDING_INPUT_SECRET_42"
    payload = json.loads((FIXTURE_ROOT / "minimal" / "v1_15.json").read_text())
    payload["pending_input"] = sentinel
    agent = Agent(name="compat-agent")

    with pytest.raises(UserError, match="Run state pending_input must be a list") as exc_info:
        if operation == "from_json":
            await RunState.from_json(agent, payload)
        else:
            await RunState.from_string(agent, json.dumps(payload))

    assert type(exc_info.value) is UserError
    assert str(exc_info.value) == "Run state pending_input must be a list"
    assert sentinel not in _redaction_observables(exc_info.value, [])


@pytest.mark.parametrize("carrier", ["message", "attribute", "args", "notes", "group"])
async def test_run_state_failure_discards_exception_owned_payload(carrier: str) -> None:
    class PayloadError(Exception):
        pass

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = f"RUNSTATE_{carrier.upper()}_SENTINEL_42"
    payload["original_input"] = sentinel

    async def raise_payload_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        if carrier == "message":
            raise RuntimeError(sentinel)
        if carrier == "attribute":
            error = PayloadError("safe restoration failure")
            error.payload = payload  # type: ignore[attr-defined]
            raise error
        if carrier == "args":
            raise ValueError("safe restoration failure", payload)
        if carrier == "notes":
            note_error = RuntimeError("safe restoration failure")
            note_error.__notes__ = [sentinel]
            raise note_error
        nested = RuntimeError("safe nested failure")
        nested.payload = payload  # type: ignore[attr-defined]
        raise BaseExceptionGroup("safe restoration group", [nested])

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_payload_error, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError) as exc_info:
        await RunState.from_json(agent, payload)

    assert str(exc_info.value) == "Error details are redacted."
    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_failure_discards_retained_source_exception() -> None:
    class PayloadError(Exception):
        pass

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_RETAINED_SOURCE_SENTINEL_42"
    payload["original_input"] = sentinel
    source_error = PayloadError("safe restoration failure")
    source_error.payload = payload  # type: ignore[attr-defined]

    async def raise_source_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise source_error

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_source_error, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted"):
        await RunState.from_json(agent, payload)

    assert source_error.args == ()
    assert source_error.__dict__ == {}
    assert source_error.__traceback__ is None


async def test_run_state_failure_discards_retained_nested_exception() -> None:
    class PayloadError(Exception):
        pass

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_RETAINED_NESTED_EXCEPTION_SENTINEL_42"
    child = PayloadError(sentinel)
    child.payload = {"credential": sentinel}  # type: ignore[attr-defined]
    source_error = RuntimeError("safe restoration failure", {"children": [child]})
    source_error.payload = (child,)  # type: ignore[attr-defined]

    async def raise_source_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise source_error

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_source_error, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted"):
        await RunState.from_json(agent, payload)

    assert source_error.args == ()
    assert source_error.__dict__ == {}
    assert source_error.__traceback__ is None
    assert child.args == ()
    assert child.__dict__ == {}
    assert child.__traceback__ is None


async def test_run_state_failure_discards_retained_exception_group_children() -> None:
    class SlottedPayloadError(Exception):
        payload: object
        __slots__ = ("payload",)

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_RETAINED_GROUP_SENTINEL_42"
    payload["original_input"] = sentinel
    child = SlottedPayloadError("safe child")
    child.payload = payload
    source_group = BaseExceptionGroup(sentinel, [child])

    async def raise_group(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise source_group

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_group, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(agent, payload)

    assert child.args == ()
    with pytest.raises(AttributeError):
        _ = child.payload
    assert source_group.args[0] == "Error details are redacted."
    assert source_group.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_failure_discards_slots_without_metaclass_callbacks() -> None:
    descriptor_calls: list[str] = []

    class HostileMroDescriptor:
        def __get__(self, obj: object, owner: type | None = None) -> object:
            _ = (obj, owner)
            descriptor_calls.append("get")
            raise AssertionError("Metaclass descriptor executed")

        def __set__(self, obj: object, value: object) -> None:
            _ = (obj, value)
            descriptor_calls.append("set")
            raise AssertionError("Metaclass descriptor executed")

    class HostileExceptionMeta(type):
        __mro__ = cast(Any, HostileMroDescriptor())

    class SlottedPayloadError(Exception, metaclass=HostileExceptionMeta):
        payload: object
        __slots__ = ("payload",)

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_HOSTILE_METACLASS_SLOT_SENTINEL_42"
    payload["original_input"] = sentinel
    source_error = SlottedPayloadError("safe restoration failure")
    source_error.payload = payload

    async def raise_source_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise source_error

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_source_error, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(agent, payload)

    assert descriptor_calls == []
    assert source_error.args == ()
    with pytest.raises(AttributeError):
        _ = source_error.payload
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_hidden_overwritten_slot_remains_caller_owned_without_callbacks() -> None:
    descriptor_calls: list[str] = []

    class HostileDescriptor:
        def __get__(self, obj: object, owner: type | None = None) -> object:
            _ = (obj, owner)
            descriptor_calls.append("get")
            raise AssertionError("Provider descriptor executed")

        def __set__(self, obj: object, value: object) -> None:
            _ = (obj, value)
            descriptor_calls.append("set")
            raise AssertionError("Provider descriptor executed")

        def __delete__(self, obj: object) -> None:
            _ = obj
            descriptor_calls.append("delete")
            raise AssertionError("Provider descriptor executed")

    class SlottedPayloadError(Exception):
        payload: object
        __slots__ = ("payload",)

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_HIDDEN_PROVIDER_SLOT_SENTINEL_42"
    payload["original_input"] = sentinel
    source_error = SlottedPayloadError("safe restoration failure")
    source_error.payload = payload
    original_descriptor = type.__getattribute__(SlottedPayloadError, "__dict__")["payload"]
    type.__setattr__(SlottedPayloadError, "payload", HostileDescriptor())

    async def raise_source_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise source_error

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_source_error, agent)  # type: ignore[method-assign]

    try:
        with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
            await RunState.from_json(agent, payload)

        assert descriptor_calls == []
        assert source_error.args == ()
        assert source_error.__traceback__ is None
        assert sentinel not in _redaction_observables(exc_info.value, [])

        # Python exposes no callback-free way to reach the hidden slot after its
        # defining descriptor is replaced. The provider-owned source shell remains
        # outside the SDK public error boundary and can recover its own storage.
        type.__setattr__(SlottedPayloadError, "payload", original_descriptor)
        assert source_error.payload is payload
    finally:
        type.__setattr__(SlottedPayloadError, "payload", original_descriptor)


async def test_run_state_from_string_discards_exception_owned_payload() -> None:
    class PayloadError(Exception):
        pass

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_FROM_STRING_SENTINEL_42"
    payload["original_input"] = sentinel

    async def raise_payload_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        error = PayloadError("safe restoration failure")
        error.payload = payload  # type: ignore[attr-defined]
        raise error

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_payload_error, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_string(agent, json.dumps(payload))

    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_from_string_malformed_json_discards_caller_owned_state() -> None:
    sentinel = "RUNSTATE_MALFORMED_JSON_CALLER_SENTINEL_42"
    agent = Agent(name=sentinel)

    def deserialize_context(value: object) -> object:
        _ = value
        return {"credential": sentinel}

    with pytest.raises(UserError, match="Failed to parse run state JSON") as exc_info:
        await RunState.from_string(
            agent,
            "{",
            context_override={"credential": sentinel},
            context_deserializer=deserialize_context,
        )

    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_from_string_deep_json_failure_discards_parser_state() -> None:
    sentinel = "RUNSTATE_DEEP_JSON_PARSER_SENTINEL_42"
    state_string = "[" * 100_000 + json.dumps(sentinel) + "]" * 100_000

    with pytest.raises(
        UserError,
        match="Failed to parse run state JSON|Run state JSON must be an object",
    ) as exc_info:
        await RunState.from_string(Agent(name="compat-agent"), state_string)

    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_from_string_non_decode_parser_failure_discards_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "RUNSTATE_NON_DECODE_PARSER_SENTINEL_42"
    source_error = RecursionError("parser nesting limit")

    def fail_to_parse(state_string: str) -> object:
        retained_input = state_string
        if retained_input:
            raise source_error
        return {}

    monkeypatch.setattr(json, "loads", fail_to_parse)

    with pytest.raises(UserError, match="Failed to parse run state JSON") as exc_info:
        await RunState.from_string(Agent(name="compat-agent"), sentinel)

    assert source_error.args == ()
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_from_string_discards_hostile_json_decode_error_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "RUNSTATE_HOSTILE_JSON_DECODE_DESCRIPTOR_SECRET_42"

    class HostileJSONDecodeError(json.JSONDecodeError):
        def __getattribute__(self, name: str) -> object:
            if name in {"lineno", "colno", "pos"}:
                raise RuntimeError(sentinel)
            return super().__getattribute__(name)

    source_error = HostileJSONDecodeError("invalid JSON", sentinel, 0)

    def fail_to_parse(_state_string: str) -> object:
        raise source_error

    monkeypatch.setattr(json, "loads", fail_to_parse)

    with pytest.raises(UserError, match="Failed to parse run state JSON") as exc_info:
        await RunState.from_string(Agent(name="compat-agent"), sentinel)

    assert source_error.args == ()
    assert source_error.__dict__ == {}
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


@pytest.mark.parametrize(
    ("source_error", "expected_type", "expected_args"),
    [
        (SystemExit("sensitive parser exit"), SystemExit, (1,)),
        (KeyboardInterrupt("sensitive parser interrupt"), KeyboardInterrupt, ()),
    ],
)
async def test_run_state_from_string_parser_preserves_value_free_process_control(
    monkeypatch: pytest.MonkeyPatch,
    source_error: BaseException,
    expected_type: type[BaseException],
    expected_args: tuple[object, ...],
) -> None:
    def fail_to_parse(_state_string: str) -> object:
        raise source_error

    monkeypatch.setattr(json, "loads", fail_to_parse)

    with pytest.raises(expected_type) as exc_info:
        await RunState.from_string(Agent(name="compat-agent"), "{}")

    assert type(exc_info.value) is expected_type
    assert exc_info.value.args == expected_args
    assert exc_info.value is not source_error
    assert source_error.args == ()
    assert source_error.__traceback__ is None


@pytest.mark.parametrize("operation", ["from_json", "from_string"])
async def test_run_state_discards_base_exception_group_with_cancellation(
    operation: str,
) -> None:
    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_BASE_GROUP_SENTINEL_42"
    payload["original_input"] = sentinel

    async def raise_group(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        child = RuntimeError("safe child")
        child.payload = payload  # type: ignore[attr-defined]
        raise BaseExceptionGroup(
            "safe restoration group",
            [asyncio.CancelledError(), child],
        )

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_group, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        if operation == "from_json":
            await RunState.from_json(agent, payload)
        else:
            await RunState.from_string(agent, json.dumps(payload))

    assert sentinel not in _redaction_observables(exc_info.value, [])


@pytest.mark.parametrize("hostile_field", ["args", "traceback"])
async def test_run_state_cleanup_handles_hostile_exception_descriptors(
    hostile_field: str,
) -> None:
    class HostileError(Exception):
        if hostile_field == "args":

            @property
            def args(self) -> tuple[object, ...]:  # type: ignore[override]
                raise RuntimeError("hostile descriptor failure")

        if hostile_field == "traceback":

            @property
            def __traceback__(self) -> object:  # type: ignore[override]
                raise RuntimeError("hostile descriptor failure")

    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = f"RUNSTATE_HOSTILE_{hostile_field.upper()}_SENTINEL_42"
    payload["original_input"] = sentinel

    async def raise_hostile(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise HostileError(sentinel)

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_hostile, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(agent, payload)

    assert exc_info.value.__context__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


@pytest.mark.parametrize("module_name", ["agents.spoofed_external_hook", object()])
async def test_run_state_does_not_trust_mutable_frame_module_metadata(
    module_name: object,
) -> None:
    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_SPOOFED_MODULE_SENTINEL_42"
    payload["original_input"] = sentinel
    scope = {
        "__name__": module_name,
        "RuntimeError": RuntimeError,
        "sentinel": sentinel,
    }
    exec("async def fail(self, context):\n    raise RuntimeError(sentinel)", scope)
    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(cast(Any, scope["fail"]), agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(agent, payload)

    assert exc_info.value.__context__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_does_not_trust_foreign_code_with_sdk_globals_and_filename() -> None:
    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_FOREIGN_CODE_SENTINEL_42"
    try:
        exec(
            compile(
                f"raise UserError({sentinel!r})",
                run_state_module.__file__,
                "exec",
            ),
            run_state_module.__dict__,
        )
    except UserError as error:
        source_error = error

    async def raise_source_error(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise source_error

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_source_error, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(agent, payload)

    assert source_error.args == ()
    assert source_error.__traceback__ is None
    assert sentinel not in _redaction_observables(exc_info.value, [])


async def test_run_state_does_not_trust_cancellation_name_or_module() -> None:
    fake_cancel_type = type(
        "CancelledError",
        (Exception,),
        {"__module__": "asyncio.exceptions"},
    )
    fixture = FIXTURE_ROOT / "resume" / "v1_13_pending_tool_approval.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    sentinel = "RUNSTATE_FAKE_CANCEL_SENTINEL_42"
    payload["original_input"] = sentinel

    async def raise_fake_cancel(self: Agent[Any], context: object) -> list[object]:
        _ = (self, context)
        raise fake_cancel_type(sentinel)

    agent = Agent(name="compat-agent")
    agent.get_all_tools = types.MethodType(raise_fake_cancel, agent)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Error details are redacted") as exc_info:
        await RunState.from_json(agent, payload)

    assert type(exc_info.value) is RuntimeError
    assert sentinel not in _redaction_observables(exc_info.value, [])
