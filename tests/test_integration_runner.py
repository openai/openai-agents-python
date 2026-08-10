from __future__ import annotations

import os
import runpy
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

RUNNER = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "run_integration_tests.py"
CHANGE_DETECTOR = RUNNER.with_name("detect-changes.sh")
INTEGRATION_CONFTEST = RUNNER.parents[2] / "integration_tests" / "conftest.py"


def _sanitizer() -> Callable[[Path], Any]:
    return cast(Callable[[Path], Any], runpy.run_path(str(RUNNER))["_sanitize_and_load_junit"])


def _run_suite() -> Callable[..., None]:
    return cast(Callable[..., None], runpy.run_path(str(RUNNER))["run_suite"])


def test_junit_sanitizer_removes_failure_details_and_captured_output(tmp_path: Path) -> None:
    sentinel = "JUNIT_SECRET_SENTINEL_42"
    report = tmp_path / "results.xml"
    report.write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite tests="4" failures="1" errors="1" skipped="1">
<properties><property name="suite-token" value="{sentinel}" /></properties>
<testcase classname="contract" name="passed" />
<testcase classname="contract" name="failed">
<properties><property name="case-token" value="{sentinel}" /></properties>
<failure message="{sentinel}">traceback {sentinel}</failure>
<system-out>stdout {sentinel}</system-out><system-err>stderr {sentinel}</system-err>
</testcase>
<testcase classname="contract" name="errored">
<error message="{sentinel}">exception {sentinel}</error>
</testcase>
<testcase classname="contract" name="skipped">
<skipped message="{sentinel}">reason {sentinel}</skipped>
</testcase></testsuite></testsuites>
""",
        encoding="utf-8",
    )

    root = _sanitizer()(report)

    assert root is not None
    serialized = report.read_text(encoding="utf-8")
    assert sentinel not in serialized
    assert 'name="suite-0"' in serialized
    assert [case.attrib for case in root.findall("testsuite/testcase")] == [
        {"name": "case-0"},
        {"name": "case-1"},
        {"name": "case-2"},
        {"name": "case-3"},
    ]
    assert "properties" not in serialized


def test_junit_sanitizer_rebuilds_only_safe_count_and_outcome_structure(tmp_path: Path) -> None:
    sentinel = "JUNIT_UNKNOWN_CARRIER_SECRET_42"
    report = tmp_path / "results.xml"
    report.write_text(
        f'<testsuites token="{sentinel}">{sentinel}'
        f'<testsuite name="{sentinel}" hostname="{sentinel}" tests="2" failures="1" '
        f'errors="0" skipped="0">{sentinel}'
        f'<unknown token="{sentinel}">{sentinel}</unknown>'
        f'<testcase name="{sentinel}" classname="{sentinel}" time="{sentinel}">'
        f"{sentinel}</testcase>{sentinel}"
        f'<testcase name="{sentinel}" custom="{sentinel}">'
        f'<failure custom="{sentinel}">{sentinel}</failure>'
        f"<unknown>{sentinel}</unknown></testcase></testsuite>{sentinel}</testsuites>",
        encoding="utf-8",
    )

    root = _sanitizer()(report)

    assert root is not None
    assert report.read_text(encoding="utf-8") == (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        '<testsuites><testsuite name="suite-0" tests="2" failures="1" errors="0" '
        'skipped="0"><testcase name="case-0" /><testcase name="case-1"><failure />'
        "</testcase></testsuite></testsuites>"
    )


def test_junit_sanitizer_discards_malformed_reports(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text("<testsuite><failure>secret", encoding="utf-8")

    assert _sanitizer()(report) is None
    assert not report.exists()


def test_junit_sanitizer_preserves_single_suite_counts(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite name="source" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase name="passed" /></testsuite>',
        encoding="utf-8",
    )

    root = _sanitizer()(report)

    assert root is not None
    assert root.tag == "testsuite"
    assert root.attrib == {
        "name": "suite-0",
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "0",
    }
    assert [case.attrib for case in root.findall("testcase")] == [{"name": "case-0"}]


def test_junit_sanitizer_discards_reports_with_invalid_counts(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite tests="not-a-number" failures="0" errors="0" skipped="0" />',
        encoding="utf-8",
    )

    assert _sanitizer()(report) is None
    assert not report.exists()


def test_junit_sanitizer_discards_reports_with_impossible_totals(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite tests="1" failures="1" errors="1" skipped="0" />',
        encoding="utf-8",
    )

    assert _sanitizer()(report) is None
    assert not report.exists()


@pytest.mark.parametrize(
    "suite",
    [
        '<testsuite tests="3" failures="0" errors="0" skipped="0">'
        '<testcase name="only" /></testsuite>',
        '<testsuite tests="1" failures="1" errors="0" skipped="0">'
        '<testcase name="passed" /></testsuite>',
    ],
)
def test_junit_sanitizer_discards_declared_actual_count_mismatches(
    tmp_path: Path,
    suite: str,
) -> None:
    report = tmp_path / "results.xml"
    report.write_text(suite, encoding="utf-8")

    assert _sanitizer()(report) is None
    assert not report.exists()


def test_junit_sanitizer_discards_duplicate_terminal_outcomes(tmp_path: Path) -> None:
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite tests="1" failures="1" errors="0" skipped="0">'
        '<testcase name="failed"><failure /><failure /></testcase></testsuite>',
        encoding="utf-8",
    )

    assert _sanitizer()(report) is None
    assert not report.exists()


@pytest.mark.parametrize("invalid_report", [False, True], ids=["missing", "invalid"])
def test_successful_integration_run_requires_valid_junit_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_report: bool,
) -> None:
    run_suite = _run_suite()

    def fake_run_pytest(command: list[str], *, env: dict[str, str]) -> tuple[int, str]:
        _ = env
        if invalid_report:
            result_path = Path(command[-1].removeprefix("--junitxml="))
            result_path.write_text("<testsuite>", encoding="utf-8")
        return 0, "1 passed"

    monkeypatch.setitem(run_suite.__globals__, "RESULTS", tmp_path)
    monkeypatch.setitem(run_suite.__globals__, "run_pytest", fake_run_pytest)

    with pytest.raises(RuntimeError, match="did not produce a valid JUnit report"):
        run_suite(
            tmp_path / "python",
            tmp_path / "candidate.whl",
            tmp_path / "candidate.tar.gz",
            selection="packaging",
            environment_kind="core",
            profile="packaging",
        )


@pytest.mark.parametrize(
    ("profile", "strict"),
    [("release", True), ("security", True), ("packaging", False)],
)
def test_strict_integration_profiles_reject_valid_junit_with_skips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: str,
    strict: bool,
) -> None:
    run_suite = _run_suite()

    def fake_run_pytest(command: list[str], *, env: dict[str, str]) -> tuple[int, str]:
        _ = env
        result_path = Path(command[-1].removeprefix("--junitxml="))
        result_path.write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="1">'
            '<testcase name="required"><skipped /></testcase></testsuite>',
            encoding="utf-8",
        )
        return 0, "1 skipped"

    monkeypatch.setitem(run_suite.__globals__, "RESULTS", tmp_path)
    monkeypatch.setitem(run_suite.__globals__, "run_pytest", fake_run_pytest)

    if strict:
        with pytest.raises(RuntimeError, match="skipped 1 required test"):
            run_suite(
                tmp_path / "python",
                tmp_path / "candidate.whl",
                tmp_path / "candidate.tar.gz",
                selection=profile,
                environment_kind="core",
                profile=profile,
            )
    else:
        run_suite(
            tmp_path / "python",
            tmp_path / "candidate.whl",
            tmp_path / "candidate.tar.gz",
            selection=profile,
            environment_kind="core",
            profile=profile,
        )


def test_strict_integration_profile_rejects_skipped_requested_optional_extra(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_suite = _run_suite()

    def fake_run_pytest(command: list[str], *, env: dict[str, str]) -> tuple[int, str]:
        _ = env
        result_path = Path(command[-1].removeprefix("--junitxml="))
        result_path.write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="1">'
            '<testcase name="requested-extra"><skipped /></testcase></testsuite>',
            encoding="utf-8",
        )
        return 0, "1 skipped"

    monkeypatch.setitem(run_suite.__globals__, "RESULTS", tmp_path)
    monkeypatch.setitem(run_suite.__globals__, "run_pytest", fake_run_pytest)

    with pytest.raises(RuntimeError, match="skipped 1 required test"):
        run_suite(
            tmp_path / "python",
            tmp_path / "candidate.whl",
            tmp_path / "candidate.tar.gz",
            selection="extras",
            environment_kind="extra-any-llm",
            profile="release",
        )


def test_required_packaging_dependency_suite_rejects_valid_junit_with_skips(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_suite = _run_suite()

    def fake_run_pytest(command: list[str], *, env: dict[str, str]) -> tuple[int, str]:
        _ = env
        result_path = Path(command[-1].removeprefix("--junitxml="))
        result_path.write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="1">'
            '<testcase name="dependency-contract"><skipped /></testcase></testsuite>',
            encoding="utf-8",
        )
        return 0, "1 skipped"

    monkeypatch.setitem(run_suite.__globals__, "RESULTS", tmp_path)
    monkeypatch.setitem(run_suite.__globals__, "run_pytest", fake_run_pytest)

    with pytest.raises(RuntimeError, match="skipped 1 required test"):
        run_suite(
            tmp_path / "python",
            tmp_path / "candidate.whl",
            tmp_path / "candidate.tar.gz",
            selection="packaging_dependency",
            environment_kind="wheel-cloudflare",
            profile="packaging",
            require_no_skips=True,
        )


def test_extra_collection_deselects_only_non_applicable_memory_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = cast(
        Callable[[Any, list[Any]], None],
        runpy.run_path(str(INTEGRATION_CONFTEST))["pytest_collection_modifyitems"],
    )
    requested = SimpleNamespace(originalname="test_requested_optional_extra_imports")
    matching = SimpleNamespace(
        originalname="test_memory_extra_lazy_exports_resolve_to_the_installed_backend",
        callspec=SimpleNamespace(params={"optional_extra": "redis"}),
    )
    non_applicable = SimpleNamespace(
        originalname="test_memory_extra_lazy_exports_resolve_to_the_installed_backend",
        callspec=SimpleNamespace(params={"optional_extra": "encrypt"}),
    )
    deselected: list[Any] = []
    config = SimpleNamespace(
        hook=SimpleNamespace(
            pytest_deselected=lambda *, items: deselected.extend(items),
        )
    )
    items = [requested, matching, non_applicable]
    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_EXTRA", "redis")

    hook(config, items)

    assert items == [requested, matching]
    assert deselected == [non_applicable]


def test_code_change_detection_includes_packaged_contract_inputs() -> None:
    detector = CHANGE_DETECTOR.read_text(encoding="utf-8")

    assert "integration_tests/" in detector
    assert "detect-changes\\.sh" in detector
    assert "run_integration_tests\\.py" in detector
    assert "update_released_api_contract\\.py" in detector
    assert "\\.github/workflows/tests\\.yml" in detector


def test_packaging_profile_checks_dependency_present_contract_for_wheel_and_sdist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(RUNNER))
    main = cast(Callable[[], None], namespace["main"])
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    created: list[tuple[str, Path, str | None]] = []
    suites: list[dict[str, Any]] = []

    def fake_build_distributions() -> tuple[Path, Path]:
        return wheel, sdist

    def fake_create_environment(
        name: str,
        distribution: Path,
        *,
        extras: bool = False,
        optional_extra: str | None = None,
        additional_requirements: tuple[str, ...] = (),
    ) -> Path:
        _ = (extras, additional_requirements)
        created.append((name, distribution, optional_extra))
        return tmp_path / name / "python"

    def fake_run_suite(*args: object, **kwargs: Any) -> None:
        _ = args
        suites.append(kwargs)

    monkeypatch.setattr(sys, "argv", [str(RUNNER), "--profile", "packaging"])
    monkeypatch.setitem(main.__globals__, "build_distributions", fake_build_distributions)
    monkeypatch.setitem(main.__globals__, "create_environment", fake_create_environment)
    monkeypatch.setitem(main.__globals__, "run_suite", fake_run_suite)
    monkeypatch.setattr(main.__globals__["shutil"], "rmtree", lambda *args, **kwargs: None)

    main()

    assert ("wheel-cloudflare", wheel, "cloudflare") in created
    assert ("sdist-cloudflare", sdist, "cloudflare") in created
    dependency_suites = [
        suite
        for suite in suites
        if suite["environment_kind"] in {"wheel-cloudflare", "sdist-cloudflare"}
    ]
    assert [suite["environment_kind"] for suite in dependency_suites] == [
        "wheel-cloudflare",
        "sdist-cloudflare",
    ]
    assert all(suite["selection"] == "packaging_dependency" for suite in dependency_suites)
    assert all(
        suite["additional_env"]
        == {
            "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES": "aiohttp",
            "OPENAI_AGENTS_INTEGRATION_OPTIONAL_DEPENDENCY_INSTALLATION": "extra cloudflare",
            "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_EXTRA": "cloudflare",
        }
        for suite in dependency_suites
    )
    assert all(suite["require_no_skips"] is True for suite in dependency_suites)


def test_release_profile_enforces_strict_security_for_wheel_and_sdist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(RUNNER))
    main = cast(Callable[[], None], namespace["main"])
    created: list[tuple[str, str | None]] = []
    suites: list[dict[str, Any]] = []

    def fake_build_distributions() -> tuple[Path, Path]:
        return tmp_path / "candidate.whl", tmp_path / "candidate.tar.gz"

    def fake_create_environment(
        name: str,
        distribution: Path,
        *,
        extras: bool = False,
        optional_extra: str | None = None,
        additional_requirements: tuple[str, ...] = (),
    ) -> Path:
        _ = (distribution, extras, additional_requirements)
        created.append((name, optional_extra))
        return tmp_path / name / "python"

    def fake_run_suite(*args: object, **kwargs: Any) -> None:
        _ = args
        suites.append(kwargs)

    monkeypatch.setenv("OPENAI_AGENTS_INTEGRATION_STRICT", "0")
    monkeypatch.setattr(sys, "argv", [str(RUNNER), "--profile", "release"])
    monkeypatch.setitem(main.__globals__, "build_distributions", fake_build_distributions)
    monkeypatch.setitem(main.__globals__, "create_environment", fake_create_environment)
    monkeypatch.setitem(main.__globals__, "run_suite", fake_run_suite)
    monkeypatch.setattr(main.__globals__["shutil"], "rmtree", lambda *args, **kwargs: None)

    main()

    assert os.environ["OPENAI_AGENTS_INTEGRATION_STRICT"] == "1"
    assert ("core", "docker") in created
    assert ("sdist", "docker") in created
    assert ("wheel-cloudflare", "cloudflare") in created
    assert ("sdist-cloudflare", "cloudflare") in created
    assert any(
        suite["environment_kind"] == "core" and "security" in suite["selection"] for suite in suites
    )
    assert any(
        suite["environment_kind"] == "sdist" and "security" in suite["selection"]
        for suite in suites
    )
    extra_suites = [suite for suite in suites if suite["environment_kind"].startswith("extra-")]
    assert extra_suites
    assert all("allow_skips" not in suite for suite in extra_suites)
    dependency_suites = [
        suite
        for suite in suites
        if suite["environment_kind"] in {"wheel-cloudflare", "sdist-cloudflare"}
    ]
    assert len(dependency_suites) == 2
    assert all(suite["selection"] == "packaging_dependency" for suite in dependency_suites)
    assert all(
        suite["additional_env"]
        == {
            "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES": "aiohttp",
            "OPENAI_AGENTS_INTEGRATION_OPTIONAL_DEPENDENCY_INSTALLATION": "extra cloudflare",
            "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_EXTRA": "cloudflare",
        }
        for suite in dependency_suites
    )
    assert all(suite["require_no_skips"] is True for suite in dependency_suites)
    assert namespace["STRICT_PROFILES"] == frozenset({"release", "security"})


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_prospective_contract_profile_isolates_each_policy_installation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
) -> None:
    namespace = runpy.run_path(str(RUNNER))
    main = cast(Callable[[], None], namespace["main"])
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    prospective_contract = tmp_path / "prospective-contract.json"
    prospective_contract.write_text("{}", encoding="utf-8")
    created: list[tuple[str, Path, str | None, tuple[str, ...]]] = []
    suites: list[dict[str, Any]] = []

    def fake_build_distributions() -> tuple[Path, Path]:
        return wheel, sdist

    def fake_create_environment(
        name: str,
        distribution: Path,
        *,
        extras: bool = False,
        optional_extra: str | None = None,
        additional_requirements: tuple[str, ...] = (),
    ) -> Path:
        _ = extras
        created.append((name, distribution, optional_extra, additional_requirements))
        return tmp_path / name / "python"

    def fake_run_suite(*args: object, **kwargs: Any) -> None:
        _ = args
        suites.append(kwargs)

    monkeypatch.setenv("OPENAI_AGENTS_PROSPECTIVE_RELEASE_CONTRACT", str(prospective_contract))
    monkeypatch.setattr(sys, "argv", [str(RUNNER), "--profile", "prospective-contract"])
    monkeypatch.setattr(main.__globals__["sys"], "platform", platform)
    monkeypatch.setitem(main.__globals__, "build_distributions", fake_build_distributions)
    monkeypatch.setitem(main.__globals__, "create_environment", fake_create_environment)
    monkeypatch.setitem(main.__globals__, "run_suite", fake_run_suite)
    monkeypatch.setattr(main.__globals__["shutil"], "rmtree", lambda *args, **kwargs: None)

    main()

    policy = namespace["load_submodule_export_policy"](namespace["CONTRACT_POLICY"])
    supported_installations = tuple(
        installation
        for installation in policy.dependency_installations
        if installation.is_supported_on_current_platform()
    )
    isolated = [entry for entry in created if "-prospective-" in entry[0]]
    assert len(isolated) == 2 * len(supported_installations)
    assert all(bool(extra) != bool(requirements) for _, _, extra, requirements in isolated)
    assert ("wheel-prospective-aiohttp", wheel, "cloudflare", ()) in isolated
    assert (
        "wheel-prospective-aiosqlite",
        wheel,
        None,
        ("aiosqlite>=0.21.0",),
    ) in isolated
    assert ("sdist-prospective-modal", sdist, "modal", ()) in isolated

    isolated_suites = [suite for suite in suites if "-prospective-" in suite["environment_kind"]]
    assert len(isolated_suites) == len(isolated)
    assert all(suite["selection"] == "packaging_dependency" for suite in isolated_suites)
    assert all(suite["require_no_skips"] is True for suite in isolated_suites)
    assert {
        suite["additional_env"]["OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES"]
        for suite in isolated_suites
    } == {installation.dependency_module for installation in supported_installations}
    assert {
        (
            suite["additional_env"]["OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES"],
            suite["additional_env"].get("OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_EXTRA"),
        )
        for suite in isolated_suites
    } == {
        (installation.dependency_module, installation.extra)
        for installation in supported_installations
    }


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_prospective_platform_profile_checks_core_before_combined_optional_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
) -> None:
    namespace = runpy.run_path(str(RUNNER))
    main = cast(Callable[[], None], namespace["main"])
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    prospective_contract = tmp_path / "prospective-contract.json"
    prospective_contract.write_text("{}", encoding="utf-8")
    created: list[tuple[str, Path, str | None, tuple[str, ...]]] = []
    suites: list[dict[str, Any]] = []

    def fake_build_distributions() -> tuple[Path, Path]:
        return wheel, sdist

    def fake_create_environment(
        name: str,
        distribution: Path,
        *,
        extras: bool = False,
        optional_extra: str | None = None,
        additional_requirements: tuple[str, ...] = (),
    ) -> Path:
        _ = extras
        created.append((name, distribution, optional_extra, additional_requirements))
        return tmp_path / name / "python"

    def fake_run_suite(*args: object, **kwargs: Any) -> None:
        _ = args
        suites.append(kwargs)

    monkeypatch.setenv("OPENAI_AGENTS_PROSPECTIVE_RELEASE_CONTRACT", str(prospective_contract))
    monkeypatch.setattr(sys, "argv", [str(RUNNER), "--profile", "prospective-platform"])
    monkeypatch.setattr(main.__globals__["sys"], "platform", platform)
    monkeypatch.setitem(main.__globals__, "build_distributions", fake_build_distributions)
    monkeypatch.setitem(main.__globals__, "create_environment", fake_create_environment)
    monkeypatch.setitem(main.__globals__, "run_suite", fake_run_suite)
    monkeypatch.setattr(main.__globals__["shutil"], "rmtree", lambda *args, **kwargs: None)

    main()

    policy = namespace["load_submodule_export_policy"](namespace["CONTRACT_POLICY"])
    supported_installations = tuple(
        installation
        for installation in policy.dependency_installations
        if installation.is_supported_on_current_platform()
    )
    expected_extras = ",".join(
        sorted(
            {
                installation.extra
                for installation in supported_installations
                if installation.extra is not None
            }
        )
    )
    expected_requirements = tuple(
        sorted(
            {
                installation.requirement
                for installation in supported_installations
                if installation.requirement is not None
            }
        )
    )
    assert created == [
        (
            "wheel-prospective-platform-core",
            wheel,
            None,
            (),
        ),
        (
            "wheel-prospective-platform",
            wheel,
            expected_extras,
            expected_requirements,
        ),
    ]
    assert len(suites) == 2
    assert suites[0]["environment_kind"] == "wheel-prospective-platform-core"
    assert suites[0]["selection"] == "packaging_dependency"
    assert suites[0]["require_no_skips"] is True
    assert "additional_env" not in suites[0]
    assert suites[1]["environment_kind"] == "wheel-prospective-platform"
    assert suites[1]["selection"] == "packaging_dependency"
    assert suites[1]["require_no_skips"] is True
    assert suites[1]["additional_env"] == {
        "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES": ",".join(
            installation.dependency_module for installation in supported_installations
        ),
        "OPENAI_AGENTS_INTEGRATION_OPTIONAL_DEPENDENCY_INSTALLATION": (
            "policy optional dependencies"
        ),
    }


def test_prospective_platform_profile_excludes_unsupported_optional_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = runpy.run_path(str(RUNNER))
    main = cast(Callable[[], None], namespace["main"])
    wheel = tmp_path / "candidate.whl"
    sdist = tmp_path / "candidate.tar.gz"
    prospective_contract = tmp_path / "prospective-contract.json"
    prospective_contract.write_text("{}", encoding="utf-8")
    created: list[tuple[str, Path, str | None, tuple[str, ...]]] = []
    suites: list[dict[str, Any]] = []

    def fake_create_environment(
        name: str,
        distribution: Path,
        *,
        extras: bool = False,
        optional_extra: str | None = None,
        additional_requirements: tuple[str, ...] = (),
    ) -> Path:
        _ = extras
        created.append((name, distribution, optional_extra, additional_requirements))
        return tmp_path / name / "python"

    monkeypatch.setenv("OPENAI_AGENTS_PROSPECTIVE_RELEASE_CONTRACT", str(prospective_contract))
    monkeypatch.setattr(sys, "argv", [str(RUNNER), "--profile", "prospective-platform"])
    monkeypatch.setattr(main.__globals__["sys"], "platform", "win32")
    monkeypatch.setitem(main.__globals__, "build_distributions", lambda: (wheel, sdist))
    monkeypatch.setitem(main.__globals__, "create_environment", fake_create_environment)
    monkeypatch.setitem(
        main.__globals__, "run_suite", lambda *args, **kwargs: suites.append(kwargs)
    )
    monkeypatch.setattr(main.__globals__["shutil"], "rmtree", lambda *args, **kwargs: None)

    main()

    combined_environment = next(
        environment for environment in created if environment[0] == "wheel-prospective-platform"
    )
    assert "vercel" not in (combined_environment[2] or "").split(",")
    combined_suite = next(
        suite for suite in suites if suite["environment_kind"] == "wheel-prospective-platform"
    )
    required_dependencies = combined_suite["additional_env"][
        "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES"
    ].split(",")
    assert "vercel" not in required_dependencies
    assert "aiohttp" in required_dependencies
    assert (
        "[integration] skipping optional dependency vercel on unsupported platform win32"
        in capsys.readouterr().out
    )
