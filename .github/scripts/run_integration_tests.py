from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integration_tests._contract_support import (  # noqa: E402
    SubmoduleExportPolicy,
    load_submodule_export_policy,
)

WORKSPACE = ROOT / ".tmp" / "integration-tests"
DIST = WORKSPACE / "dist"
RESULTS = WORKSPACE / "results"
TESTS = ROOT / "integration_tests"
CONTRACT_POLICY = ROOT / "tests" / "fixtures" / "released_api_contract_policy.json"
PROSPECTIVE_CONTRACT_ENV = "OPENAI_AGENTS_PROSPECTIVE_RELEASE_CONTRACT"
EXTRAS = "any-llm,litellm,realtime,voice"
OPTIONAL_EXTRAS = (
    "any-llm",
    "litellm",
    "realtime",
    "voice",
    "sqlalchemy",
    "encrypt",
    "redis",
    "viz",
    "s3",
)
STRICT_PROFILES = frozenset({"release", "security"})
PROFILES = (
    "packaging",
    "prospective-contract",
    "prospective-platform",
    "security",
    "mcp-v1",
    "core",
    "providers",
    "realtime",
    "voice",
    "hosted",
    "extras",
    "full",
    "release",
    "nightly",
    "manual",
)


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    print(f"[integration] {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def run_pytest(command: list[str], *, env: dict[str, str]) -> tuple[int, str]:
    print(f"[integration] {' '.join(command)}", flush=True)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output.append(line)
    return process.wait(), "".join(output)


def build_distributions() -> tuple[Path, Path]:
    shutil.rmtree(DIST, ignore_errors=True)
    DIST.mkdir(parents=True, exist_ok=True)
    run(["uv", "build", "--out-dir", str(DIST)])
    wheels = sorted(DIST.glob("openai_agents-*.whl"), key=lambda path: path.stat().st_mtime)
    sdists = sorted(DIST.glob("openai_agents-*.tar.gz"), key=lambda path: path.stat().st_mtime)
    if not wheels or not sdists:
        raise RuntimeError("uv build did not produce both an openai-agents wheel and sdist.")
    return wheels[-1], sdists[-1]


def _any_llm_provider_extras(
    *, external_providers_enabled: bool, direct_providers_enabled: bool
) -> list[str]:
    provider_extras: set[str] = set()
    configured_models = os.environ.get("OPENAI_AGENTS_INTEGRATION_ANY_LLM_MODELS", "")
    for model in configured_models.split(","):
        provider = model.strip().partition("/")[0]
        if provider in {"anthropic", "openrouter"}:
            provider_extras.add(provider)
        elif provider in {"gemini", "google"}:
            provider_extras.add("gemini")

    if external_providers_enabled:
        if direct_providers_enabled and os.environ.get("ANTHROPIC_API_KEY"):
            provider_extras.add("anthropic")
        if direct_providers_enabled and (
            os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        ):
            provider_extras.add("gemini")
        if os.environ.get("OPENROUTER_API_KEY"):
            provider_extras.add("openrouter")

    return sorted(provider_extras)


def create_environment(
    name: str,
    distribution: Path,
    *,
    extras: bool = False,
    optional_extra: str | None = None,
    additional_requirements: tuple[str, ...] = (),
) -> Path:
    environment = WORKSPACE / name
    venv_command = ["uv", "venv", "--clear", str(environment)]
    if python_version := os.environ.get("OPENAI_AGENTS_INTEGRATION_PYTHON"):
        venv_command.extend(["--python", python_version])
    run(venv_command)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    selected_extra = EXTRAS if extras else optional_extra
    requirement = f"{distribution}[{selected_extra}]" if selected_extra else str(distribution)
    requirements = [
        requirement,
        "pytest",
        "pytest-asyncio",
        "pytest-timeout",
        *additional_requirements,
    ]
    external_providers_enabled = os.environ.get(
        "OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS", ""
    ).lower() in {"1", "true", "yes"}
    direct_providers_enabled = os.environ.get(
        "OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS", ""
    ).lower() in {"1", "true", "yes"}
    if extras:
        any_llm_extras = _any_llm_provider_extras(
            external_providers_enabled=external_providers_enabled,
            direct_providers_enabled=direct_providers_enabled,
        )
        if any_llm_extras:
            requirements.append(f"any-llm-sdk[{','.join(any_llm_extras)}]")
    proxy_values = [
        os.environ.get(name, "")
        for name in (
            "ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "http_proxy",
            "https_proxy",
        )
    ]
    if any(value.lower().startswith("socks") for value in proxy_values):
        requirements.append("httpx[socks]")
    run(["uv", "pip", "install", "--python", str(python), *requirements])
    return python


def run_suite(
    python: Path,
    wheel: Path,
    sdist: Path,
    *,
    selection: str,
    environment_kind: str,
    additional_env: dict[str, str] | None = None,
    profile: str,
    require_no_skips: bool = False,
) -> None:
    child_env = dict(os.environ)
    child_env.pop("PYTHONPATH", None)
    if child_env.get("OPENAI_AGENTS_INTEGRATION_DISABLE_PROXY", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        for variable in (
            "ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "http_proxy",
            "https_proxy",
        ):
            child_env.pop(variable, None)
    child_env["PYTHONNOUSERSITE"] = "1"
    child_env["OPENAI_AGENTS_INTEGRATION_WHEEL"] = str(wheel)
    child_env["OPENAI_AGENTS_INTEGRATION_SDIST"] = str(sdist)
    child_env["OPENAI_AGENTS_INTEGRATION_ENVIRONMENT"] = environment_kind
    if additional_env:
        child_env.update(additional_env)
    if environment_kind.startswith("extra-"):
        child_env["OPENAI_AGENTS_INTEGRATION_EXTRA"] = environment_kind.removeprefix("extra-")
    if not os.environ.get("OPENAI_AGENTS_INTEGRATION_ENABLE_TRACING"):
        child_env["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    command = [
        str(python),
        "-I",
        "-m",
        "pytest",
        "-c",
        str(TESTS / "pytest.ini"),
        str(TESTS),
        "-v",
        "--tb=short",
        "-m",
        selection,
    ]
    result_path = RESULTS / profile / f"{environment_kind}.xml"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    command.append(f"--junitxml={result_path}")
    return_code = 1
    output = ""
    try:
        return_code, output = run_pytest(command, env=child_env)
    finally:
        deselected_matches = re.findall(r"(\d+) deselected", output)
        deselected = int(deselected_matches[-1]) if deselected_matches else 0
        junit_totals = _print_junit_summary(
            profile,
            environment_kind,
            result_path,
            deselected=deselected,
        )
    if return_code:
        raise subprocess.CalledProcessError(return_code, command)
    if junit_totals is None:
        raise RuntimeError(
            f"Integration profile {profile}/{environment_kind} did not produce "
            "a valid JUnit report."
        )
    if (profile in STRICT_PROFILES or require_no_skips) and junit_totals["skipped"]:
        raise RuntimeError(
            f"Required integration suite {profile}/{environment_kind} skipped "
            f"{junit_totals['skipped']} required test(s)."
        )


def _print_junit_summary(
    profile: str,
    environment_kind: str,
    result_path: Path,
    *,
    deselected: int,
) -> dict[str, int] | None:
    if not result_path.exists():
        print(
            f"[integration] summary profile={profile} environment={environment_kind} "
            "result=missing",
            flush=True,
        )
        return None
    root = _sanitize_and_load_junit(result_path)
    if root is None:
        print(
            f"[integration] summary profile={profile} environment={environment_kind} "
            "result=invalid",
            flush=True,
        )
        return None
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    totals = {
        key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    print(
        f"[integration] summary profile={profile} environment={environment_kind} "
        f"passed={passed} failed={totals['failures']} errors={totals['errors']} "
        f"skipped={totals['skipped']} deselected={deselected}",
        flush=True,
    )
    return totals


def _sanitize_and_load_junit(result_path: Path) -> ET.Element | None:
    try:
        tree = ET.parse(result_path)
        source_root = tree.getroot()
        if source_root.tag == "testsuite":
            suites = [source_root]
        elif source_root.tag == "testsuites":
            suites = list(source_root.findall("testsuite"))
        else:
            suites = []
        if not suites:
            raise ValueError("JUnit report does not contain a test suite.")

        safe_suites: list[ET.Element] = []
        for suite_index, suite in enumerate(suites):
            counts: dict[str, int] = {}
            for key in ("tests", "failures", "errors", "skipped"):
                value = int(suite.attrib.get(key, "0"))
                if value < 0:
                    raise ValueError(f"JUnit {key} count must be non-negative.")
                counts[key] = value
            testcases = list(suite.findall("testcase"))
            actual_counts = {
                "tests": len(testcases),
                "failures": sum(len(case.findall("failure")) for case in testcases),
                "errors": sum(len(case.findall("error")) for case in testcases),
                "skipped": sum(len(case.findall("skipped")) for case in testcases),
            }
            if counts != actual_counts:
                raise ValueError("JUnit declared counts do not match testcase outcomes.")
            if any(
                sum(len(case.findall(outcome)) for outcome in ("failure", "error", "skipped")) > 1
                for case in testcases
            ):
                raise ValueError("JUnit testcase has multiple terminal outcomes.")

            safe_suite = ET.Element(
                "testsuite",
                {
                    "name": f"suite-{suite_index}",
                    **{key: str(value) for key, value in counts.items()},
                },
            )
            safe_suites.append(safe_suite)
            for case_index, case in enumerate(testcases):
                safe_case = ET.SubElement(
                    safe_suite,
                    "testcase",
                    {"name": f"case-{case_index}"},
                )
                for outcome in ("failure", "error", "skipped"):
                    if case.find(outcome) is not None:
                        ET.SubElement(safe_case, outcome)
                        break

        if source_root.tag == "testsuite":
            safe_root = safe_suites[0]
        else:
            safe_root = ET.Element("testsuites")
            safe_root.extend(safe_suites)
        ET.ElementTree(safe_root).write(result_path, encoding="utf-8", xml_declaration=True)
    except (ET.ParseError, OSError, ValueError):
        try:
            result_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return safe_root


def main() -> None:
    parser = argparse.ArgumentParser(description="Run packaged openai-agents integration tests.")
    parser.add_argument("--profile", choices=PROFILES, default="full")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include configured direct Anthropic and Gemini providers alongside OpenRouter.",
    )
    args = parser.parse_args()
    prospective_policy: SubmoduleExportPolicy | None = None
    if args.profile in {"prospective-contract", "prospective-platform"}:
        prospective_contract = os.environ.get(PROSPECTIVE_CONTRACT_ENV)
        if not prospective_contract or not Path(prospective_contract).is_file():
            raise RuntimeError(
                "The prospective-contract profile requires "
                f"{PROSPECTIVE_CONTRACT_ENV} to name an existing contract file."
            )
        prospective_policy = load_submodule_export_policy(CONTRACT_POLICY)
    if args.profile in STRICT_PROFILES:
        os.environ["OPENAI_AGENTS_INTEGRATION_STRICT"] = "1"
    shutil.rmtree(RESULTS / args.profile, ignore_errors=True)
    if args.all:
        os.environ["OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS"] = "1"
        os.environ["OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS"] = "1"
    wheel, sdist = build_distributions()
    print(f"[integration] wheel={wheel.name} sdist={sdist.name} profile={args.profile}")

    if args.profile == "mcp-v1":
        for mcp_version in ("1.19.0", "1.29.0"):
            environment_kind = f"mcp-v1-{mcp_version}"
            python = create_environment(
                environment_kind,
                wheel,
                additional_requirements=(f"mcp=={mcp_version}",),
            )
            run_suite(
                python,
                wheel,
                sdist,
                selection="mcp_compat",
                environment_kind=environment_kind,
                additional_env={"OPENAI_AGENTS_INTEGRATION_MCP_VERSION": mcp_version},
                profile=args.profile,
            )

    if args.profile in {
        "packaging",
        "prospective-contract",
        "security",
        "core",
        "hosted",
        "full",
        "release",
        "nightly",
        "manual",
    }:
        python = create_environment(
            "core",
            wheel,
            optional_extra="docker" if args.profile in STRICT_PROFILES else None,
        )
        selections = {
            "packaging": "packaging",
            "prospective-contract": "packaging",
            "security": "security",
            "core": "packaging or core",
            "hosted": "packaging or hosted",
            "full": "packaging or ((core or hosted) and not nightly and not manual)",
            "release": (
                "packaging or security or ((core or hosted) and not nightly and not manual)"
            ),
            "nightly": "packaging or ((core or hosted) and not manual)",
            "manual": "packaging or core or hosted",
        }
        run_suite(
            python,
            wheel,
            sdist,
            selection=selections[args.profile],
            environment_kind="core",
            profile=args.profile,
        )

    if args.profile in {"providers", "realtime", "voice", "full", "release", "nightly", "manual"}:
        python = create_environment("extended", wheel, extras=True)
        if args.profile in {"full", "release"}:
            selection = "(providers or realtime or voice) and not nightly and not manual"
        elif args.profile == "nightly":
            selection = "(providers or realtime or voice) and not manual"
        elif args.profile == "manual":
            selection = "providers or realtime or voice"
        else:
            selection = args.profile
        run_suite(
            python,
            wheel,
            sdist,
            selection=selection,
            environment_kind="extended",
            profile=args.profile,
        )

    if args.profile in {
        "packaging",
        "prospective-contract",
        "security",
        "full",
        "release",
        "nightly",
        "manual",
    }:
        python = create_environment(
            "sdist",
            sdist,
            optional_extra="docker" if args.profile in STRICT_PROFILES else None,
        )
        if args.profile == "security":
            selection = "security"
        elif args.profile == "release":
            selection = "packaging or distribution_smoke or security"
        elif args.profile in {"nightly", "manual"}:
            selection = "packaging or distribution_smoke"
        else:
            selection = "packaging"
        run_suite(
            python,
            wheel,
            sdist,
            selection=selection,
            environment_kind="sdist",
            profile=args.profile,
        )

    if args.profile == "prospective-contract":
        assert prospective_policy is not None
        for artifact_kind, distribution in (("wheel", wheel), ("sdist", sdist)):
            for installation in prospective_policy.dependency_installations:
                if not installation.is_supported_on_current_platform():
                    print(
                        "[integration] skipping optional dependency "
                        f"{installation.dependency_module} on unsupported platform "
                        f"{sys.platform}",
                        flush=True,
                    )
                    continue
                dependency_slug = re.sub(r"[^a-z0-9]+", "-", installation.dependency_module.lower())
                environment_kind = f"{artifact_kind}-prospective-{dependency_slug}"
                additional_requirements = (
                    (installation.requirement,) if installation.requirement is not None else ()
                )
                python = create_environment(
                    environment_kind,
                    distribution,
                    optional_extra=installation.extra,
                    additional_requirements=additional_requirements,
                )
                installation_description = (
                    f"extra {installation.extra}"
                    if installation.extra is not None
                    else f"requirement {installation.requirement}"
                )
                additional_env = {
                    "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES": (
                        installation.dependency_module
                    ),
                    "OPENAI_AGENTS_INTEGRATION_OPTIONAL_DEPENDENCY_INSTALLATION": (
                        installation_description
                    ),
                }
                if installation.extra is not None:
                    additional_env["OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_EXTRA"] = (
                        installation.extra
                    )
                run_suite(
                    python,
                    wheel,
                    sdist,
                    selection="packaging_dependency",
                    environment_kind=environment_kind,
                    additional_env=additional_env,
                    profile=args.profile,
                    require_no_skips=True,
                )

    if args.profile == "prospective-platform":
        assert prospective_policy is not None
        core_environment_kind = "wheel-prospective-platform-core"
        core_python = create_environment(core_environment_kind, wheel)
        run_suite(
            core_python,
            wheel,
            sdist,
            selection="packaging_dependency",
            environment_kind=core_environment_kind,
            profile=args.profile,
            require_no_skips=True,
        )

        unsupported_installations = tuple(
            installation
            for installation in prospective_policy.dependency_installations
            if not installation.is_supported_on_current_platform()
        )
        for installation in unsupported_installations:
            print(
                "[integration] skipping optional dependency "
                f"{installation.dependency_module} on unsupported platform {sys.platform}",
                flush=True,
            )
        supported_installations = tuple(
            installation
            for installation in prospective_policy.dependency_installations
            if installation.is_supported_on_current_platform()
        )
        dependency_extras = sorted(
            {
                installation.extra
                for installation in supported_installations
                if installation.extra is not None
            }
        )
        dependency_requirements = tuple(
            sorted(
                {
                    installation.requirement
                    for installation in supported_installations
                    if installation.requirement is not None
                }
            )
        )
        dependency_modules = ",".join(
            installation.dependency_module for installation in supported_installations
        )
        environment_kind = "wheel-prospective-platform"
        python = create_environment(
            environment_kind,
            wheel,
            optional_extra=",".join(dependency_extras) or None,
            additional_requirements=dependency_requirements,
        )
        run_suite(
            python,
            wheel,
            sdist,
            selection="packaging_dependency",
            environment_kind=environment_kind,
            additional_env={
                "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES": dependency_modules,
                "OPENAI_AGENTS_INTEGRATION_OPTIONAL_DEPENDENCY_INSTALLATION": (
                    "policy optional dependencies"
                ),
            },
            profile=args.profile,
            require_no_skips=True,
        )

    if args.profile in {"packaging", "release"}:
        for artifact_kind, distribution in (("wheel", wheel), ("sdist", sdist)):
            environment_kind = f"{artifact_kind}-cloudflare"
            python = create_environment(
                environment_kind,
                distribution,
                optional_extra="cloudflare",
            )
            run_suite(
                python,
                wheel,
                sdist,
                selection="packaging_dependency",
                environment_kind=environment_kind,
                additional_env={
                    "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_DEPENDENCIES": "aiohttp",
                    "OPENAI_AGENTS_INTEGRATION_OPTIONAL_DEPENDENCY_INSTALLATION": (
                        "extra cloudflare"
                    ),
                    "OPENAI_AGENTS_INTEGRATION_REQUIRED_OPTIONAL_EXTRA": "cloudflare",
                },
                profile=args.profile,
                require_no_skips=True,
            )

    if args.profile in {"extras", "full", "release", "nightly", "manual"}:
        for optional_extra in OPTIONAL_EXTRAS:
            environment_kind = f"extra-{optional_extra}"
            python = create_environment(environment_kind, wheel, optional_extra=optional_extra)
            run_suite(
                python,
                wheel,
                sdist,
                selection="extras",
                environment_kind=environment_kind,
                profile=args.profile,
            )


if __name__ == "__main__":
    main()
