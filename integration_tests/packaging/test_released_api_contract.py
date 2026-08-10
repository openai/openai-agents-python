import os
from importlib.metadata import version
from pathlib import Path

import pytest

from integration_tests._contract_support import (
    load_api_contract,
    validate_released_api_contract,
)

pytestmark = pytest.mark.packaging

CONTRACT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "released_api_contract.json"


@pytest.mark.packaging_dependency
def test_installed_distribution_preserves_released_public_api_contract() -> None:
    contract = load_api_contract(CONTRACT)
    assert contract["baseline"] == f"v{version('openai-agents')}"
    assert len(contract["baseline_commit"]) == 40

    errors = validate_released_api_contract(
        contract,
        require_all_optional_exports=(
            os.environ.get("OPENAI_AGENTS_INTEGRATION_REQUIRE_OPTIONAL_EXPORTS") == "1"
        ),
    )

    assert errors == []
