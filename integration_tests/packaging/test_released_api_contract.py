import os
from importlib.metadata import metadata, requires, version
from importlib.util import find_spec
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from integration_tests._contract_support import (
    load_api_contract,
    validate_released_api_contract,
)

pytestmark = pytest.mark.packaging

CONTRACT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "released_api_contract.json"
PROSPECTIVE_CONTRACT_ENV = "OPENAI_AGENTS_PROSPECTIVE_RELEASE_CONTRACT"
REQUIRED_OPTIONAL_DEPENDENCIES_ENV = "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES"
OPTIONAL_DEPENDENCY_INSTALLATION_ENV = "OPENAI_AGENTS_INTEGRATION_OPTIONAL_DEPENDENCY_INSTALLATION"
REQUIRED_OPTIONAL_EXTRA_ENV = "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_EXTRA"


def _distributions_declared_by_extra(requirement_strings: list[str], extra: str) -> set[str]:
    no_extra = "__openai_agents_no_extra__"
    declared: set[str] = set()
    for requirement_string in requirement_strings:
        requirement = Requirement(requirement_string)
        marker = requirement.marker
        if (
            marker is not None
            and marker.evaluate({"extra": extra})
            and not marker.evaluate({"extra": no_extra})
        ):
            declared.add(canonicalize_name(requirement.name))
    return declared


def _extra_metadata_error(
    *,
    extra: str,
    dependency_module: str,
    provided_extras: list[str],
    requirement_strings: list[str],
) -> str | None:
    canonical_extra = canonicalize_name(extra)
    if canonical_extra not in {
        canonicalize_name(provided_extra) for provided_extra in provided_extras
    }:
        return (
            f"The installed openai-agents artifact does not provide policy extra {extra!r}. "
            "Correct its entry in tests/fixtures/released_api_contract_policy.json or add "
            "the extra under [project.optional-dependencies]."
        )

    distribution_name = canonicalize_name(dependency_module)
    declared_distributions = _distributions_declared_by_extra(requirement_strings, extra)
    if distribution_name not in declared_distributions:
        return (
            f"The installed openai-agents artifact extra {extra!r} does not declare "
            f"distribution {distribution_name!r} for policy dependency module "
            f"{dependency_module!r}. Add it to [project.optional-dependencies].{extra}; "
            "transitive or base-environment availability does not satisfy this check."
        )
    return None


def _prospective_contract_failure_message(errors: list[str]) -> str:
    details = "\n".join(f"- {error}" for error in errors)
    return (
        "Prospective release API contract check failed before release preparation.\n"
        "The installed distribution does not match the generated public API contract:\n"
        f"{details}\n\n"
        "If a missing name is absent from the clean module's `__all__` because it depends "
        "on an optional extra, add it to `tests/fixtures/released_api_contract_policy.json` "
        "under the module's `optional_exports` mapping. If the name remains in `__all__` "
        "but resolving its binding requires the optional dependency, add it under "
        "`optional_bindings` instead. If the export is required, make its defining module "
        "importable without the optional package. If the optional dependency package itself "
        "does not support this runner platform, verify that upstream limitation and add the "
        "platform to its `unsupported_platforms` list under `optional_dependencies`; do not "
        "exclude SDK-owned platform failures. Then run `make sync` and "
        "`make check-prospective-released-api-contract` again."
    )


@pytest.mark.packaging_dependency
def test_artifact_installation_provides_its_declared_optional_dependencies() -> None:
    configured_dependencies = os.environ.get(REQUIRED_OPTIONAL_DEPENDENCIES_ENV)
    if not configured_dependencies:
        return

    installation = os.environ.get(OPTIONAL_DEPENDENCY_INSTALLATION_ENV, "configured installation")
    dependency_modules = [
        module_name.strip()
        for module_name in configured_dependencies.split(",")
        if module_name.strip()
    ]
    missing_dependencies = [
        module_name for module_name in dependency_modules if find_spec(module_name) is None
    ]
    if missing_dependencies:
        pytest.fail(
            f"The artifact environment for {installation} does not provide declared "
            f"optional dependency modules {missing_dependencies!r}. Update the corresponding "
            "openai-agents extra or policy requirement."
        )


@pytest.mark.packaging_dependency
def test_artifact_extra_declares_its_policy_dependency() -> None:
    extra = os.environ.get(REQUIRED_OPTIONAL_EXTRA_ENV)
    if not extra:
        return

    configured_dependencies = os.environ.get(REQUIRED_OPTIONAL_DEPENDENCIES_ENV, "")
    dependency_modules = [
        module_name.strip()
        for module_name in configured_dependencies.split(",")
        if module_name.strip()
    ]
    if len(dependency_modules) != 1:
        pytest.fail(
            f"Extra provenance validation requires exactly one dependency module, got "
            f"{dependency_modules!r}. Fix the prospective-contract runner configuration."
        )
    dependency_module = dependency_modules[0]
    error = _extra_metadata_error(
        extra=extra,
        dependency_module=dependency_module,
        provided_extras=metadata("openai-agents").get_all("Provides-Extra") or [],
        requirement_strings=requires("openai-agents") or [],
    )
    if error is not None:
        pytest.fail(error)


def test_extra_metadata_provenance_ignores_base_and_transitive_requirements() -> None:
    assert _distributions_declared_by_extra(
        [
            "cryptography>=45",
            "pyjwt[crypto]>=2; python_version >= '3.10'",
            "redis>=7; extra == 'redis'",
            "cryptography>=45; extra == 'encrypt'",
        ],
        "encrypt",
    ) == {"cryptography"}

    assert _extra_metadata_error(
        extra="encrypt",
        dependency_module="cryptography",
        provided_extras=["encrypt"],
        requirement_strings=[
            "cryptography>=45",
            "pyjwt[crypto]>=2; python_version >= '3.10'",
        ],
    ) == (
        "The installed openai-agents artifact extra 'encrypt' does not declare distribution "
        "'cryptography' for policy dependency module 'cryptography'. Add it to "
        "[project.optional-dependencies].encrypt; transitive or base-environment availability "
        "does not satisfy this check."
    )


def test_extra_metadata_provenance_rejects_unknown_extra() -> None:
    assert _extra_metadata_error(
        extra="missing",
        dependency_module="cryptography",
        provided_extras=["encrypt"],
        requirement_strings=["cryptography>=45; extra == 'encrypt'"],
    ) == (
        "The installed openai-agents artifact does not provide policy extra 'missing'. Correct "
        "its entry in tests/fixtures/released_api_contract_policy.json or add the extra under "
        "[project.optional-dependencies]."
    )


@pytest.mark.packaging_dependency
def test_installed_distribution_preserves_released_public_api_contract() -> None:
    contract = load_api_contract(CONTRACT)
    assert contract["baseline"] == f"v{version('openai-agents')}"
    assert len(contract["baseline_commit"]) == 40

    errors = validate_released_api_contract(contract)

    assert errors == []


@pytest.mark.packaging_dependency
def test_installed_distribution_is_ready_for_prospective_release_contract() -> None:
    configured_path = os.environ.get(PROSPECTIVE_CONTRACT_ENV)
    if not configured_path:
        return

    path = Path(configured_path)
    if not path.is_file():
        pytest.fail(
            f"Prospective release API contract does not exist: {path}. "
            "Run `make check-prospective-released-api-contract` from the repository root."
        )

    contract = load_api_contract(path)
    errors = validate_released_api_contract(contract)
    if errors:
        pytest.fail(_prospective_contract_failure_message(errors))


def test_prospective_contract_failure_guidance_distinguishes_optional_shapes() -> None:
    message = _prospective_contract_failure_message(
        [
            "Missing released agents.example exports: ['ConditionalExport']",
            "Missing released agents.example bindings: ['LazyBinding']",
        ]
    )

    assert "absent from the clean module's `__all__`" in message
    assert "`optional_exports`" in message
    assert "remains in `__all__`" in message
    assert "`optional_bindings` instead" in message
    assert "`unsupported_platforms`" in message
    assert "do not exclude SDK-owned platform failures" in message
    assert "make check-prospective-released-api-contract" in message
