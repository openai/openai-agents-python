from dataclasses import dataclass
from enum import Enum
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from integration_tests._contract_support import (
    _parameter_contract,
    _validate_parameter_contract,
    build_released_api_contract,
    load_api_contract,
    validate_released_api_contract,
)

CONTRACT = Path(__file__).parent / "fixtures" / "released_api_contract.json"


def test_current_source_preserves_released_public_api_contract() -> None:
    contract = load_api_contract(CONTRACT)
    assert contract["baseline"] == f"v{version('openai-agents')}"
    assert len(contract["baseline_commit"]) == 40

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


def test_enum_constructor_contract_uses_member_lookup_signature() -> None:
    class ReleasedEnum(Enum):
        VALUE = "value"

    assert _parameter_contract(ReleasedEnum) == [
        {
            "name": "value",
            "kind": "POSITIONAL_OR_KEYWORD",
            "default": {"kind": "required"},
        }
    ]


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


def test_release_contract_update_freezes_new_exports_and_classes() -> None:
    @dataclass
    class Existing:
        value: str
        optional: int = 1

    @dataclass
    class NewPublic:
        name: str
        enabled: bool = True

    def new_helper() -> None:
        return None

    class Uninspectable:
        __signature__ = "invalid"

    agents_module = SimpleNamespace(
        __all__=["new_helper", "Existing", "NewPublic", "Uninspectable"],
        Existing=Existing,
        new_helper=new_helper,
        NewPublic=NewPublic,
        Uninspectable=Uninspectable,
    )
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": ["Existing"],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "constructors": {},
    }

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
    )

    assert updated["baseline"] == "v0.20.0"
    assert updated["baseline_commit"] == "b" * 40
    assert updated["required_top_level_exports"] == [
        "Existing",
        "new_helper",
        "NewPublic",
        "Uninspectable",
    ]
    assert set(updated["constructors"]) == {"Existing", "NewPublic"}
    assert [field["name"] for field in updated["constructors"]["Existing"]["dataclass_fields"]] == [
        "value",
        "optional",
    ]
    assert [
        field["name"] for field in updated["constructors"]["NewPublic"]["dataclass_fields"]
    ] == [
        "name",
        "enabled",
    ]
    assert updated["public_modules"] == ["agents"]
    assert updated["canonical_imports"] == []

    unchanged = build_released_api_contract(
        updated,
        baseline="v0.20.0",
        baseline_commit="c" * 40,
        agents_module=agents_module,
    )
    assert unchanged["baseline_commit"] == "b" * 40


def test_release_contract_update_rejects_incompatible_current_surface() -> None:
    class Released:
        def __init__(self, value: str) -> None:
            self.value = value

    class Incompatible:
        def __init__(self, renamed: str) -> None:
            self.renamed = renamed

    agents_module = SimpleNamespace(__all__=["Released"], Released=Incompatible)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": ["Released"],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "constructors": {
            "Released": {
                "parameters": _parameter_contract(Released),
                "dataclass_fields": [],
            }
        },
    }

    with pytest.raises(
        ValueError,
        match="Cannot promote an incompatible released API contract",
    ):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
        )


def test_release_contract_update_rejects_class_replaced_by_function() -> None:
    class Released:
        def __init__(self, value: str) -> None:
            self.value = value

    def replacement(value: str) -> None:
        _ = value

    agents_module = SimpleNamespace(__all__=["Released"], Released=replacement)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": ["Released"],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "constructors": {
            "Released": {
                "parameters": _parameter_contract(Released),
                "dataclass_fields": [],
            }
        },
    }

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Released constructor agents.Released is no longer a class"
    ]
    with pytest.raises(
        ValueError,
        match="Released constructor agents.Released is no longer a class",
    ):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
        )


def test_release_contract_update_rejects_duplicate_exports() -> None:
    agents_module = SimpleNamespace(__all__=["Duplicate", "Duplicate"], Duplicate=object())
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": [],
        "canonical_imports": [],
        "constructors": {},
    }

    with pytest.raises(ValueError, match="must not contain duplicate exports"):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
        )
