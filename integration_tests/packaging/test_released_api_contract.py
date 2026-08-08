from pathlib import Path

import pytest

from integration_tests._contract_support import (
    load_api_contract,
    validate_released_api_contract,
)

pytestmark = pytest.mark.packaging

CONTRACT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "released_api_contract.json"


def test_installed_distribution_preserves_v0194_public_api_contract() -> None:
    contract = load_api_contract(CONTRACT)
    assert contract["baseline"] == "v0.19.4"
    assert contract["baseline_commit"] == "92aa1b905306d7f5a130d911061c44cddeaa6e20"

    errors = validate_released_api_contract(contract)

    assert errors == []
