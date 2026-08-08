import json
from pathlib import Path

import pytest

from agents import Agent, RunState
from agents.run_state import SUPPORTED_SCHEMA_VERSIONS
from integration_tests._contract_support import validate_historical_run_state_fixture

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "run_state"
SOURCES = json.loads((FIXTURE_ROOT / "sources.json").read_text(encoding="utf-8"))


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
    ("fixture_name", "message"),
    [
        ("missing_version.json", "missing schema version"),
        ("future_version.json", "schema version 999 is not supported"),
        ("malformed_current_agent.json", "string indices"),
    ],
)
async def test_invalid_run_state_fixtures_fail_without_disclosing_values(
    fixture_name: str,
    message: str,
) -> None:
    fixture = FIXTURE_ROOT / "negative" / fixture_name
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    with pytest.raises(Exception, match=message) as exc_info:
        await RunState.from_json(Agent(name="compat-agent"), payload)

    observables = "\n".join(
        (
            str(exc_info.value),
            repr(exc_info.value),
            repr(exc_info.value.__cause__),
            repr(exc_info.value.__context__),
        )
    )
    assert "RUNSTATE_SECRET_SENTINEL_42" not in observables
