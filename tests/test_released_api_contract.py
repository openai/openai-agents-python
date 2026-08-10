import sys
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import Field

import integration_tests._contract_support as contract_support
from integration_tests._contract_support import (
    _callable_contract,
    _default_contract,
    _parameter_contract,
    _public_class_member_contract,
    _validate_parameter_contract,
    _validate_public_property_contract,
    build_released_api_contract,
    load_api_contract,
    validate_released_api_contract,
)

CONTRACT = Path(__file__).parent / "fixtures" / "released_api_contract.json"


@pytest.mark.parametrize(
    ("released", "changed"),
    [(False, 0), (1, 1.0)],
)
def test_literal_default_contract_preserves_exact_builtin_type(
    released: object,
    changed: object,
) -> None:
    assert _default_contract(released) != _default_contract(changed)

    def released_callable(value: object = released) -> None:
        _ = value

    def changed_callable(value: object = changed) -> None:
        _ = value

    errors = _validate_parameter_contract(
        "Example",
        _parameter_contract(released_callable),
        _parameter_contract(changed_callable),
    )

    assert len(errors) == 1
    assert "changed its released positional parameter prefix" in errors[0]


@pytest.mark.allow_call_model_methods
def test_current_source_preserves_released_public_api_contract() -> None:
    contract = load_api_contract(CONTRACT)
    assert contract["baseline"] == f"v{version('openai-agents')}"
    assert len(contract["baseline_commit"]) == 40
    if contract["baseline"] == "v0.19.4":
        assert contract["baseline_commit"] == "9bfad15ab8297fbb2afe389c983a5cb573eeef56"
        assert all(
            field["name"] != "preserve_raw_usage"
            for field in contract["callables"]["ModelSettings"]["dataclass_fields"]
        )
        assert set(contract["callables"]["Runner"]["members"]) == {
            "run",
            "run_streamed",
            "run_sync",
        }
        assert {"final_output_as", "release_agents", "to_input_list", "to_state"}.issubset(
            contract["callables"]["RunResult"]["members"]
        )
        assert {"from_json", "to_json"}.issubset(contract["callables"]["RunState"]["members"])
        assert contract["public_properties"] == [
            {
                "module": "agents.result",
                "class_name": "RunResultBase",
                "names": ["agent_tool_invocation", "last_agent", "last_response_id"],
            },
            {
                "module": "agents.result",
                "class_name": "RunResult",
                "names": ["agent_tool_invocation", "last_agent", "last_response_id"],
            },
            {
                "module": "agents.result",
                "class_name": "RunResultStreaming",
                "names": [
                    "agent_tool_invocation",
                    "last_agent",
                    "last_response_id",
                    "run_loop_exception",
                ],
            },
        ]

    errors = validate_released_api_contract(contract)

    assert errors == []


def test_callable_contract_ignores_typing_aliases() -> None:
    alias = Callable[[str], None]
    agents_module = SimpleNamespace(__all__=["Callback"], Callback=alias)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {},
    }

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
    )

    assert updated["required_top_level_exports"] == ["Callback"]
    assert updated["callables"] == {}


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

    def released_variadic(*args: object) -> None:
        _ = args

    def incompatible_before_variadic(optional: int = 1, *args: object) -> None:
        _ = (optional, args)

    assert _validate_parameter_contract(
        "VariadicExample",
        _parameter_contract(released_variadic),
        _parameter_contract(incompatible_before_variadic),
    ) == [
        "VariadicExample added positional parameters before its released variadic parameter: "
        "[{'name': 'optional', 'kind': 'POSITIONAL_OR_KEYWORD', "
        "'default': {'kind': 'literal', 'type': 'builtins.int', 'value': 1}}]"
    ]


def test_public_class_member_contract_tracks_direct_callable_bindings() -> None:
    class Released:
        def instance(self, value: str, optional: int = 1) -> None:
            _ = (value, optional)

        @classmethod
        def class_method(cls, value: str) -> None:
            _ = (cls, value)

        @staticmethod
        def static_method(value: str) -> None:
            _ = value

        @property
        def property_value(self) -> str:
            return "value"

    assert _public_class_member_contract(Released) == {
        "instance": {
            "binding": "instance",
            "execution_kind": "sync",
            "parameters": [
                {
                    "name": "value",
                    "kind": "POSITIONAL_OR_KEYWORD",
                    "default": {"kind": "required"},
                },
                {
                    "name": "optional",
                    "kind": "POSITIONAL_OR_KEYWORD",
                    "default": {
                        "kind": "literal",
                        "type": "builtins.int",
                        "value": 1,
                    },
                },
            ],
        },
        "class_method": {
            "binding": "class",
            "execution_kind": "sync",
            "parameters": [
                {
                    "name": "value",
                    "kind": "POSITIONAL_OR_KEYWORD",
                    "default": {"kind": "required"},
                }
            ],
        },
        "static_method": {
            "binding": "static",
            "execution_kind": "sync",
            "parameters": [
                {
                    "name": "value",
                    "kind": "POSITIONAL_OR_KEYWORD",
                    "default": {"kind": "required"},
                }
            ],
        },
    }


def test_curated_public_property_contract_detects_removed_or_changed_properties() -> None:
    class ReleasedBase:
        @property
        def retained(self) -> str:
            return "value"

    class Released(ReleasedBase):
        @property
        def retained(self) -> str:
            return "value"

        @property
        def concrete_only(self) -> str:
            return "value"

    contract: dict[str, Any] = {
        "public_properties": [
            {
                "module": "agents",
                "class_name": "ReleasedBase",
                "names": ["retained", "removed"],
            },
            {
                "module": "agents",
                "class_name": "Released",
                "names": ["retained", "concrete_only"],
            },
        ]
    }
    agents_module = SimpleNamespace(
        __all__=[],
        ReleasedBase=ReleasedBase,
        Released=Released,
    )

    assert _validate_public_property_contract(contract, agents_module) == [
        "agents.ReleasedBase.removed removed or changed a released public property"
    ]

    Changed = type(
        "Changed",
        (ReleasedBase,),
        {"retained": lambda self: "value"},
    )

    agents_module.Released = Changed

    assert _validate_public_property_contract(contract, agents_module) == [
        "agents.ReleasedBase.removed removed or changed a released public property",
        "agents.Released.retained removed or changed a released public property",
        "agents.Released.concrete_only removed or changed a released public property",
    ]


def test_public_class_member_contract_tracks_only_sdk_owned_inherited_methods() -> None:
    class ExternalBase:
        def external_method(self) -> None:
            return None

    class SDKBase(ExternalBase):
        __module__ = "agents.contract_test"

        def instance_method(self, value: str) -> None:
            _ = value

        @classmethod
        def class_method(cls, value: str) -> None:
            _ = (cls, value)

        @staticmethod
        def shadowed_method(value: str) -> None:
            _ = value

    def shadowed_method(_self: object) -> str:
        return "value"

    Released = type(
        "Released",
        (SDKBase,),
        {"shadowed_method": property(shadowed_method)},
    )

    assert _public_class_member_contract(Released) == {
        "class_method": {
            "binding": "class",
            "execution_kind": "sync",
            "parameters": [
                {
                    "name": "value",
                    "kind": "POSITIONAL_OR_KEYWORD",
                    "default": {"kind": "required"},
                }
            ],
        },
        "instance_method": {
            "binding": "instance",
            "execution_kind": "sync",
            "parameters": [
                {
                    "name": "value",
                    "kind": "POSITIONAL_OR_KEYWORD",
                    "default": {"kind": "required"},
                }
            ],
        },
    }


def test_callable_contract_preserves_wrapped_function_signature() -> None:
    def released(value: str, optional: int = 1) -> str:
        return value * optional

    def middle(*args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        return None

    def outer(*args: object, **kwargs: object) -> object:
        _ = (args, kwargs)
        return None

    middle.__wrapped__ = released  # type: ignore[attr-defined]
    outer.__wrapped__ = middle  # type: ignore[attr-defined]

    assert _callable_contract(outer)["parameters"] == [
        {
            "name": "value",
            "kind": "POSITIONAL_OR_KEYWORD",
            "default": {"kind": "required"},
        },
        {
            "name": "optional",
            "kind": "POSITIONAL_OR_KEYWORD",
            "default": {
                "kind": "literal",
                "type": "builtins.int",
                "value": 1,
            },
        },
    ]


def test_released_public_class_member_contract_rejects_breaking_changes() -> None:
    class Released:
        def inherited(self, value: str) -> None:
            _ = value

        @classmethod
        def changed_binding(cls, value: str) -> None:
            _ = (cls, value)

        @staticmethod
        def removed(value: str) -> None:
            _ = value

        def changed_signature(self, value: str, optional: int = 1) -> None:
            _ = (value, optional)

    class CompatibleBase:
        __module__ = "agents.contract_test"

        def inherited(self, value: str, optional: int = 1) -> None:
            _ = (value, optional)

    class Incompatible(CompatibleBase):
        @staticmethod
        def changed_binding(value: str) -> None:
            _ = value

        def changed_signature(self, renamed: str, optional: int = 1) -> None:
            _ = (renamed, optional)

    agents_module = SimpleNamespace(__all__=["Released"], Released=Incompatible)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": ["Released"],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {"Released": _callable_contract(Released)},
    }

    errors = validate_released_api_contract(contract, agents_module=agents_module)

    assert errors == [
        "Released.changed_binding changed binding from class to static",
        "Released.removed removed a released public method",
        "Released.changed_signature changed its released positional parameter prefix: "
        "expected [{'name': 'value', 'kind': 'POSITIONAL_OR_KEYWORD', "
        "'default': {'kind': 'required'}}, {'name': 'optional', "
        "'kind': 'POSITIONAL_OR_KEYWORD', 'default': {'kind': 'literal', "
        "'type': 'builtins.int', 'value': 1}}], got [{'name': 'renamed', "
        "'kind': 'POSITIONAL_OR_KEYWORD', 'default': {'kind': 'required'}}, "
        "{'name': 'optional', 'kind': 'POSITIONAL_OR_KEYWORD', "
        "'default': {'kind': 'literal', 'type': 'builtins.int', 'value': 1}}]",
        "Released.changed_signature.renamed added a required parameter",
    ]


def test_released_callable_contract_rejects_execution_kind_changes() -> None:
    async def released_async(value: str) -> str:
        return value

    def released_sync(value: str) -> str:
        return value

    def changed_to_sync(value: str) -> str:
        return value

    async def changed_to_async(value: str) -> str:
        return value

    def released_generator(value: str) -> Iterator[str]:
        yield value

    def changed_generator_to_sync(value: str) -> str:
        return value

    class ReleasedBase:
        __module__ = "agents.contract_test"

        @classmethod
        async def inherited_async(cls, value: str) -> str:
            _ = cls
            return value

    class Released(ReleasedBase):
        async def direct_async(self, value: str) -> str:
            return value

        @staticmethod
        def direct_sync(value: str) -> str:
            return value

        async def direct_async_generator(self, value: str) -> AsyncIterator[str]:
            yield value

    class ChangedBase:
        __module__ = "agents.contract_test"

        @classmethod
        def inherited_async(cls, value: str) -> str:
            _ = cls
            return value

    class Changed(ChangedBase):
        def direct_async(self, value: str) -> str:
            return value

        @staticmethod
        async def direct_sync(value: str) -> str:
            return value

        async def direct_async_generator(self, value: str) -> str:
            return value

    agents_module = SimpleNamespace(
        __all__=["released_async", "released_sync", "released_generator", "Released"],
        released_async=changed_to_sync,
        released_sync=changed_to_async,
        released_generator=changed_generator_to_sync,
        Released=Changed,
    )
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [
            "released_async",
            "released_sync",
            "released_generator",
            "Released",
        ],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {
            "released_async": _callable_contract(released_async),
            "released_sync": _callable_contract(released_sync),
            "released_generator": _callable_contract(released_generator),
            "Released": _callable_contract(Released),
        },
    }

    errors = validate_released_api_contract(contract, agents_module=agents_module)

    assert set(errors) == {
        "released_async changed execution from coroutine to sync",
        "released_sync changed execution from sync to coroutine",
        "released_generator changed execution from generator to sync",
        "Released.direct_async changed execution from coroutine to sync",
        "Released.direct_sync changed execution from sync to coroutine",
        "Released.direct_async_generator changed execution from async_generator to coroutine",
        "Released.inherited_async changed execution from coroutine to sync",
    }


def test_released_opaque_sentinel_default_rejects_unrepresentable_replacement() -> None:
    from agents.tool import _UNSET_FAILURE_ERROR_FUNCTION

    def released(value: object = _UNSET_FAILURE_ERROR_FUNCTION) -> None:
        _ = value

    def incompatible(value: object = object()) -> None:
        _ = value

    released_contract = _parameter_contract(released)

    assert released_contract[0]["default"] == {
        "kind": "sentinel",
        "identity": "agents.tool._UNSET_FAILURE_ERROR_FUNCTION",
    }
    with pytest.raises(TypeError, match="Unsupported public API default value: builtins.object"):
        _parameter_contract(incompatible)


def test_field_info_default_contract_preserves_the_complete_default() -> None:
    assert _default_contract(Field(default=1)) != _default_contract(Field(default=2))


def test_qualified_submodule_callable_contract_detects_signature_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def released(value: str, optional: int = 1) -> None:
        _ = (value, optional)

    def incompatible(renamed: str, optional: int = 1) -> None:
        _ = (renamed, optional)

    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(released=incompatible)
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {"agents.submodule.released": _callable_contract(released)},
    }

    errors = validate_released_api_contract(contract, agents_module=agents_module)

    assert any("changed its released positional parameter prefix" in error for error in errors)


def test_release_contract_update_freezes_submodule_only_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def helper(value: str = "default") -> None:
        _ = value

    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(helper=helper)
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "canonical_imports": [
            {
                "module": "agents.submodule",
                "name": "helper",
                "canonical_module": "agents.submodule",
                "canonical_name": "helper",
            }
        ],
        "callables": {},
    }

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
    )

    assert updated["callables"]["agents.submodule.helper"] == _callable_contract(helper)


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


def test_released_enum_contract_freezes_members_and_values() -> None:
    class ReleasedEnum(Enum):
        OLD = "old"

    class CompatibleEnum(Enum):
        OLD = "old"
        NEW = "new"

    class RenamedEnum(Enum):
        RENAMED = "old"

    class ChangedValueEnum(Enum):
        OLD = "changed"

    contract: dict[str, Any] = {
        "required_top_level_exports": ["ReleasedEnum"],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {"ReleasedEnum": _callable_contract(ReleasedEnum)},
    }

    assert (
        validate_released_api_contract(
            contract,
            agents_module=SimpleNamespace(__all__=["ReleasedEnum"], ReleasedEnum=CompatibleEnum),
        )
        == []
    )
    assert validate_released_api_contract(
        contract,
        agents_module=SimpleNamespace(__all__=["ReleasedEnum"], ReleasedEnum=RenamedEnum),
    ) == ["ReleasedEnum.OLD removed or renamed a released enum member"]
    assert validate_released_api_contract(
        contract,
        agents_module=SimpleNamespace(__all__=["ReleasedEnum"], ReleasedEnum=ChangedValueEnum),
    ) == [
        "ReleasedEnum.OLD changed its released enum value: expected "
        "{'kind': 'literal', 'type': 'builtins.str', 'value': 'old'}, got "
        "{'kind': 'literal', 'type': 'builtins.str', 'value': 'changed'}"
    ]


def test_public_api_contract_requires_real_export_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agents

    contract: dict[str, Any] = {
        "required_top_level_exports": ["AgentsException"],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.delattr(agents, "AgentsException")

    assert validate_released_api_contract(contract) == [
        "Missing released top-level bindings: ['AgentsException']"
    ]


@pytest.mark.parametrize("failure", ["membership", "binding"])
def test_public_api_contract_requires_released_submodule_exports(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    sandbox_error = type("SandboxError", (Exception,), {})
    submodule = SimpleNamespace(
        __all__=[] if failure == "membership" else ["SandboxError"],
        SandboxError=sandbox_error,
    )
    if failure == "binding":
        del submodule.SandboxError
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["SandboxError"],
                "optional_bindings": {},
                "optional_exports": {},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    errors = validate_released_api_contract(contract, agents_module=agents_module)

    expected_kind = "exports" if failure == "membership" else "bindings"
    assert errors == [f"Missing released agents.submodule {expected_kind}: ['SandboxError']"]


def test_public_api_contract_rejects_missing_self_canonical_binding() -> None:
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [
            {
                "module": "agents",
                "name": "Missing",
                "canonical_module": "agents",
                "canonical_name": "Missing",
            }
        ],
        "callables": {},
    }

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "agents.Missing no longer resolves to agents.Missing"
    ]


def test_public_api_contract_allows_declared_platform_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.platform_specific"],
        "platform_import_errors": [
            {
                "module": "agents.platform_specific",
                "platforms": ["win32"],
                "error_type": "ImportError",
                "message_contains": "not supported on Windows",
            }
        ],
        "canonical_imports": [
            {
                "module": "agents.platform_specific",
                "name": "PlatformBinding",
                "canonical_module": "agents.platform_specific",
                "canonical_name": "PlatformBinding",
            }
        ],
        "callables": {},
    }

    def raise_platform_error(module_name: str, _: Any) -> Any:
        assert module_name == "agents.platform_specific"
        raise ImportError("Backend is not supported on Windows. Use another backend.")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_support, "_import_contract_module", raise_platform_error)

    assert validate_released_api_contract(contract, agents_module=agents_module) == []


def test_public_api_contract_allows_binding_with_unavailable_canonical_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    parent_module = SimpleNamespace()
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.platform_parent", "agents.platform_child"],
        "platform_import_errors": [
            {
                "module": "agents.platform_child",
                "platforms": ["win32"],
                "error_type": "ImportError",
                "message_contains": "not supported on Windows",
            }
        ],
        "canonical_imports": [
            {
                "module": "agents.platform_parent",
                "name": "PlatformBinding",
                "canonical_module": "agents.platform_child",
                "canonical_name": "PlatformBinding",
            }
        ],
        "callables": {},
    }

    def import_platform_module(module_name: str, _: Any) -> Any:
        if module_name == "agents.platform_parent":
            return parent_module
        assert module_name == "agents.platform_child"
        raise ImportError("Backend is not supported on Windows. Use another backend.")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_support, "_import_contract_module", import_platform_module)

    assert validate_released_api_contract(contract, agents_module=agents_module) == []


def test_public_api_contract_rejects_unexpected_platform_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.platform_specific"],
        "platform_import_errors": [
            {
                "module": "agents.platform_specific",
                "platforms": ["win32"],
                "error_type": "ImportError",
                "message_contains": "not supported on Windows",
            }
        ],
        "canonical_imports": [],
        "callables": {},
    }

    def raise_unexpected_error(module_name: str, _: Any) -> Any:
        assert module_name == "agents.platform_specific"
        raise ImportError("Unexpected dependency failure")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_support, "_import_contract_module", raise_unexpected_error)

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Failed to import released module agents.platform_specific: "
        "ImportError('Unexpected dependency failure')"
    ]


def test_public_api_contract_rejects_same_named_foreign_platform_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.platform_specific"],
        "platform_import_errors": [
            {
                "module": "agents.platform_specific",
                "platforms": ["win32"],
                "error_type": "ImportError",
                "message_contains": "not supported on Windows",
            }
        ],
        "canonical_imports": [],
        "callables": {},
    }
    foreign_import_error = type("ImportError", (Exception,), {})

    def raise_foreign_error(module_name: str, _: Any) -> Any:
        assert module_name == "agents.platform_specific"
        raise foreign_import_error("Backend is not supported on Windows.")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_support, "_import_contract_module", raise_foreign_error)

    errors = validate_released_api_contract(contract, agents_module=agents_module)
    assert len(errors) == 1
    assert errors[0].startswith("Failed to import released module agents.platform_specific:")


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
        "callables": {
            "ContractExample": {
                "kind": "class",
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


def test_release_contract_update_freezes_new_exports_and_callables() -> None:
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

    class NewEnum(Enum):
        VALUE = "value"

    agents_module = SimpleNamespace(
        __all__=["new_helper", "Existing", "NewPublic", "NewEnum", "Uninspectable"],
        Existing=Existing,
        new_helper=new_helper,
        NewPublic=NewPublic,
        NewEnum=NewEnum,
        Uninspectable=Uninspectable,
    )
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": ["Existing"],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "callables": {},
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
        "NewEnum",
        "Uninspectable",
    ]
    assert set(updated["callables"]) == {"Existing", "NewEnum", "NewPublic", "new_helper"}
    assert updated["callables"]["Existing"]["kind"] == "class"
    assert updated["callables"]["new_helper"]["kind"] == "function"
    assert [field["name"] for field in updated["callables"]["Existing"]["dataclass_fields"]] == [
        "value",
        "optional",
    ]
    assert [field["name"] for field in updated["callables"]["NewPublic"]["dataclass_fields"]] == [
        "name",
        "enabled",
    ]
    assert updated["callables"]["NewEnum"]["enum_members"] == [
        {
            "name": "VALUE",
            "value": {"kind": "literal", "type": "builtins.str", "value": "value"},
        }
    ]
    assert updated["public_modules"] == ["agents"]
    assert updated["canonical_imports"] == []
    assert updated["required_submodule_exports"] == {}

    unchanged = build_released_api_contract(
        updated,
        baseline="v0.20.0",
        baseline_commit="c" * 40,
        agents_module=agents_module,
    )
    assert unchanged["baseline_commit"] == "b" * 40


def test_release_contract_update_promotes_selected_submodule_exports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = object()
    added = object()
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["Existing", "Added"], Existing=existing, Added=added)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["Existing"],
                "optional_bindings": {},
                "optional_exports": {},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
    )

    assert updated["required_submodule_exports"] == {
        "agents.submodule": {
            "names": ["Existing", "Added"],
            "optional_bindings": {},
            "optional_exports": {},
        }
    }


def test_public_api_contract_allows_declared_optional_submodule_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["OptionalBackend"])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalBackend"],
                "optional_bindings": {"OptionalBackend": "missing_optional_backend_dependency"},
                "optional_exports": {},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == []


def test_public_api_contract_allows_declared_optional_submodule_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalBackend"],
                "optional_bindings": {},
                "optional_exports": {"OptionalBackend": "missing_optional_backend_dependency"},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == []


def test_public_api_contract_requires_available_optional_submodule_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalBackend"],
                "optional_bindings": {},
                "optional_exports": {"OptionalBackend": "json"},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Missing released agents.submodule exports: ['OptionalBackend']",
        "Missing released agents.submodule bindings: ['OptionalBackend']",
    ]


def test_public_api_contract_requires_declared_dependencies_in_strict_optional_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalBackend"],
                "optional_bindings": {},
                "optional_exports": {"OptionalBackend": "mistyped_dependency_name"},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(
        contract,
        agents_module=agents_module,
        require_all_optional_exports=True,
    ) == [
        "Required optional dependencies for released agents.submodule are unavailable: "
        "['OptionalBackend -> mistyped_dependency_name']"
    ]


def test_public_api_contract_treats_loaded_dependency_without_spec_as_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=[])
    dependency_name = "loaded_dependency_without_spec"
    monkeypatch.setitem(sys.modules, dependency_name, SimpleNamespace(__spec__=None))
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalBackend"],
                "optional_bindings": {},
                "optional_exports": {"OptionalBackend": dependency_name},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Missing released agents.submodule exports: ['OptionalBackend']",
        "Missing released agents.submodule bindings: ['OptionalBackend']",
    ]


@pytest.mark.parametrize(
    ("optional_exports", "expected_error"),
    [
        (
            {"OptionalBackend": None},
            "optional_exports dependency for 'OptionalBackend' must be a non-empty string",
        ),
        (
            {"OptionalBackend": ""},
            "optional_exports dependency for 'OptionalBackend' must be a non-empty string",
        ),
        (
            [],
            "optional_exports must be an object mapping export names to dependency modules",
        ),
    ],
)
def test_public_api_contract_rejects_malformed_optional_dependency_declarations(
    monkeypatch: pytest.MonkeyPatch,
    optional_exports: object,
    expected_error: str,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalBackend"],
                "optional_bindings": {},
                "optional_exports": optional_exports,
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Invalid released agents.submodule optional dependency declarations: " + expected_error
    ]


def test_release_contract_update_rejects_new_submodule_export_without_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = object()
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["Existing", "Added"], Existing=existing)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["Existing"],
                "optional_bindings": {},
                "optional_exports": {},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_support,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    with pytest.raises(
        ValueError,
        match="Cannot promote an invalid released API contract",
    ):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
        )


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
        "callables": {"Released": _callable_contract(Released)},
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


def test_release_contract_update_rejects_function_signature_change() -> None:
    def released(value: str, optional: int = 1) -> None:
        _ = (value, optional)

    def incompatible(renamed: str, optional: int = 1) -> None:
        _ = (renamed, optional)

    agents_module = SimpleNamespace(__all__=["released"], released=incompatible)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": ["released"],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {"released": _callable_contract(released)},
    }

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "released changed its released positional parameter prefix: expected "
        "[{'name': 'value', 'kind': 'POSITIONAL_OR_KEYWORD', "
        "'default': {'kind': 'required'}}, {'name': 'optional', "
        "'kind': 'POSITIONAL_OR_KEYWORD', "
        "'default': {'kind': 'literal', 'type': 'builtins.int', 'value': 1}}], got "
        "[{'name': 'renamed', 'kind': 'POSITIONAL_OR_KEYWORD', "
        "'default': {'kind': 'required'}}, {'name': 'optional', "
        "'kind': 'POSITIONAL_OR_KEYWORD', "
        "'default': {'kind': 'literal', 'type': 'builtins.int', 'value': 1}}]",
        "released.renamed added a required parameter",
    ]


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
        "callables": {"Released": _callable_contract(Released)},
    }

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Released callable agents.Released changed kind from class to function"
    ]
    with pytest.raises(
        ValueError,
        match="Released callable agents.Released changed kind from class to function",
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
        "callables": {},
    }

    with pytest.raises(ValueError, match="must not contain duplicate exports"):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
        )
