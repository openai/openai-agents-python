import json
from pathlib import Path

import pytest

from agents import Agent, RunState
from agents.run_state import SUPPORTED_SCHEMA_VERSIONS
from integration_tests._contract_support import validate_historical_run_state_fixture

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
    ("fixture_name", "message"),
    [
        ("missing_version.json", "missing schema version"),
        ("future_version.json", "schema version 999 is not supported"),
        ("malformed_current_agent.json", "string indices"),
    ],
)
async def test_installed_distribution_rejects_invalid_run_state_without_disclosure(
    fixture_name: str,
    message: str,
) -> None:
    fixture = FIXTURE_ROOT / "negative" / fixture_name
    payload = json.loads(fixture.read_text(encoding="utf-8"))

    with pytest.raises(Exception, match=message) as exc_info:
        await RunState.from_json(Agent(name="compat-agent"), payload)

    assert "RUNSTATE_SECRET_SENTINEL_42" not in repr(exc_info.value)
