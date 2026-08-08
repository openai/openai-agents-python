from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from integration_tests._contract_support import (
    _parameter_contract,
    _validate_parameter_contract,
    load_api_contract,
    validate_released_api_contract,
)

CONTRACT = Path(__file__).parent / "fixtures" / "released_api_contract.json"


def test_current_source_preserves_v0194_public_api_contract() -> None:
    contract = load_api_contract(CONTRACT)
    assert contract["baseline"] == "v0.19.4"
    assert contract["baseline_commit"] == "92aa1b905306d7f5a130d911061c44cddeaa6e20"

    errors = validate_released_api_contract(contract)

    assert errors == []


def test_constructor_contract_allows_optional_suffixes_only() -> None:
    def released(value: str) -> None:
        _ = value

    def compatible(value: str, optional: int = 1, *, named: bool = False) -> None:
        _ = (value, optional, named)

    def compatible_variadic(value: str, *args: object, **kwargs: object) -> None:
        _ = (value, args, kwargs)

    def incompatible(value: str, required: int) -> None:
        _ = (value, required)

    released_contract = _parameter_contract(released)

    assert (
        _validate_parameter_contract("Example", released_contract, _parameter_contract(compatible))
        == []
    )
    assert (
        _validate_parameter_contract(
            "Example", released_contract, _parameter_contract(compatible_variadic)
        )
        == []
    )
    assert _validate_parameter_contract(
        "Example", released_contract, _parameter_contract(incompatible)
    ) == ["Example.required added a required parameter"]


def test_public_api_contract_requires_real_export_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agents

    contract: dict[str, Any] = {
        "required_top_level_exports": ["AgentsException"],
        "public_modules": [],
        "canonical_imports": [],
        "constructors": {},
    }
    monkeypatch.delattr(agents, "AgentsException")

    assert validate_released_api_contract(contract) == [
        "Missing released top-level bindings: ['AgentsException']"
    ]


def test_public_api_contract_rejects_required_dataclass_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agents

    @dataclass
    class Incompatible:
        value: str
        required_suffix: int

    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": [],
        "canonical_imports": [],
        "constructors": {
            "ContractExample": {
                "parameters": [
                    {
                        "name": "value",
                        "kind": "POSITIONAL_OR_KEYWORD",
                        "default": {"kind": "required"},
                    }
                ],
                "dataclass_fields": [
                    {"name": "value", "init": True, "default": {"kind": "required"}}
                ],
            }
        },
    }
    monkeypatch.setattr(agents, "ContractExample", Incompatible, raising=False)

    assert validate_released_api_contract(contract) == [
        "ContractExample.required_suffix added a required parameter",
        "ContractExample.required_suffix added a required dataclass field",
    ]
