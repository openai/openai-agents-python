import json
import logging
from pathlib import Path

import pytest

from agents import Agent, RunState
from agents.run_state import SUPPORTED_SCHEMA_VERSIONS
from integration_tests._contract_support import (
    _deserialize_common_sandbox_session_state,
    _redaction_observables,
    validate_historical_resume_behavior,
    validate_historical_run_state_fixture,
    validate_legacy_credential_run_state_fixture,
)

pytestmark = pytest.mark.packaging

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "run_state"
SOURCES = json.loads((FIXTURE_ROOT / "sources.json").read_text(encoding="utf-8"))


def test_installed_distribution_supports_the_historical_fixture_corpus() -> None:
    assert frozenset(SOURCES["versions"]) == SUPPORTED_SCHEMA_VERSIONS


@pytest.mark.parametrize(
    ("schema_version", "entry"),
    sorted(SOURCES["versions"].items()),
)
async def test_installed_distribution_rewrites_historical_run_state(
    schema_version: str, entry: dict[str, str]
) -> None:
    fixture = FIXTURE_ROOT / entry["fixture"]
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    assert payload["$schemaVersion"] == schema_version
    assert await validate_historical_run_state_fixture(fixture) == []


@pytest.mark.parametrize("entry", SOURCES["features"], ids=lambda entry: entry["feature"])
async def test_installed_distribution_rewrites_historical_features_semantically(
    entry: dict[str, str],
) -> None:
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
async def test_installed_distribution_resumes_historical_approval_decisions(
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


async def test_installed_distribution_sanitizes_v0194_mount_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = SOURCES["security"]
    fixture = FIXTURE_ROOT / entry["fixture"]
    sentinels = entry["sentinels"]
    fixture_text = fixture.read_text(encoding="utf-8")

    assert entry["provenance"] == "historical_writer"
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
        session_payload = canonical["sandbox"]["session_state"]
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
async def test_installed_distribution_rejects_invalid_run_state_without_disclosure(
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
