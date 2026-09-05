import abc
import ast
import builtins
import importlib
import inspect
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import asdict, dataclass, replace
from enum import Enum
from importlib.metadata import version
from inspect import Parameter, Signature
from pathlib import Path
from textwrap import indent
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, TypeAlias, TypeVar, cast

import pytest
from pydantic import BaseModel, Field
from typing_extensions import Required, TypeAliasType, TypedDict

import integration_tests._contract_surface as contract_surface
from integration_tests._contract_support import (
    _validate_voice_public_class_contract_policy,
    build_released_api_contract,
)
from integration_tests._contract_surface import (
    OptionalDependencyInstallation,
    SubmoduleExportPolicy,
    _callable_contract,
    _default_contract,
    _parameter_contract,
    _public_class_member_contract,
    load_api_contract,
    load_submodule_export_policy,
)
from integration_tests._contract_validation import (
    _validate_parameter_contract,
    _validate_public_class_contract,
    _validate_public_property_contract,
    _validate_public_type_alias_contract,
    _validate_public_typed_dict_contract,
    validate_released_api_contract,
)

CONTRACT = Path(__file__).parent / "fixtures" / "released_api_contract.json"


def _release_policy(
    modules: dict[str, dict[str, dict[str, str]]],
    *,
    dependency_installations: tuple[OptionalDependencyInstallation, ...] = (),
    canonical_imports: tuple[dict[str, str], ...] = (),
    public_class_contracts: tuple[dict[str, Any], ...] = (),
    public_properties: tuple[dict[str, Any], ...] = (),
    public_type_aliases: tuple[dict[str, str], ...] = (),
    public_typed_dicts: tuple[dict[str, Any], ...] = (),
) -> SubmoduleExportPolicy:
    return SubmoduleExportPolicy(
        modules=modules,
        dependency_installations=dependency_installations,
        canonical_imports=canonical_imports,
        public_class_contracts=public_class_contracts,
        public_properties=public_properties,
        public_type_aliases=public_type_aliases,
        public_typed_dicts=public_typed_dicts,
    )


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


def test_type_default_contract_preserves_identity() -> None:
    assert _default_contract(int) == {
        "kind": "type",
        "identity": "builtins.int",
    }

    def released_callable(value: type = int) -> None:
        _ = value

    def changed_callable(value: type = float) -> None:
        _ = value

    errors = _validate_parameter_contract(
        "Example",
        _parameter_contract(released_callable),
        _parameter_contract(changed_callable),
    )

    assert len(errors) == 1
    assert "changed its released positional parameter prefix" in errors[0]


def test_optional_dependency_for_module_import_uses_canonical_bindings() -> None:
    contract = {
        "required_submodule_exports": {
            "agents.voice": {
                "names": ["VoicePipeline"],
                "optional_bindings": {"VoicePipeline": "numpy"},
                "optional_exports": {},
            }
        },
        "canonical_imports": [
            {
                "canonical_module": "agents.voice.pipeline",
                "canonical_name": "VoicePipeline",
                "module": "agents.voice",
                "name": "VoicePipeline",
            }
        ],
    }

    assert (
        contract_surface._optional_dependency_for_module_import(contract, "agents.voice.pipeline")
        == "numpy"
    )
    assert (
        contract_surface._optional_dependency_for_binding(
            contract, "agents.voice.pipeline", "VoicePipeline"
        )
        == "numpy"
    )


def test_released_api_contract_fixture_matches_installed_version() -> None:
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
    class ReleasedBase(metaclass=abc.ABCMeta):
        @abc.abstractmethod
        def base_requirement(self) -> None:
            pass

        @property
        def retained(self) -> str:
            return "value"

    class Released(ReleasedBase):
        def base_requirement(self) -> None:
            pass

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

    class Changed(ReleasedBase):
        def base_requirement(self) -> None:
            pass

        @abc.abstractmethod
        def new_requirement(self) -> None:
            pass

    with pytest.raises(TypeError):
        cast(type[Any], Changed)()

    agents_module.Released = Changed

    assert _validate_public_property_contract(contract, agents_module) == [
        "agents.ReleasedBase.removed removed or changed a released public property",
        "agents.Released.concrete_only removed or changed a released public property",
    ]


def test_curated_public_class_contract_detects_abstract_member_and_state_changes() -> None:
    class ReleasedBase(metaclass=abc.ABCMeta):
        @abc.abstractmethod
        def base_requirement(self) -> None:
            pass

    class Released(ReleasedBase):
        def base_requirement(self) -> None:
            pass

    contract: dict[str, Any] = {
        "public_class_contracts": [
            {
                "abstract_members": ["base_requirement"],
                "class_name": "ReleasedBase",
                "module": "agents",
            },
            {
                "abstract": False,
                "class_name": "Released",
                "module": "agents",
            },
        ]
    }
    agents_module = SimpleNamespace(__all__=[], ReleasedBase=ReleasedBase, Released=Released)

    assert _validate_public_class_contract(contract, agents_module) == []

    class ChangedBase(ReleasedBase):
        @abc.abstractmethod
        def new_requirement(self) -> None:
            pass

    class Changed(ChangedBase):
        def base_requirement(self) -> None:
            pass

        def new_requirement(self) -> None:
            pass

    class ExistingExternalSubclass(ChangedBase):
        def base_requirement(self) -> None:
            pass

    assert not inspect.isabstract(Changed)
    with pytest.raises(TypeError):
        cast(type[Any], ExistingExternalSubclass)()

    agents_module.ReleasedBase = ChangedBase
    agents_module.Released = Changed

    assert _validate_public_class_contract(contract, agents_module) == [
        "agents.ReleasedBase changed its released public abstract members: expected "
        "['base_requirement'], got ['base_requirement', 'new_requirement']"
    ]

    agents_module.Released = ExistingExternalSubclass
    assert _validate_public_class_contract(contract, agents_module) == [
        "agents.ReleasedBase changed its released public abstract members: expected "
        "['base_requirement'], got ['base_requirement', 'new_requirement']",
        "agents.Released changed its released public class state: expected concrete, got abstract",
    ]


def test_curated_public_property_contract_supports_factory_return_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ScriptedSession:
        @property
        def calls(self) -> tuple[str, ...]:
            return ()

    def scripted_session() -> ScriptedSession:
        return ScriptedSession()

    contract: dict[str, Any] = {
        "public_properties": [
            {
                "module": "agents.testing",
                "factory_name": "scripted_session",
                "names": ["calls", "remaining_steps"],
            }
        ]
    }
    testing_module = SimpleNamespace(scripted_session=scripted_session)
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda _module_name, _agents_module: testing_module,
    )

    assert _validate_public_property_contract(contract, testing_module) == [
        "agents.testing.scripted_session.remaining_steps removed or changed a released public "
        "property"
    ]


def test_curated_public_type_alias_contract_records_and_validates_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EventA:
        pass

    class EventB:
        pass

    EventA.__module__ = "agents.aliases"
    EventA.__qualname__ = "EventA"
    EventB.__module__ = "agents.aliases"
    EventB.__qualname__ = "EventB"

    agents_module = SimpleNamespace(__all__=[])
    aliases_module = SimpleNamespace(
        __all__=[],
        PublicAlias=Literal["b", "a"] | EventB | EventA,
    )
    modules = {
        "agents": agents_module,
        "agents.aliases": aliases_module,
    }
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: modules[module_name],
    )
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_class_contracts": [],
        "public_properties": [],
        "public_type_aliases": [],
        "public_typed_dicts": [],
        "callables": {},
    }

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {"agents.aliases": {"optional_bindings": {}, "optional_exports": {}}},
            public_type_aliases=(
                {
                    "module": "agents.aliases",
                    "name": "PublicAlias",
                },
            ),
        ),
    )

    assert updated["public_type_aliases"] == [
        {
            "definition": {
                "kind": "union",
                "members": [
                    {
                        "kind": "literal",
                        "values": [
                            {
                                "kind": "literal",
                                "type": "builtins.str",
                                "value": "a",
                            },
                            {
                                "kind": "literal",
                                "type": "builtins.str",
                                "value": "b",
                            },
                        ],
                    },
                    {"identity": "agents.aliases.EventA", "kind": "type"},
                    {"identity": "agents.aliases.EventB", "kind": "type"},
                ],
            },
            "module": "agents.aliases",
            "name": "PublicAlias",
        }
    ]

    aliases_module.PublicAlias = Literal["a", "b"] | EventA | EventB
    assert _validate_public_type_alias_contract(updated, agents_module) == []

    aliases_module.PublicAlias = Literal["a"] | EventA
    errors = _validate_public_type_alias_contract(updated, agents_module)
    assert len(errors) == 1
    assert errors[0].startswith("agents.aliases.PublicAlias changed its released public type alias")

    aliases_module.PublicAlias = list[str]
    with pytest.raises(
        ValueError,
        match=(
            r"agents\.aliases\.PublicAlias no longer has a supported released public type alias "
            r"definition: unsupported"
        ),
    ):
        build_released_api_contract(
            updated,
            baseline="v0.20.1",
            baseline_commit="c" * 40,
            agents_module=agents_module,
            release_policy=_release_policy(
                {"agents.aliases": {"optional_bindings": {}, "optional_exports": {}}},
                public_type_aliases=(
                    {
                        "module": "agents.aliases",
                        "name": "PublicAlias",
                    },
                ),
            ),
        )


def test_new_top_level_type_alias_requires_explicit_policy() -> None:
    existing_alias = Literal["existing"]
    new_alias = Callable[[str], str | None]
    agents_module = SimpleNamespace(
        __all__=["ExistingAlias", "NewAlias"],
        ExistingAlias=existing_alias,
        NewAlias=new_alias,
    )
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": ["ExistingAlias"],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_class_contracts": [],
        "public_properties": [],
        "public_type_aliases": [],
        "public_typed_dicts": [],
        "callables": {},
    }

    with pytest.raises(ValueError) as exc_info:
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy({}),
        )

    assert str(exc_info.value) == (
        "Cannot promote new top-level type aliases without public_type_aliases policy entries "
        "for module 'agents': ['NewAlias']"
    )

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {},
            public_type_aliases=({"module": "agents", "name": "NewAlias"},),
        ),
    )

    assert updated["public_type_aliases"] == [
        {
            "definition": {
                "kind": "callable",
                "parameters": [{"identity": "builtins.str", "kind": "type"}],
                "return": {
                    "kind": "union",
                    "members": [
                        {"identity": "builtins.NoneType", "kind": "type"},
                        {"identity": "builtins.str", "kind": "type"},
                    ],
                },
            },
            "module": "agents",
            "name": "NewAlias",
        }
    ]

    agents_module.NewAlias = Callable[[bytes], str | None]
    errors = _validate_public_type_alias_contract(updated, agents_module)
    assert len(errors) == 1
    assert errors[0].startswith("agents.NewAlias changed its released public type alias")


def test_new_originless_explicit_type_alias_requires_policy() -> None:
    class ExistingClass:
        pass

    class NewClass:
        pass

    agents_module = ModuleType("synthetic_agents")
    agents_module.TypeAlias = TypeAlias
    agents_module.__annotations__ = {"NewAlias": "TypeAlias"}
    agents_module.__all__ = [
        "ExistingClass",
        "NewAlias",
        "NewClass",
        "UnannotatedBinding",
    ]
    agents_module.ExistingClass = ExistingClass
    agents_module.NewAlias = str
    agents_module.NewClass = NewClass
    agents_module.UnannotatedBinding = str
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": ["ExistingClass"],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_class_contracts": [],
        "public_properties": [],
        "public_type_aliases": [],
        "public_typed_dicts": [],
        "callables": {},
    }

    with pytest.raises(ValueError) as exc_info:
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy({}),
        )

    assert str(exc_info.value) == (
        "Cannot promote new top-level type aliases without public_type_aliases policy entries "
        "for module 'agents': ['NewAlias']"
    )

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {},
            public_type_aliases=({"module": "agents", "name": "NewAlias"},),
        ),
    )
    assert updated["public_type_aliases"] == [
        {
            "definition": {"identity": "builtins.str", "kind": "type"},
            "module": "agents",
            "name": "NewAlias",
        }
    ]

    agents_module.NewAlias = bytes
    errors = _validate_public_type_alias_contract(updated, agents_module)
    assert len(errors) == 1
    assert errors[0].startswith("agents.NewAlias changed its released public type alias")


@pytest.mark.parametrize(
    ("facade_count", "control_flow"), [(0, False), (1, False), (3, False), (1, True)]
)
def test_new_renamed_originless_type_alias_reexport_requires_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, facade_count: int, control_flow: bool
) -> None:
    package_name = f"contract_alias_reexport_package_{facade_count}_{control_flow}"
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "aliases.py").write_text(
        "from typing import TypeAlias\nInternalAlias: TypeAlias = str\n",
        encoding="utf-8",
    )
    import_module, import_name = "aliases", "InternalAlias"
    for index in range(facade_count):
        facade_module, facade_name = f"facade{index}", f"ForwardedAlias{index}"
        facade_import = f"from .{import_module} import {import_name} as {facade_name}\n"
        if control_flow:
            facade_import = (
                "try:\n" + indent(facade_import, "    ") + "except ImportError:\n    raise\n"
            )
        (package_dir / f"{facade_module}.py").write_text(
            facade_import,
            encoding="utf-8",
        )
        import_module, import_name = facade_module, facade_name
    export_import = (
        f"from .{import_module} import (\n"
        f"    {import_name} as PublicAlias,\n"
        f"    {import_name} as ShadowedAlias,\n"
        f"    {import_name} as WalrusShadowedAlias,\n"
        f"    {import_name} as ControlFlowShadowedAlias,\n"
        f"    {import_name} as FunctionGlobalShadowedAlias,\n"
        f"    {import_name} as ClassGlobalShadowedAlias,\n"
        ")\n"
    )
    if control_flow:
        export_import = "import sys\nif sys.version_info >= (3, 10):\n" + indent(
            export_import, "    "
        )
    (package_dir / "__init__.py").write_text(
        export_import + "ShadowedAlias = str\n"
        "UnrelatedBinding = (WalrusShadowedAlias := str)\n"
        "if True:\n"
        "    ControlFlowShadowedAlias = str\n"
        "def capture(PublicAlias):\n"
        "    from .aliases import InternalAlias as PublicAlias\n"
        "    PublicAlias = str\n"
        "    match PublicAlias:\n"
        "        case {**PublicAlias}:\n"
        "            return PublicAlias\n"
        "class Container:\n"
        "    from .aliases import InternalAlias as PublicAlias\n"
        "    PublicAlias = str\n"
        "def mutate_global():\n"
        "    global FunctionGlobalShadowedAlias\n"
        "    FunctionGlobalShadowedAlias = str\n"
        "class GlobalMutator:\n"
        "    global ClassGlobalShadowedAlias\n"
        "    ClassGlobalShadowedAlias = str\n"
        "UnannotatedBinding = str\n"
        "__all__ = [\n"
        '    "PublicAlias",\n'
        '    "ShadowedAlias",\n'
        '    "WalrusShadowedAlias",\n'
        '    "ControlFlowShadowedAlias",\n'
        '    "FunctionGlobalShadowedAlias",\n'
        '    "ClassGlobalShadowedAlias",\n'
        '    "UnannotatedBinding",\n'
        "]\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    agents_module = importlib.import_module(package_name)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_class_contracts": [],
        "public_properties": [],
        "public_type_aliases": [],
        "public_typed_dicts": [],
        "callables": {},
    }

    with pytest.raises(ValueError) as exc_info:
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy({}),
        )

    assert str(exc_info.value) == (
        "Cannot promote new top-level type aliases without public_type_aliases policy entries "
        "for module 'agents': ['PublicAlias']"
    )

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {},
            public_type_aliases=({"module": "agents", "name": "PublicAlias"},),
        ),
    )
    assert updated["public_type_aliases"] == [
        {
            "definition": {"identity": "builtins.str", "kind": "type"},
            "module": "agents",
            "name": "PublicAlias",
        }
    ]

    agents_module.PublicAlias = bytes
    errors = _validate_public_type_alias_contract(updated, agents_module)
    assert len(errors) == 1
    assert errors[0].startswith("agents.PublicAlias changed its released public type alias")


@pytest.mark.parametrize(
    "invalid_edge", ["rebound", "mismatched_value", "outside_package", "cycle"]
)
def test_originless_alias_facade_requires_unambiguous_package_provenance(
    monkeypatch: pytest.MonkeyPatch, invalid_edge: str
) -> None:
    package_name = "contract_alias_chain_package"
    agents_module = ModuleType(package_name)
    facade = ModuleType(f"{package_name}.facade")
    definitions = ModuleType(f"{package_name}.definitions")
    external = ModuleType(f"{package_name}_external")
    agents_module.__all__ = ["PublicAlias"]
    agents_module.PublicAlias = str
    facade.ForwardedAlias = str
    definitions.InternalAlias = str
    definitions.__annotations__ = {"InternalAlias": TypeAlias}
    external.InternalAlias = str
    external.__annotations__ = {"InternalAlias": TypeAlias}
    sources = {
        package_name: "from .facade import ForwardedAlias as PublicAlias\n",
        facade.__name__: "from .definitions import InternalAlias as ForwardedAlias\n",
    }
    if invalid_edge == "rebound":
        sources[facade.__name__] += "ForwardedAlias = str\n"
    elif invalid_edge == "mismatched_value":
        facade.ForwardedAlias = bytes
    elif invalid_edge == "outside_package":
        sources[facade.__name__] = (
            f"from {external.__name__} import InternalAlias as ForwardedAlias\n"
        )
    else:
        sources[facade.__name__] = "from . import PublicAlias as ForwardedAlias\n"

    # Use already-loaded modules to exercise cyclic provenance without executing circular imports.
    for module in (agents_module, facade, definitions, external):
        module.__package__ = package_name
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(
        contract_surface.inspect,
        "getsource",
        lambda module: sources[module.__name__],
    )
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_class_contracts": [],
        "public_properties": [],
        "public_type_aliases": [],
        "public_typed_dicts": [],
        "callables": {},
    }

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy({}),
    )

    assert updated["required_top_level_exports"] == ["PublicAlias"]
    assert updated["public_type_aliases"] == []


def test_module_binding_visitor_ignores_type_parameter_bindings() -> None:
    if not hasattr(ast, "TypeVar"):
        pytest.skip("PEP 695 AST nodes require Python 3.12 or newer")

    module_tree = ast.parse(
        "from .aliases import InternalAlias as PublicAlias\n"
        "def capture[PublicAlias](value):\n"
        "    return value\n"
    )

    bindings = contract_surface._ModuleBindingVisitor("PublicAlias")
    bindings.visit(module_tree)

    assert bindings.count == 1
    assert not bindings.has_wildcard_import
    assert len(bindings.from_imports) == 1
    assert bindings.from_imports[0][1] == "InternalAlias"


def test_new_type_alias_type_requires_policy() -> None:
    new_alias = TypeAliasType("NewAlias", str)
    agents_module = SimpleNamespace(__all__=["NewAlias"], NewAlias=new_alias)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_class_contracts": [],
        "public_properties": [],
        "public_type_aliases": [],
        "public_typed_dicts": [],
        "callables": {},
    }

    with pytest.raises(ValueError, match="for module 'agents': \\['NewAlias'\\]"):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy({}),
        )

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {},
            public_type_aliases=({"module": "agents", "name": "NewAlias"},),
        ),
    )
    assert updated["public_type_aliases"][0]["definition"] == {
        "identity": "builtins.str",
        "kind": "type",
    }


@pytest.mark.parametrize(
    "declaration",
    [
        "backport",
        "type Public[T] = str",
        "type Public[T, U] = str",
        "type Public[T] = UndefinedType",
    ],
)
def test_generic_type_alias_type_is_rejected_before_unwrapping(declaration: str) -> None:
    if declaration == "backport":
        alias = TypeAliasType("Public", str, type_params=(TypeVar("T"),))
    else:
        if sys.version_info < (3, 12):
            pytest.skip("PEP 695 requires Python 3.12+")
        namespace: dict[str, Any] = {}
        exec(declaration, namespace)
        alias = namespace["Public"]
    agents_module = SimpleNamespace(__all__=["Public"], Public=alias)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_type_aliases": [],
        "callables": {},
    }

    with pytest.raises(ValueError) as exc_info:
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy(
                {}, public_type_aliases=({"module": "agents", "name": "Public"},)
            ),
        )

    assert str(exc_info.value) == (
        "Cannot promote public type alias agents.Public: "
        "generic public type alias is unsupported: Public"
    )

    contract["public_type_aliases"] = [
        {
            "module": "agents",
            "name": "Public",
            "definition": {"kind": "type", "identity": "builtins.str"},
        }
    ]
    assert _validate_public_type_alias_contract(contract, agents_module) == [
        "agents.Public no longer has a supported released public type alias definition: "
        "generic public type alias is unsupported: Public"
    ]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12+")
def test_unresolved_type_alias_is_validation_error() -> None:
    namespace: dict[str, Any] = {}
    exec(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    TypeOnlyName = str\n"
        "type Public = TypeOnlyName\n",
        namespace,
    )
    agents_module = SimpleNamespace(__all__=["Public"], Public=namespace["Public"])
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_type_aliases": [],
        "callables": {},
    }
    policy = _release_policy({}, public_type_aliases=({"module": "agents", "name": "Public"},))

    with pytest.raises(ValueError) as exc_info:
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=policy,
        )

    assert str(exc_info.value) == (
        "Cannot promote public type alias agents.Public: "
        "cannot resolve public type alias Public at runtime: "
        "NameError: name 'TypeOnlyName' is not defined"
    )
    contract["public_type_aliases"] = [
        {
            "module": "agents",
            "name": "Public",
            "definition": {"kind": "type", "identity": "builtins.str"},
        }
    ]
    assert _validate_public_type_alias_contract(contract, agents_module) == [
        "agents.Public no longer has a supported released public type alias definition: "
        "cannot resolve public type alias Public at runtime: "
        "NameError: name 'TypeOnlyName' is not defined"
    ]

    namespace["TypeOnlyName"] = str
    contract["public_type_aliases"] = []
    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=policy,
    )
    assert updated["public_type_aliases"][0]["definition"] == {
        "kind": "type",
        "identity": "builtins.str",
    }
    assert _validate_public_type_alias_contract(updated, agents_module) == []


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12+")
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_lazy_type_alias_evaluation_exception_boundary(error_type: type[BaseException]) -> None:
    class AliasTarget:
        def __class_getitem__(cls, parameter: object) -> object:
            raise error_type("alias evaluation failed")

    namespace: dict[str, Any] = {"AliasTarget": AliasTarget}
    exec("type Public = AliasTarget[str]", namespace)
    agents_module = SimpleNamespace(Public=namespace["Public"])
    policy_entries = ({"module": "agents", "name": "Public"},)

    if error_type is RuntimeError:
        with pytest.raises(ValueError) as exc_info:
            contract_surface._public_type_alias_contract(policy_entries, agents_module)
        assert str(exc_info.value) == (
            "Cannot promote public type alias agents.Public: "
            "cannot resolve public type alias Public at runtime: "
            "RuntimeError: alias evaluation failed"
        )
    else:
        with pytest.raises(error_type, match="alias evaluation failed"):
            contract_surface._public_type_alias_contract(policy_entries, agents_module)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12+")
def test_recursive_type_alias_type_is_rejected() -> None:
    namespace: dict[str, Any] = {}
    exec("type Recursive = Recursive | None", namespace)
    agents_module = SimpleNamespace(__all__=["Recursive"], Recursive=namespace["Recursive"])
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_class_contracts": [],
        "public_properties": [],
        "public_type_aliases": [],
        "public_typed_dicts": [],
        "callables": {},
    }

    with pytest.raises(ValueError) as exc_info:
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy(
                {},
                public_type_aliases=({"module": "agents", "name": "Recursive"},),
            ),
        )

    assert str(exc_info.value) == (
        "Cannot promote public type alias agents.Recursive: "
        "recursive public type alias is unsupported: Recursive"
    )


def test_curated_public_typed_dict_contract_detects_field_shape_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReleasedState(TypedDict, total=False):
        required_name: Required[str]
        count: int

    contract: dict[str, Any] = {
        "public_typed_dicts": [
            {
                "module": "agents.testing",
                "class_name": "ReleasedState",
                "fields": [
                    {
                        "name": "required_name",
                        "required": True,
                        "annotation": "Required[str]",
                    },
                    {"name": "count", "required": False, "annotation": "int"},
                ],
            }
        ]
    }
    testing_module = SimpleNamespace(ReleasedState=ReleasedState)
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda _module_name, _agents_module: testing_module,
    )

    assert _validate_public_typed_dict_contract(contract, testing_module) == []

    class ChangedState(TypedDict, total=False):
        required_name: int

    testing_module.ReleasedState = ChangedState

    assert _validate_public_typed_dict_contract(contract, testing_module) == [
        "agents.testing.ReleasedState.required_name changed its released TypedDict field "
        "contract: expected {'name': 'required_name', 'required': True, 'annotation': "
        "'Required[str]'}, got {'name': 'required_name', 'required': False, "
        "'annotation': 'int'}",
        "agents.testing.ReleasedState.count changed its released TypedDict field contract: "
        "expected {'name': 'count', 'required': False, 'annotation': 'int'}, got None",
    ]


@pytest.mark.parametrize(
    ("formatted", "expected"),
    [
        ("typing.Required[str]", "Required[str]"),
        ("typing_extensions.Required[str]", "Required[str]"),
        ("typing.NotRequired[str]", "NotRequired[str]"),
        ("typing_extensions.NotRequired[str]", "NotRequired[str]"),
        ("mytyping.Required[str]", "mytyping.Required[str]"),
        (
            "vendor.typing_extensions.NotRequired[str]",
            "vendor.typing_extensions.NotRequired[str]",
        ),
        ("list[typing.Required[str]]", "list[typing.Required[str]]"),
    ],
)
def test_typed_dict_requiredness_annotation_contract_is_python_version_independent(
    monkeypatch: pytest.MonkeyPatch,
    formatted: str,
    expected: str,
) -> None:
    annotation = object()
    monkeypatch.setattr(
        contract_surface.inspect,
        "formatannotation",
        lambda value: formatted if value is annotation else repr(value),
    )

    assert contract_surface._annotation_contract(annotation) == expected


def test_curated_public_property_contract_honors_optional_dependency_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OptionalClient:
        pass

    contract: dict[str, Any] = {
        "required_submodule_exports": {
            "agents.optional": {
                "names": ["OptionalClient"],
                "optional_bindings": {},
                "optional_exports": {"OptionalClient": "optional_backend"},
            }
        },
        "public_properties": [
            {
                "module": "agents.optional",
                "class_name": "OptionalClient",
                "names": ["status"],
            }
        ],
    }
    agents_module = SimpleNamespace(__all__=[])
    optional_module = SimpleNamespace(OptionalClient=OptionalClient)
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else optional_module
        ),
    )
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: False)

    assert _validate_public_property_contract(contract, agents_module) == []

    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: True)

    assert _validate_public_property_contract(contract, agents_module) == [
        "agents.optional.OptionalClient.status removed or changed a released public property"
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
        contract_surface,
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
        contract_surface,
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


def test_public_api_contract_requires_real_export_bindings() -> None:
    contract: dict[str, Any] = {
        "required_top_level_exports": ["AgentsException"],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {},
    }
    agents_module = SimpleNamespace(__all__=["AgentsException"])

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
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
        contract_surface,
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
    monkeypatch.setattr(contract_surface, "_import_contract_module", raise_platform_error)

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
    monkeypatch.setattr(contract_surface, "_import_contract_module", import_platform_module)

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
    monkeypatch.setattr(contract_surface, "_import_contract_module", raise_unexpected_error)

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
    monkeypatch.setattr(contract_surface, "_import_contract_module", raise_foreign_error)

    errors = validate_released_api_contract(contract, agents_module=agents_module)
    assert len(errors) == 1
    assert errors[0].startswith("Failed to import released module agents.platform_specific:")


def test_public_api_contract_rejects_required_dataclass_suffix() -> None:
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
    agents_module = SimpleNamespace(__all__=[], ContractExample=Incompatible)

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "ContractExample.required_suffix added a required parameter",
        "ContractExample.required_suffix added a required dataclass field",
    ]


def test_callable_contract_tracks_pydantic_model_fields() -> None:
    class Model(BaseModel):
        required: str
        optional: int = 1
        generated: list[str] = Field(default_factory=list)

    Model.__signature__ = Signature([Parameter("data", kind=Parameter.VAR_KEYWORD)])
    callable_contract = _callable_contract(Model)

    assert callable_contract["parameters"] == [
        {"name": "data", "kind": "VAR_KEYWORD", "default": {"kind": "required"}}
    ]
    assert callable_contract["model_fields"] == [
        {"name": "required", "default": {"kind": "required"}},
        {
            "name": "optional",
            "default": {"kind": "literal", "type": "builtins.int", "value": 1},
        },
        {
            "name": "generated",
            "default": {"kind": "factory", "factory": "builtins.list"},
        },
    ]


def test_public_api_contract_validates_pydantic_model_fields() -> None:
    class Released(BaseModel):
        required: str
        optional: int = 1

    class Compatible(BaseModel):
        optional: int = 1
        required: str
        added_optional: bool = False

    class Renamed(BaseModel):
        renamed: str
        optional: int = 1

    class ChangedDefault(BaseModel):
        required: str
        optional: int = 2

    class AddedRequired(BaseModel):
        required: str
        optional: int = 1
        added_required: bool

    opaque_signature = Signature([Parameter("data", kind=Parameter.VAR_KEYWORD)])
    for model in (Released, Compatible, Renamed, ChangedDefault, AddedRequired):
        model.__signature__ = opaque_signature

    released_callable = _callable_contract(Released)
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": [],
        "canonical_imports": [],
        "callables": {"Model": released_callable},
    }

    def validate(model: type[BaseModel]) -> list[str]:
        return validate_released_api_contract(
            contract,
            agents_module=SimpleNamespace(__all__=[], Model=model),
        )

    assert validate(Compatible) == []
    assert validate(Renamed) == [
        "Model.required changed its released Pydantic model field contract: "
        "expected {'name': 'required', 'default': {'kind': 'required'}}, got None",
        "Model.renamed added a required Pydantic model field",
    ]
    assert validate(ChangedDefault) == [
        "Model.optional changed its released Pydantic model field contract: "
        "expected {'name': 'optional', 'default': {'kind': 'literal', "
        "'type': 'builtins.int', 'value': 1}}, got {'name': 'optional', "
        "'default': {'kind': 'literal', 'type': 'builtins.int', 'value': 2}}"
    ]
    assert validate(AddedRequired) == ["Model.added_required added a required Pydantic model field"]

    legacy_contract = {
        **contract,
        "callables": {
            "Model": {
                key: value for key, value in released_callable.items() if key != "model_fields"
            }
        },
    }
    assert (
        validate_released_api_contract(
            legacy_contract,
            agents_module=SimpleNamespace(__all__=[], Model=Renamed),
        )
        == []
    )


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
        contract_surface,
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


def test_release_contract_update_freezes_new_sdk_submodule_callable_without_canonical_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Existing:
        pass

    class NewPublic:
        def __init__(self, value: str, optional: int = 1) -> None:
            self.value = value
            self.optional = optional

    Existing.__module__ = "agents.submodule"
    NewPublic.__module__ = "agents.submodule"
    agents_module = SimpleNamespace(__all__=["NewPublic"], NewPublic=NewPublic)
    submodule = SimpleNamespace(
        __all__=["Existing", "NewPublic"], Existing=Existing, NewPublic=NewPublic
    )
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
        contract_surface,
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

    assert "agents.submodule.Existing" not in updated["callables"]
    assert updated["callables"]["NewPublic"] == _callable_contract(NewPublic)
    assert updated["callables"]["agents.submodule.NewPublic"] == _callable_contract(NewPublic)
    assert updated["canonical_imports"] == []

    unchanged = build_released_api_contract(
        updated,
        baseline="v0.20.0",
        baseline_commit="c" * 40,
        agents_module=agents_module,
    )
    assert unchanged["callables"]["agents.submodule.NewPublic"] == _callable_contract(NewPublic)

    class ChangedPublic:
        def __init__(self, value: str, optional: int = 1, *, required: int) -> None:
            self.value = value
            self.optional = optional
            self.required = required

    ChangedPublic.__module__ = "agents.submodule"
    submodule.NewPublic = ChangedPublic

    assert validate_released_api_contract(updated, agents_module=agents_module) == [
        "agents.submodule.NewPublic.required added a required parameter"
    ]


def test_release_contract_update_skips_new_third_party_submodule_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExternalPublic:
        pass

    ExternalPublic.__module__ = "external_package"
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["ExternalPublic"], ExternalPublic=ExternalPublic)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": [],
                "optional_bindings": {},
                "optional_exports": {},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_surface,
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

    assert "agents.submodule.ExternalPublic" not in updated["callables"]


def test_release_contract_update_preserves_tracked_submodule_callable_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OptionalPublic:
        def __init__(self, value: str) -> None:
            self.value = value

    OptionalPublic.__module__ = "agents.optional_parent"
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=[])
    callable_contract = _callable_contract(OptionalPublic)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents.optional_parent"],
        "required_submodule_exports": {
            "agents.optional_parent": {
                "names": ["OptionalPublic"],
                "optional_bindings": {},
                "optional_exports": {"OptionalPublic": "optional_backend"},
            }
        },
        "optional_dependency_unsupported_platforms": {"optional_backend": ["win32"]},
        "canonical_imports": [],
        "callables": {"agents.optional_parent.OptionalPublic": callable_contract},
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: False)
    monkeypatch.setattr(
        contract_surface,
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
        release_policy=_release_policy(
            {
                "agents.optional_parent": {
                    "optional_bindings": {},
                    "optional_exports": {"OptionalPublic": "optional_backend"},
                }
            },
            dependency_installations=(
                OptionalDependencyInstallation(
                    dependency_module="optional_backend",
                    extra="optional-provider",
                    unsupported_platforms=("win32",),
                ),
            ),
        ),
    )

    assert updated["callables"]["agents.optional_parent.OptionalPublic"] == callable_contract


def test_release_contract_update_preserves_tracked_submodule_callable_on_platform_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PlatformPublic:
        def __init__(self, value: str) -> None:
            self.value = value

    PlatformPublic.__module__ = "agents.platform_specific"
    agents_module = SimpleNamespace(__all__=[])
    callable_contract = _callable_contract(PlatformPublic)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents.platform_specific"],
        "required_submodule_exports": {
            "agents.platform_specific": {
                "names": ["PlatformPublic"],
                "optional_bindings": {},
                "optional_exports": {},
            }
        },
        "platform_import_errors": [
            {
                "module": "agents.platform_specific",
                "platforms": ["win32"],
                "error_type": "ImportError",
                "message_contains": "not supported on Windows",
            }
        ],
        "canonical_imports": [],
        "callables": {"agents.platform_specific.PlatformPublic": callable_contract},
    }

    def import_platform_module(module_name: str, _: Any) -> Any:
        assert module_name == "agents.platform_specific"
        raise ImportError("Backend is not supported on Windows.")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_surface, "_import_contract_module", import_platform_module)

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
    )

    assert updated["callables"]["agents.platform_specific.PlatformPublic"] == callable_contract


def test_release_contract_policy_preserves_new_optional_export_in_core_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = object()
    optional = object()
    agents_module = SimpleNamespace(__all__=[])
    full_submodule = SimpleNamespace(
        __all__=["Existing", "OptionalBackend"],
        Existing=existing,
        OptionalBackend=optional,
    )
    core_submodule = SimpleNamespace(__all__=["Existing"], Existing=existing)
    imported_submodule = full_submodule
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

    def import_module(module_name: str, _agents_module: object) -> object:
        return agents_module if module_name == "agents" else imported_submodule

    monkeypatch.setattr(contract_surface, "_import_contract_module", import_module)
    dependency_available = True
    monkeypatch.setattr(
        contract_surface,
        "_optional_dependency_is_available",
        lambda _module_name: dependency_available,
    )
    policy = {
        "agents.submodule": {
            "optional_bindings": {},
            "optional_exports": {"OptionalBackend": "missing_optional_backend_dependency"},
        }
    }

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(policy),
    )
    dependency_available = False
    imported_submodule = core_submodule

    assert updated["required_submodule_exports"]["agents.submodule"] == {
        "names": ["Existing", "OptionalBackend"],
        "optional_bindings": {},
        "optional_exports": {"OptionalBackend": "missing_optional_backend_dependency"},
    }
    assert validate_released_api_contract(updated, agents_module=agents_module) == []


def test_release_contract_policy_promotes_curated_public_state_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NewPublic:
        def __init__(self, value: str, optional: int = 1) -> None:
            self.value = value
            self.optional = optional

        @property
        def status(self) -> str:
            return "ready"

    class FactoryResult:
        @property
        def calls(self) -> tuple[str, ...]:
            return ()

    def create_result() -> FactoryResult:
        return FactoryResult()

    class PublicState(TypedDict, total=False):
        label: Required[str]
        count: int

    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(
        __all__=["NewPublic", "PublicState", "create_result"],
        NewPublic=NewPublic,
        PublicState=PublicState,
        create_result=create_result,
    )
    modules = {
        "agents": agents_module,
        "agents.submodule": submodule,
        "agents.submodule.impl": SimpleNamespace(NewPublic=NewPublic),
    }
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_properties": [],
        "public_typed_dicts": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: modules[module_name],
    )

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {"agents.submodule": {"optional_bindings": {}, "optional_exports": {}}},
            canonical_imports=(
                {
                    "canonical_module": "agents.submodule.impl",
                    "canonical_name": "NewPublic",
                    "module": "agents.submodule",
                    "name": "NewPublic",
                },
            ),
            public_class_contracts=(
                {
                    "abstract": False,
                    "class_name": "NewPublic",
                    "module": "agents.submodule",
                },
            ),
            public_properties=(
                {
                    "class_name": "NewPublic",
                    "module": "agents.submodule",
                    "names": ["status"],
                },
                {
                    "factory_name": "create_result",
                    "module": "agents.submodule",
                    "names": ["calls"],
                },
            ),
            public_typed_dicts=(
                {
                    "class_name": "PublicState",
                    "module": "agents.submodule",
                    "names": ["label", "count"],
                },
            ),
        ),
    )

    assert updated["canonical_imports"] == [
        {
            "canonical_module": "agents.submodule.impl",
            "canonical_name": "NewPublic",
            "module": "agents.submodule",
            "name": "NewPublic",
        }
    ]
    assert updated["public_properties"] == [
        {
            "class_name": "NewPublic",
            "module": "agents.submodule",
            "names": ["status"],
        },
        {
            "factory_name": "create_result",
            "module": "agents.submodule",
            "names": ["calls"],
        },
    ]
    assert updated["public_class_contracts"] == [
        {
            "abstract": False,
            "class_name": "NewPublic",
            "module": "agents.submodule",
        }
    ]
    assert updated["public_typed_dicts"] == [
        {
            "class_name": "PublicState",
            "fields": [
                {
                    "name": "label",
                    "required": True,
                    "annotation": "Required[str]",
                },
                {"name": "count", "required": False, "annotation": "int"},
            ],
            "module": "agents.submodule",
        }
    ]
    assert updated["callables"]["agents.submodule.NewPublic"] == _callable_contract(NewPublic)


def test_release_contract_promotion_rejects_missing_voice_concrete_state_policy() -> None:
    contract = load_api_contract(CONTRACT)
    policy = load_submodule_export_policy(CONTRACT.with_name("released_api_contract_policy.json"))
    omitted_classes = {"OpenAIVoiceModelProvider", "SingleAgentVoiceWorkflow"}
    incomplete_policy = replace(
        policy,
        public_class_contracts=tuple(
            entry
            for entry in policy.public_class_contracts
            if entry["class_name"] not in omitted_classes
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        build_released_api_contract(
            contract,
            baseline=contract["baseline"],
            baseline_commit=contract["baseline_commit"],
            release_policy=incomplete_policy,
        )

    message = str(exc_info.value)
    assert "Cannot promote the public Voice API" in message
    assert "OpenAIVoiceModelProvider" in message
    assert "SingleAgentVoiceWorkflow" in message
    assert "abstract': False" in message
    assert "canonical agents.voice imports" in message


def test_release_contract_promotion_rejects_new_public_voice_implementation_without_state_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PublicVoiceBase(abc.ABC):
        @abc.abstractmethod
        def run(self) -> None:
            pass

    class NewPublicVoiceImplementation(PublicVoiceBase):
        def run(self) -> None:
            pass

    class UnrelatedPublicClass:
        pass

    agents_module = SimpleNamespace(__all__=[])
    modules = {
        "agents.voice.base": SimpleNamespace(PublicVoiceBase=PublicVoiceBase),
        "agents.voice.implementation": SimpleNamespace(
            NewPublicVoiceImplementation=NewPublicVoiceImplementation,
            UnrelatedPublicClass=UnrelatedPublicClass,
        ),
    }
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: modules[module_name],
    )
    contract: dict[str, Any] = {
        "baseline": "v0.22.0",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "public_class_contracts": [],
        "public_properties": [],
        "public_type_aliases": [],
        "public_typed_dicts": [],
        "callables": {},
    }
    canonical_imports = (
        {
            "canonical_module": "agents.voice.base",
            "canonical_name": "PublicVoiceBase",
            "module": "agents.voice",
            "name": "PublicVoiceBase",
        },
        {
            "canonical_module": "agents.voice.implementation",
            "canonical_name": "NewPublicVoiceImplementation",
            "module": "agents.voice",
            "name": "NewPublicVoiceImplementation",
        },
        {
            "canonical_module": "agents.voice.implementation",
            "canonical_name": "UnrelatedPublicClass",
            "module": "agents.voice",
            "name": "UnrelatedPublicClass",
        },
    )
    release_policy = _release_policy(
        {},
        canonical_imports=canonical_imports,
        public_class_contracts=(
            {
                "abstract_members": ["run"],
                "class_name": "PublicVoiceBase",
                "module": "agents.voice.base",
            },
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        build_released_api_contract(
            contract,
            baseline="v0.22.1",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=release_policy,
        )

    message = str(exc_info.value)
    assert "NewPublicVoiceImplementation" in message
    assert "UnrelatedPublicClass" not in message


def test_typed_dict_only_promotion_updates_baseline_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PublicState(TypedDict, total=False):
        value: str

    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(PublicState=PublicState)
    contract: dict[str, Any] = {
        "baseline": "v0.20.0",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents", "agents.submodule"],
        "required_submodule_exports": {},
        "canonical_imports": [],
        "public_properties": [],
        "optional_dependency_unsupported_platforms": {},
        "submodule_export_exclusions": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: {
            "agents": agents_module,
            "agents.submodule": submodule,
        }[module_name],
    )

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {},
            public_typed_dicts=(
                {
                    "class_name": "PublicState",
                    "module": "agents.submodule",
                    "names": ["value"],
                },
            ),
        ),
    )

    assert updated["baseline_commit"] == "b" * 40


def test_release_contract_policy_honors_unsupported_platform_during_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PlatformBinding:
        def __init__(self, value: str) -> None:
            self.value = value

    agents_module = SimpleNamespace(__all__=[])
    platform_parent = SimpleNamespace(__all__=[])
    optional_parent = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": [
            "agents",
            "agents.platform_parent",
            "agents.platform_child",
        ],
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
                "canonical_module": "agents.platform_child",
                "canonical_name": "PlatformBinding",
                "module": "agents.platform_parent",
                "name": "PlatformBinding",
            }
        ],
        "callables": {
            "agents.platform_parent.PlatformBinding": _callable_contract(PlatformBinding)
        },
    }

    def import_module(module_name: str, _agents_module: object) -> object:
        if module_name == "agents":
            return agents_module
        if module_name == "agents.platform_parent":
            return platform_parent
        if module_name == "agents.platform_child":
            raise ImportError("Platform binding is not supported on Windows")
        if module_name == "agents.optional_parent":
            return optional_parent
        raise AssertionError(f"Unexpected import: {module_name}")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: False)
    monkeypatch.setattr(contract_surface, "_import_contract_module", import_module)

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {
                "agents.optional_parent": {
                    "optional_bindings": {},
                    "optional_exports": {"OptionalProvider": "optional_backend"},
                }
            },
            dependency_installations=(
                OptionalDependencyInstallation(
                    dependency_module="optional_backend",
                    extra="optional-provider",
                    unsupported_platforms=("win32",),
                ),
            ),
        ),
    )

    assert updated["optional_dependency_unsupported_platforms"] == {"optional_backend": ["win32"]}
    assert updated["canonical_imports"] == contract["canonical_imports"]
    assert updated["required_submodule_exports"]["agents.optional_parent"] == {
        "names": ["OptionalProvider"],
        "optional_bindings": {},
        "optional_exports": {"OptionalProvider": "optional_backend"},
    }
    assert updated["callables"]["agents.platform_parent.PlatformBinding"] == _callable_contract(
        PlatformBinding
    )
    assert "agents.optional_parent.OptionalProvider" not in updated["callables"]


def test_release_contract_policy_rejects_new_callable_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "callables": {},
    }

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: False)

    with pytest.raises(
        ValueError,
        match=(
            r"Cannot promote new canonical callable agents\.optional_parent\.OptionalProvider "
            r"because optional dependency 'optional_backend' is unsupported on 'win32'.*"
            r"release preparation host"
        ),
    ):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy(
                {
                    "agents.optional_parent": {
                        "optional_bindings": {},
                        "optional_exports": {"OptionalProvider": "optional_backend"},
                    }
                },
                dependency_installations=(
                    OptionalDependencyInstallation(
                        dependency_module="optional_backend",
                        extra="optional-provider",
                        unsupported_platforms=("win32",),
                    ),
                ),
                canonical_imports=(
                    {
                        "canonical_module": "agents.optional_impl",
                        "canonical_name": "OptionalProvider",
                        "module": "agents.optional_parent",
                        "name": "OptionalProvider",
                    },
                ),
            ),
        )


def test_release_contract_policy_promotes_new_uninspectable_canonical_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UninspectableMeta(type):
        @property
        def __signature__(cls) -> Signature:
            raise ValueError("signature unavailable")

    class Uninspectable(metaclass=UninspectableMeta):
        pass

    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["Uninspectable"], Uninspectable=Uninspectable)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "callables": {},
    }
    modules = {
        "agents": agents_module,
        "agents.submodule": submodule,
        "agents.submodule.impl": submodule,
    }
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: modules[module_name],
    )

    canonical_entry = {
        "canonical_module": "agents.submodule.impl",
        "canonical_name": "Uninspectable",
        "module": "agents.submodule",
        "name": "Uninspectable",
    }

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
        release_policy=_release_policy(
            {
                "agents.submodule": {
                    "optional_bindings": {},
                    "optional_exports": {},
                }
            },
            canonical_imports=(canonical_entry,),
        ),
    )

    assert updated["canonical_imports"] == [canonical_entry]
    assert "agents.submodule.Uninspectable" not in updated["callables"]


def test_release_contract_policy_keeps_existing_uninspectable_canonical_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UninspectableMeta(type):
        @property
        def __signature__(cls) -> Signature:
            raise ValueError("signature unavailable")

    class Uninspectable(metaclass=UninspectableMeta):
        pass

    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["Uninspectable"], Uninspectable=Uninspectable)
    canonical_entry = {
        "canonical_module": "agents.submodule.impl",
        "canonical_name": "Uninspectable",
        "module": "agents.submodule",
        "name": "Uninspectable",
    }
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents", "agents.submodule"],
        "canonical_imports": [canonical_entry],
        "callables": {},
    }
    modules = {
        "agents": agents_module,
        "agents.submodule": submodule,
        "agents.submodule.impl": submodule,
    }
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: modules[module_name],
    )

    updated = build_released_api_contract(
        contract,
        baseline="v0.20.0",
        baseline_commit="b" * 40,
        agents_module=agents_module,
    )

    assert updated["canonical_imports"] == [canonical_entry]
    assert "agents.submodule.Uninspectable" not in updated["callables"]


def test_public_api_contract_skips_optional_surface_on_frozen_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OptionalBackend:
        def __init__(self, value: str) -> None:
            self.value = value

    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalBackend"],
                "optional_bindings": {},
                "optional_exports": {"OptionalBackend": "optional_backend"},
            }
        },
        "optional_dependency_unsupported_platforms": {"optional_backend": ["win32"]},
        "canonical_imports": [
            {
                "canonical_module": "agents.submodule.impl",
                "canonical_name": "OptionalBackend",
                "module": "agents.submodule",
                "name": "OptionalBackend",
            }
        ],
        "callables": {"agents.submodule.OptionalBackend": _callable_contract(OptionalBackend)},
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: True)
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == []


def test_public_api_contract_allows_present_optional_surface_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    optional_export = object()
    optional_binding = object()
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(
        __all__=["OptionalExport", "OptionalBinding"],
        OptionalExport=optional_export,
        OptionalBinding=optional_binding,
    )
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalExport", "OptionalBinding"],
                "optional_bindings": {"OptionalBinding": "optional_binding_dependency"},
                "optional_exports": {"OptionalExport": "optional_export_dependency"},
            }
        },
        "optional_dependency_unsupported_platforms": {
            "optional_binding_dependency": ["win32"],
            "optional_export_dependency": ["win32"],
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: True)
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == []


def test_public_api_contract_rejects_dangling_optional_export_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["OptionalExport"])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalExport"],
                "optional_bindings": {},
                "optional_exports": {"OptionalExport": "optional_export_dependency"},
            }
        },
        "optional_dependency_unsupported_platforms": {"optional_export_dependency": ["win32"]},
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: True)
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Invalid released agents.submodule optional dependency declaration: "
        "'OptionalExport' remains in __all__ on an unsupported platform but its "
        "binding is unavailable"
    ]


def test_public_api_contract_rejects_dangling_optional_binding_on_unsupported_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["OptionalBinding"])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "required_submodule_exports": {
            "agents.submodule": {
                "names": ["OptionalBinding"],
                "optional_bindings": {"OptionalBinding": "optional_binding_dependency"},
                "optional_exports": {},
            }
        },
        "optional_dependency_unsupported_platforms": {"optional_binding_dependency": ["win32"]},
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: True)
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Invalid released agents.submodule optional dependency declaration: "
        "'OptionalBinding' remains in __all__ on an unsupported platform but its "
        "binding is unavailable"
    ]


def test_public_api_contract_requires_optional_surface_on_supported_platform(
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
                "optional_exports": {"OptionalBackend": "optional_backend"},
            }
        },
        "optional_dependency_unsupported_platforms": {"optional_backend": ["win32"]},
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(contract_surface, "_optional_dependency_is_available", lambda _name: True)
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Missing released agents.submodule exports: ['OptionalBackend']",
        "Missing released agents.submodule bindings: ['OptionalBackend']",
    ]


def test_release_contract_policy_rejects_unavailable_dependency_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents.submodule"],
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_surface,
        "_optional_dependency_is_available",
        lambda _module_name: False,
    )
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    with pytest.raises(
        ValueError,
        match="submodule export policy dependency modules are unavailable: "
        r"\['mistyped_dependency'\]",
    ):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy(
                {
                    "agents.submodule": {
                        "optional_bindings": {},
                        "optional_exports": {"OptionalBackend": "mistyped_dependency"},
                    }
                }
            ),
        )


def test_release_contract_policy_adds_new_public_optional_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    optional = object()
    agents_module = SimpleNamespace(__all__=[])
    submodule = SimpleNamespace(__all__=["OptionalBackend"], OptionalBackend=optional)
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_surface,
        "_optional_dependency_is_available",
        lambda _module_name: True,
    )
    monkeypatch.setattr(
        contract_surface,
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
        release_policy=_release_policy(
            {
                "agents.new_submodule": {
                    "optional_bindings": {},
                    "optional_exports": {"OptionalBackend": "optional_backend"},
                }
            }
        ),
    )

    assert updated["public_modules"] == ["agents", "agents.new_submodule"]
    assert updated["required_submodule_exports"]["agents.new_submodule"] == {
        "names": ["OptionalBackend"],
        "optional_bindings": {},
        "optional_exports": {"OptionalBackend": "optional_backend"},
    }


def test_release_contract_policy_rejects_unimportable_new_public_module() -> None:
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "callables": {},
    }

    with pytest.raises(
        ValueError,
        match=r"Cannot import submodule export policy module agents\.typo: ModuleNotFoundError",
    ):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy(
                {"agents.typo": {"optional_bindings": {}, "optional_exports": {}}}
            ),
        )


def test_release_contract_policy_rejects_new_module_outside_agents_package() -> None:
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "baseline": "v0.19.4",
        "baseline_commit": "a" * 40,
        "required_top_level_exports": [],
        "public_modules": ["agents"],
        "canonical_imports": [],
        "callables": {},
    }

    with pytest.raises(
        ValueError,
        match="new submodule export policy modules must be under the agents package: "
        r"\['external_package'\]",
    ):
        build_released_api_contract(
            contract,
            baseline="v0.20.0",
            baseline_commit="b" * 40,
            agents_module=agents_module,
            release_policy=_release_policy(
                {"external_package": {"optional_bindings": {}, "optional_exports": {}}}
            ),
        )


def test_load_submodule_export_policy_rejects_unknown_fields(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        '{"modules": {"agents.submodule": {"optional_export": {}}}, "optional_dependencies": {}}',
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match=r"submodule export policy for agents.submodule has unknown fields: "
        r"\['optional_export'\]",
    ):
        load_submodule_export_policy(policy_path)


def test_load_submodule_export_policy_requires_dependency_installations(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        '{"modules": {"agents.submodule": {"optional_exports": '
        '{"OptionalBackend": "optional_backend"}}}, "optional_dependencies": {}}',
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="submodule export policy dependencies are missing installation declarations: "
        r"\['optional_backend'\]",
    ):
        load_submodule_export_policy(policy_path)


def test_load_submodule_export_policy_collects_artifact_installations(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        '{"canonical_imports": [{"canonical_module": "agents.submodule.impl", '
        '"canonical_name": "ConditionalExport", "module": "agents.submodule", '
        '"name": "ConditionalExport"}], "modules": {"agents.submodule": {"optional_bindings": '
        '{"LazyBinding": "binding_dependency"}, "optional_exports": '
        '{"ConditionalExport": "export_dependency"}}}, "optional_dependencies": '
        '{"binding_dependency": {"requirement": "binding-package>=1"}, '
        '"export_dependency": {"extra": "export-extra"}}, "public_properties": '
        '[{"class_name": "ConditionalExport", '
        '"module": "agents.submodule", "names": ["status"]}, '
        '{"factory_name": "create_client", '
        '"module": "agents.submodule", "names": ["calls"]}], "public_class_contracts": '
        '[{"abstract": false, "class_name": "ConditionalExport", '
        '"module": "agents.submodule"}], "public_type_aliases": '
        '[{"module": "agents.submodule", "name": "PublicAlias"}], '
        '"public_typed_dicts": '
        '[{"class_name": "ClientState", "module": "agents.submodule", '
        '"names": ["status"]}]}',
        encoding="utf-8",
    )

    policy = load_submodule_export_policy(policy_path)

    assert policy.modules == {
        "agents.submodule": {
            "optional_bindings": {"LazyBinding": "binding_dependency"},
            "optional_exports": {"ConditionalExport": "export_dependency"},
        }
    }
    assert [asdict(installation) for installation in policy.dependency_installations] == [
        {
            "dependency_module": "binding_dependency",
            "extra": None,
            "requirement": "binding-package>=1",
            "unsupported_platforms": (),
        },
        {
            "dependency_module": "export_dependency",
            "extra": "export-extra",
            "requirement": None,
            "unsupported_platforms": (),
        },
    ]
    assert policy.canonical_imports == (
        {
            "canonical_module": "agents.submodule.impl",
            "canonical_name": "ConditionalExport",
            "module": "agents.submodule",
            "name": "ConditionalExport",
        },
    )
    assert policy.public_class_contracts == (
        {
            "abstract": False,
            "class_name": "ConditionalExport",
            "module": "agents.submodule",
        },
    )
    assert tuple(
        entry
        for entry in policy.public_properties
        if entry["module"]
        not in {
            "agents.testing.model",
            "agents.testing.sandbox",
            "agents.realtime.testing",
            "agents.voice.testing",
        }
    ) == (
        {
            "class_name": "ConditionalExport",
            "module": "agents.submodule",
            "names": ["status"],
        },
        {
            "factory_name": "create_client",
            "module": "agents.submodule",
            "names": ["calls"],
        },
    )
    assert policy.public_type_aliases == (
        {
            "module": "agents.submodule",
            "name": "PublicAlias",
        },
    )
    assert policy.public_typed_dicts == (
        {
            "class_name": "ClientState",
            "module": "agents.submodule",
            "names": ["status"],
        },
    )


def test_load_submodule_export_policy_rejects_invalid_class_abstract_state(
    tmp_path: Path,
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        '{"modules": {}, "optional_dependencies": {}, "public_class_contracts": '
        '[{"abstract": "false", "class_name": "Released", "module": "agents"}]}',
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="public_class_contracts abstract must be a boolean",
    ):
        load_submodule_export_policy(policy_path)


def test_load_submodule_export_policy_collects_unsupported_platforms(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        '{"modules": {"agents.submodule": {"optional_exports": '
        '{"ConditionalExport": "export_dependency"}}}, "optional_dependencies": '
        '{"export_dependency": {"extra": "export-extra", '
        '"unsupported_platforms": ["win32"]}}}',
        encoding="utf-8",
    )

    policy = load_submodule_export_policy(policy_path)

    assert policy.dependency_installations[0].unsupported_platforms == ("win32",)


def test_repository_release_policy_declares_v020_contract_surfaces() -> None:
    policy = load_submodule_export_policy(CONTRACT.with_name("released_api_contract_policy.json"))

    assert next(
        installation
        for installation in policy.dependency_installations
        if installation.dependency_module == "vercel"
    ).unsupported_platforms == ("win32",)
    assert {(entry["module"], entry["name"]) for entry in policy.canonical_imports} >= {
        ("agents.items", "InputItem"),
        ("agents.extensions.sandbox", "ModalCloudBucketMountStrategy"),
        ("agents.extensions.sandbox", "ModalSandboxClient"),
        ("agents.extensions.sandbox", "ModalSandboxClientOptions"),
        ("agents.extensions.sandbox", "RunloopAfterIdle"),
        ("agents.extensions.sandbox", "RunloopGatewaySpec"),
        ("agents.extensions.sandbox", "RunloopLaunchParameters"),
        ("agents.extensions.sandbox", "RunloopMcpSpec"),
        ("agents.extensions.sandbox", "RunloopSandboxClient"),
        ("agents.extensions.sandbox", "RunloopSandboxClientOptions"),
        ("agents.extensions.sandbox", "RunloopTunnelConfig"),
        ("agents.extensions.sandbox", "RunloopUserParameters"),
        ("agents.extensions.sandbox", "VercelSandboxClient"),
        ("agents.extensions.sandbox", "VercelSandboxClientOptions"),
    }
    assert tuple(
        entry
        for entry in policy.public_properties
        if entry["module"]
        not in {
            "agents.testing.model",
            "agents.testing.sandbox",
            "agents.realtime.testing",
            "agents.voice.model",
            "agents.voice.models.openai_model_provider",
            "agents.voice.models.openai_stt",
            "agents.voice.models.openai_tts",
            "agents.voice.testing",
        }
    ) == (
        {
            "class_name": "RunState",
            "module": "agents.run_state",
            "names": ["pending_input"],
        },
        {
            "class_name": "RetryPolicyContext",
            "module": "agents.retry",
            "names": ["response_started", "replay_safety", "stateful_request"],
        },
        {
            "class_name": "RunloopPlatformClient",
            "module": "agents.extensions.sandbox",
            "names": ["axons", "benchmarks", "blueprints", "network_policies", "secrets"],
        },
        {
            "class_name": "RunloopSandboxClient",
            "module": "agents.extensions.sandbox",
            "names": ["platform"],
        },
        {
            "class_name": "SandboxSessionState",
            "module": "agents.sandbox.session.sandbox_session_state",
            "names": ["mount_authority_redacted", "mount_authority_rebound"],
        },
        {
            "class_name": "ScriptedSandboxSession",
            "module": "agents.testing",
            "names": ["calls", "remaining_steps"],
        },
    )


def test_repository_release_policy_declares_public_optional_modules() -> None:
    policy = load_submodule_export_policy(CONTRACT.with_name("released_api_contract_policy.json"))
    documented_voice_modules = {
        "agents.voice.events",
        "agents.voice.exceptions",
        "agents.voice.input",
        "agents.voice.imports",
        "agents.voice.model",
        "agents.voice.models.openai_model_provider",
        "agents.voice.models.openai_stt",
        "agents.voice.models.openai_tts",
        "agents.voice.pipeline",
        "agents.voice.pipeline_config",
        "agents.voice.result",
        "agents.voice.testing",
        "agents.voice.utils",
        "agents.voice.workflow",
    }
    expected_modules = documented_voice_modules | {
        "agents.realtime.testing",
        "agents.testing",
        "agents.testing.model",
        "agents.testing.sandbox",
        "agents.voice",
    }

    documented_directive_modules = {
        line.removeprefix("::: ")
        for path in (CONTRACT.parents[2] / "docs" / "ref" / "voice").rglob("*.md")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("::: agents.voice")
    }

    assert documented_directive_modules == documented_voice_modules
    assert expected_modules <= policy.modules.keys()
    for module_name in documented_voice_modules - {
        "agents.voice.imports",
        "agents.voice.testing",
    }:
        assert policy.modules[module_name] == {
            "optional_bindings": {},
            "optional_exports": {},
        }
    for module_name in ("agents.voice", "agents.voice.imports", "agents.voice.testing"):
        assert policy.modules[module_name] == {
            "optional_bindings": {
                export: "numpy" for export in importlib.import_module(module_name).__all__
            },
            "optional_exports": {},
        }
    assert (
        next(
            installation
            for installation in policy.dependency_installations
            if installation.dependency_module == "numpy"
        ).extra
        == "voice"
    )


def test_repository_release_policy_declares_public_state_surfaces() -> None:
    policy = load_submodule_export_policy(CONTRACT.with_name("released_api_contract_policy.json"))
    _validate_voice_public_class_contract_policy(policy, None)
    expected_modules = {
        "agents.realtime.testing",
        "agents.testing",
        "agents.testing.model",
        "agents.testing.sandbox",
        "agents.voice.testing",
    }

    assert tuple(
        entry
        for entry in policy.public_properties
        if entry["module"]
        in {
            "agents.testing.model",
            "agents.testing.sandbox",
            "agents.realtime.testing",
            "agents.voice.testing",
        }
    ) == (
        {
            "class_name": "ScriptedModel",
            "module": "agents.testing.model",
            "names": ["calls", "remaining_steps", "first_call", "last_call"],
        },
        {
            "class_name": "ScriptedRealtimeModel",
            "module": "agents.realtime.testing",
            "names": ["listeners", "connect_calls", "sent_events", "remaining_steps"],
        },
        {
            "factory_name": "scripted_sandbox_session",
            "module": "agents.testing.sandbox",
            "names": ["calls", "remaining_steps"],
        },
        {
            "class_name": "ScriptedSTTModel",
            "module": "agents.voice.testing",
            "names": ["calls", "session_calls", "created_sessions"],
        },
        {
            "class_name": "ScriptedTTSModel",
            "module": "agents.voice.testing",
            "names": ["calls"],
        },
        {
            "class_name": "ScriptedVoiceWorkflow",
            "module": "agents.voice.testing",
            "names": ["transcriptions"],
        },
    )
    assert tuple(
        entry
        for entry in policy.public_properties
        if entry["module"]
        in {
            "agents.voice.model",
            "agents.voice.models.openai_model_provider",
            "agents.voice.models.openai_stt",
            "agents.voice.models.openai_tts",
        }
    ) == (
        {
            "class_name": "STTModel",
            "module": "agents.voice.model",
            "names": ["model_name"],
        },
        {
            "class_name": "TTSModel",
            "module": "agents.voice.model",
            "names": ["model_name"],
        },
        {
            "class_name": "OpenAISTTModel",
            "module": "agents.voice.models.openai_stt",
            "names": ["model_name"],
        },
        {
            "class_name": "OpenAITTSModel",
            "module": "agents.voice.models.openai_tts",
            "names": ["model_name"],
        },
        {
            "class_name": "OpenAIVoiceModelProvider",
            "module": "agents.voice.models.openai_model_provider",
            "names": ["agent_registration"],
        },
    )
    assert policy.public_class_contracts == (
        {
            "class_name": "STTModel",
            "module": "agents.voice.model",
            "abstract_members": ["create_session", "model_name", "transcribe"],
        },
        {
            "class_name": "StreamedTranscriptionSession",
            "module": "agents.voice.model",
            "abstract_members": ["close", "transcribe_turns"],
        },
        {
            "class_name": "TTSModel",
            "module": "agents.voice.model",
            "abstract_members": ["model_name", "run"],
        },
        {
            "class_name": "VoiceModelProvider",
            "module": "agents.voice.model",
            "abstract_members": ["get_stt_model", "get_tts_model"],
        },
        {
            "abstract": False,
            "class_name": "OpenAIVoiceModelProvider",
            "module": "agents.voice.models.openai_model_provider",
        },
        {
            "abstract": False,
            "class_name": "OpenAISTTModel",
            "module": "agents.voice.models.openai_stt",
        },
        {
            "abstract": False,
            "class_name": "OpenAISTTTranscriptionSession",
            "module": "agents.voice.models.openai_stt",
        },
        {
            "abstract": False,
            "class_name": "OpenAITTSModel",
            "module": "agents.voice.models.openai_tts",
        },
        {
            "class_name": "VoiceWorkflowBase",
            "module": "agents.voice.workflow",
            "abstract_members": ["run"],
        },
        {
            "abstract": False,
            "class_name": "SingleAgentVoiceWorkflow",
            "module": "agents.voice.workflow",
        },
    )
    assert policy.public_type_aliases == (
        {
            "module": "agents.voice.model",
            "name": "TTSVoice",
        },
        {
            "module": "agents.voice.events",
            "name": "VoiceStreamEvent",
        },
        {
            "module": "agents",
            "name": "OutputGuardrailBlockedMessageFormatter",
        },
    )
    type_aliases: dict[tuple[str, str], dict[str, Any]] = {
        (cast(str, entry["module"]), cast(str, entry["name"])): cast(
            dict[str, Any], entry["definition"]
        )
        for entry in contract_surface._public_type_alias_contract(policy.public_type_aliases, None)
    }
    tts_voice = type_aliases[("agents.voice.model", "TTSVoice")]
    assert tts_voice["kind"] == "union"
    assert [
        value["value"]
        for member in tts_voice["members"]
        if member["kind"] == "literal"
        for value in member["values"]
    ] == [
        "alloy",
        "ash",
        "ballad",
        "cedar",
        "coral",
        "echo",
        "fable",
        "marin",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
    ]
    assert {member["identity"] for member in tts_voice["members"] if member["kind"] == "type"} == {
        "agents.voice.model.TTSCustomVoice"
    }
    voice_stream_event = type_aliases[("agents.voice.events", "VoiceStreamEvent")]
    assert voice_stream_event["kind"] == "union"
    assert {member["identity"] for member in voice_stream_event["members"]} == {
        "agents.voice.events.VoiceStreamEventAudio",
        "agents.voice.events.VoiceStreamEventError",
        "agents.voice.events.VoiceStreamEventLifecycle",
    }
    blocked_message_formatter = type_aliases[("agents", "OutputGuardrailBlockedMessageFormatter")]
    assert blocked_message_formatter == {
        "kind": "callable",
        "parameters": [
            {
                "arguments": [{"kind": "any"}],
                "kind": "generic",
                "origin": "agents.run_config.OutputGuardrailBlockedMessageArgs",
            }
        ],
        "return": {
            "kind": "union",
            "members": [
                {"identity": "builtins.NoneType", "kind": "type"},
                {"identity": "builtins.str", "kind": "type"},
            ],
        },
    }
    assert policy.public_typed_dicts == (
        {
            "class_name": "ModelStepSpec",
            "module": "agents.testing.model",
            "names": [
                "output",
                "usage",
                "response_id",
                "request_id",
                "raw_usage",
                "error",
                "responder",
                "stream_events",
                "retry_advice",
            ],
        },
        {
            "class_name": "SandboxStepSpec",
            "module": "agents.testing.sandbox",
            "names": ["method", "match", "result", "responder", "error"],
        },
        {
            "class_name": "RealtimeConnectCall",
            "module": "agents.realtime.testing",
            "names": [
                "api_key_provided",
                "headers_provided",
                "url",
                "initial_model_settings",
                "playback_tracker",
                "call_id",
            ],
        },
        {
            "class_name": "TTSCustomVoice",
            "module": "agents.voice",
            "names": ["id"],
        },
    )
    for module_name in expected_modules:
        module = importlib.import_module(module_name)
        assert module.__all__
        assert all(type(export) is str for export in module.__all__)

    expected_canonical_imports = {
        ("agents.testing", name, "agents.testing.model", name)
        for name in importlib.import_module("agents.testing.model").__all__
    } | {
        ("agents.testing", name, "agents.testing.sandbox", name)
        for name in importlib.import_module("agents.testing.sandbox").__all__
    }
    actual_canonical_imports = {
        (
            entry["module"],
            entry["name"],
            entry["canonical_module"],
            entry["canonical_name"],
        )
        for entry in policy.canonical_imports
        if entry["module"] == "agents.testing"
    }

    assert actual_canonical_imports == expected_canonical_imports
    for module_name, name, canonical_module_name, canonical_name in actual_canonical_imports:
        module = importlib.import_module(module_name)
        canonical_module = importlib.import_module(canonical_module_name)
        assert getattr(module, name) is getattr(canonical_module, canonical_name)

    voice_canonical_modules = {
        "AudioInput": "agents.voice.input",
        "StreamedAudioInput": "agents.voice.input",
        "STTModel": "agents.voice.model",
        "STTModelSettings": "agents.voice.model",
        "TTSCustomVoice": "agents.voice.model",
        "TTSModel": "agents.voice.model",
        "TTSModelSettings": "agents.voice.model",
        "TTSVoice": "agents.voice.model",
        "VoiceModelProvider": "agents.voice.model",
        "StreamedAudioResult": "agents.voice.result",
        "SingleAgentVoiceWorkflow": "agents.voice.workflow",
        "OpenAIVoiceModelProvider": "agents.voice.models.openai_model_provider",
        "OpenAISTTModel": "agents.voice.models.openai_stt",
        "OpenAITTSModel": "agents.voice.models.openai_tts",
        "VoiceStreamEventAudio": "agents.voice.events",
        "VoiceStreamEventError": "agents.voice.events",
        "VoiceStreamEventLifecycle": "agents.voice.events",
        "VoiceStreamEvent": "agents.voice.events",
        "VoicePipeline": "agents.voice.pipeline",
        "VoicePipelineConfig": "agents.voice.pipeline_config",
        "get_sentence_based_splitter": "agents.voice.utils",
        "VoiceWorkflowHelper": "agents.voice.workflow",
        "VoiceWorkflowBase": "agents.voice.workflow",
        "SingleAgentWorkflowCallbacks": "agents.voice.workflow",
        "StreamedTranscriptionSession": "agents.voice.model",
        "OpenAISTTTranscriptionSession": "agents.voice.models.openai_stt",
        "STTWebsocketConnectionError": "agents.voice.exceptions",
    }
    expected_voice_canonical_imports = {
        ("agents.voice", name, canonical_module_name, name)
        for name, canonical_module_name in voice_canonical_modules.items()
    }
    actual_voice_canonical_imports = {
        (
            entry["module"],
            entry["name"],
            entry["canonical_module"],
            entry["canonical_name"],
        )
        for entry in policy.canonical_imports
        if entry["module"] == "agents.voice"
    }

    assert actual_voice_canonical_imports == expected_voice_canonical_imports
    for module_name, name, canonical_module_name, canonical_name in actual_voice_canonical_imports:
        module = importlib.import_module(module_name)
        canonical_module = importlib.import_module(canonical_module_name)
        assert getattr(module, name) is getattr(canonical_module, canonical_name)


def test_voice_testing_start_sentinel_has_stable_contract_identity() -> None:
    from agents.voice.testing import _START_NOT_CONFIGURED

    assert _default_contract(_START_NOT_CONFIGURED) == {
        "kind": "sentinel",
        "identity": "agents.voice.testing._START_NOT_CONFIGURED",
    }
    with pytest.raises(TypeError, match="Unsupported public API default value: builtins.object"):
        _default_contract(object())


def test_default_contract_does_not_import_voice_testing_for_unrelated_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "agents.voice.testing":
            raise AssertionError("Unrelated defaults must not import the optional Voice package.")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert _default_contract(()) == {
        "kind": "sequence",
        "type": "builtins.tuple",
        "items": [],
    }


@pytest.mark.parametrize(
    "unsupported_platforms",
    ["win32", [""], ["win32", "win32"]],
)
def test_load_submodule_export_policy_rejects_invalid_unsupported_platforms(
    tmp_path: Path, unsupported_platforms: object
) -> None:
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "modules": {
                    "agents.submodule": {
                        "optional_exports": {"ConditionalExport": "export_dependency"}
                    }
                },
                "optional_dependencies": {
                    "export_dependency": {
                        "extra": "export-extra",
                        "unsupported_platforms": unsupported_platforms,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be a list of unique non-empty strings"):
        load_submodule_export_policy(policy_path)


@pytest.mark.parametrize(
    "protected_path",
    [
        CONTRACT,
        CONTRACT.with_name("released_api_contract_policy.json"),
    ],
)
def test_prospective_output_rejects_contract_input_path(protected_path: Path) -> None:
    root = CONTRACT.parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / ".github" / "scripts" / "update_released_api_contract.py"),
            "--version",
            version("openai-agents"),
            "--output",
            str(protected_path),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert result.stderr.strip() == (
        "--output must not overwrite released API contract inputs: "
        "tests/fixtures/released_api_contract.json or "
        "tests/fixtures/released_api_contract_policy.json"
    )


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
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == []


def test_public_api_contract_skips_fully_optional_unimportable_submodule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_module = SimpleNamespace(__all__=[])
    contract: dict[str, Any] = {
        "required_top_level_exports": [],
        "public_modules": ["agents.optional_submodule"],
        "required_submodule_exports": {
            "agents.optional_submodule": {
                "names": ["OptionalClient", "OptionalConfig"],
                "optional_bindings": {
                    "OptionalClient": "missing_optional_dependency",
                    "OptionalConfig": "missing_optional_dependency",
                },
                "optional_exports": {},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }

    def import_module(module_name: str, _agents_module: object) -> object:
        if module_name == "agents":
            return agents_module
        raise ImportError("The optional dependency is unavailable.")

    monkeypatch.setattr(contract_surface, "_import_contract_module", import_module)

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
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == []


def test_public_api_contract_rejects_optional_export_that_remains_in_all(
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
                "optional_bindings": {},
                "optional_exports": {"OptionalBackend": "missing_optional_backend_dependency"},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Invalid released agents.submodule optional dependency declaration: "
        "'OptionalBackend' remains in __all__ but its binding is unavailable; "
        "declare it in optional_bindings instead of optional_exports"
    ]


def test_public_api_contract_rejects_optional_binding_absent_from_all(
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
                "optional_bindings": {"OptionalBackend": "missing_optional_backend_dependency"},
                "optional_exports": {},
            }
        },
        "canonical_imports": [],
        "callables": {},
    }
    monkeypatch.setattr(
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Invalid released agents.submodule optional dependency declaration: "
        "'OptionalBackend' is absent from __all__; declare it in optional_exports "
        "instead of optional_bindings",
        "Missing released agents.submodule exports: ['OptionalBackend']",
    ]


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
        contract_surface,
        "_import_contract_module",
        lambda module_name, _agents_module: (
            agents_module if module_name == "agents" else submodule
        ),
    )

    assert validate_released_api_contract(contract, agents_module=agents_module) == [
        "Missing released agents.submodule exports: ['OptionalBackend']",
        "Missing released agents.submodule bindings: ['OptionalBackend']",
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
        contract_surface,
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
        contract_surface,
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
        contract_surface,
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
