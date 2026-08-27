from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.packaging


def test_wheel_excludes_integration_tests_and_contains_runtime_modules() -> None:
    wheel = Path(os.environ["OPENAI_AGENTS_INTEGRATION_WHEEL"])
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()

    assert not any(Path(member).parts[0] == "integration_tests" for member in members)
    assert "agents/py.typed" in members
    assert "agents/realtime/session.py" in members
    assert "agents/voice/pipeline.py" in members
    assert "agents/extensions/models/any_llm_model.py" in members
    assert "agents/extensions/models/litellm_model.py" in members
    assert "agents/extensions/experimental/hosted_multi_agent/model.py" in members


def test_source_distribution_excludes_repository_automation_and_local_caches() -> None:
    source_distribution = Path(os.environ["OPENAI_AGENTS_INTEGRATION_SDIST"])
    with tarfile.open(source_distribution, "r:gz") as archive:
        members = archive.getnames()

    assert not any(
        len(Path(member).parts) > 1
        and (
            Path(member).parts[1] in {".agents", ".github", "integration_tests"}
            or Path(member).parts[1].startswith((".tmp", ".uv"))
        )
        for member in members
    )
    assert any(member.endswith("/src/agents/py.typed") for member in members)


def test_installed_distribution_advertises_expected_optional_extras() -> None:
    distribution = importlib.metadata.distribution("openai-agents")
    extras = set(distribution.metadata.get_all("Provides-Extra") or [])

    assert {
        "any-llm",
        "encrypt",
        "litellm",
        "realtime",
        "redis",
        "s3",
        "sqlalchemy",
        "viz",
        "voice",
    }.issubset(extras)
    assert distribution.version


@pytest.mark.parametrize(
    "module_name",
    [
        "agents",
        "agents.models.openai_responses",
        "agents.models.openai_chatcompletions",
        "agents.decorators",
        "agents.guardrail",
        "agents.handoffs",
        "agents.memory",
        "agents.model_settings",
        "agents.realtime",
        "agents.responses_websocket_session",
        "agents.run",
        "agents.run_config",
        "agents.tool",
        "agents.tool_guardrails",
        "agents.tracing",
        "agents.extensions.experimental.hosted_multi_agent",
    ],
)
def test_public_runtime_modules_import_from_the_distribution(module_name: str) -> None:
    module = importlib.import_module(module_name)

    assert module.__file__ is not None
    assert "site-packages" in Path(module.__file__).parts


@pytest.mark.parametrize(
    ("module_name", "export_name", "canonical_module", "canonical_name"),
    [
        ("agents.decorators", "function_tool", "agents", "function_tool"),
        ("agents.decorators", "tool", "agents", "function_tool"),
        ("agents.decorators", "input_guardrail", "agents", "input_guardrail"),
        ("agents.decorators", "output_guardrail", "agents", "output_guardrail"),
        ("agents.decorators", "tool_input_guardrail", "agents", "tool_input_guardrail"),
        ("agents.decorators", "tool_output_guardrail", "agents", "tool_output_guardrail"),
        ("agents.agent", "Agent", "agents", "Agent"),
        ("agents.run", "Runner", "agents", "Runner"),
        ("agents.run_config", "RunConfig", "agents", "RunConfig"),
        ("agents.model_settings", "ModelSettings", "agents", "ModelSettings"),
        ("agents.guardrail", "input_guardrail", "agents", "input_guardrail"),
        ("agents.tool", "function_tool", "agents", "function_tool"),
        ("agents.tool_guardrails", "tool_input_guardrail", "agents", "tool_input_guardrail"),
        ("agents.memory", "SQLiteSession", "agents", "SQLiteSession"),
        ("agents.memory.sqlite_session", "SQLiteSession", "agents", "SQLiteSession"),
        (
            "agents.responses_websocket_session",
            "ResponsesWebSocketSession",
            "agents",
            "ResponsesWebSocketSession",
        ),
        ("agents.tracing", "TracingProcessor", "agents", "TracingProcessor"),
        (
            "agents.realtime.model_events",
            "RealtimeModelUsageEvent",
            "agents.realtime",
            "RealtimeModelUsageEvent",
        ),
    ],
)
def test_supported_import_paths_resolve_to_canonical_runtime_objects(
    module_name: str,
    export_name: str,
    canonical_module: str,
    canonical_name: str,
) -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", DeprecationWarning)
        module = importlib.import_module(module_name)
        canonical = importlib.import_module(canonical_module)
        actual = getattr(module, export_name)

    assert actual is getattr(canonical, canonical_name)
    assert not any(isinstance(warning.message, DeprecationWarning) for warning in captured)


def test_decorators_module_exports_supported_runtime_aliases() -> None:
    decorators = importlib.import_module("agents.decorators")

    assert decorators.__all__ == [
        "function_tool",
        "input_guardrail",
        "output_guardrail",
        "tool",
        "tool_input_guardrail",
        "tool_output_guardrail",
    ]
    assert decorators.tool is decorators.function_tool

    def legacy_status() -> str:
        """Return the supported legacy decorator status."""
        return "LEGACY_DECORATOR_READY"

    decorated_status = decorators.tool(legacy_status)
    assert decorated_status.name == "legacy_status"


@pytest.mark.parametrize(
    ("module_name", "dependency_name", "expected_extra"),
    [
        ("agents.extensions.models.any_llm_model", "any_llm", "any-llm"),
        ("agents.extensions.models.litellm_model", "litellm", "litellm"),
    ],
)
def test_optional_provider_modules_fail_with_actionable_install_guidance(
    module_name: str, dependency_name: str, expected_extra: str
) -> None:
    if importlib.util.find_spec(dependency_name) is not None:
        pytest.skip(f"{dependency_name} is already installed in this isolated environment.")

    sys.modules.pop(module_name, None)
    with pytest.raises(ImportError, match=rf"openai-agents\[{expected_extra}\]"):
        importlib.import_module(module_name)
