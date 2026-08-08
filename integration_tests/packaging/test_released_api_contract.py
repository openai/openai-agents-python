from importlib.metadata import version
from pathlib import Path

import pytest

from integration_tests._contract_support import (
    load_api_contract,
    validate_released_api_contract,
)

pytestmark = pytest.mark.packaging

CONTRACT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "released_api_contract.json"


def test_installed_distribution_preserves_released_public_api_contract() -> None:
    contract = load_api_contract(CONTRACT)
    assert contract["baseline"] == f"v{version('openai-agents')}"
    assert len(contract["baseline_commit"]) == 40

    errors = validate_released_api_contract(contract)

    assert errors == []
