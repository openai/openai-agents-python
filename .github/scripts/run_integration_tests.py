from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / ".tmp" / "integration-tests"
DIST = WORKSPACE / "dist"
TESTS = ROOT / "integration_tests"
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
PROFILES = (
    "packaging",
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


def build_distributions() -> tuple[Path, Path]:
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
    name: str, distribution: Path, *, extras: bool = False, optional_extra: str | None = None
) -> Path:
    environment = WORKSPACE / name
    venv_command = ["uv", "venv", "--clear", str(environment)]
    if python_version := os.environ.get("OPENAI_AGENTS_INTEGRATION_PYTHON"):
        venv_command.extend(["--python", python_version])
    run(venv_command)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    selected_extra = EXTRAS if extras else optional_extra
    requirement = f"{distribution}[{selected_extra}]" if selected_extra else str(distribution)
    requirements = [requirement, "pytest", "pytest-asyncio", "pytest-timeout"]
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
    run(command, env=child_env)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run packaged openai-agents integration tests.")
    parser.add_argument("--profile", choices=PROFILES, default="full")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include configured direct Anthropic and Gemini providers alongside OpenRouter.",
    )
    args = parser.parse_args()
    if args.all:
        os.environ["OPENAI_AGENTS_INTEGRATION_EXTERNAL_PROVIDERS"] = "1"
        os.environ["OPENAI_AGENTS_INTEGRATION_DIRECT_PROVIDERS"] = "1"
    wheel, sdist = build_distributions()
    print(f"[integration] wheel={wheel.name} sdist={sdist.name} profile={args.profile}")

    if args.profile in {"packaging", "core", "hosted", "full", "release", "nightly", "manual"}:
        python = create_environment("core", wheel)
        selections = {
            "packaging": "packaging",
            "core": "packaging or core",
            "hosted": "packaging or hosted",
            "full": "packaging or ((core or hosted) and not nightly and not manual)",
            "release": "packaging or ((core or hosted) and not nightly and not manual)",
            "nightly": "packaging or ((core or hosted) and not manual)",
            "manual": "packaging or core or hosted",
        }
        run_suite(
            python,
            wheel,
            sdist,
            selection=selections[args.profile],
            environment_kind="core",
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
        )

    if args.profile in {"packaging", "full", "release", "nightly", "manual"}:
        python = create_environment("sdist", sdist)
        run_suite(python, wheel, sdist, selection="packaging", environment_kind="sdist")

    if args.profile in {"extras", "full", "release", "nightly", "manual"}:
        for optional_extra in OPTIONAL_EXTRAS:
            environment_kind = f"extra-{optional_extra}"
            python = create_environment(environment_kind, wheel, optional_extra=optional_extra)
            run_suite(python, wheel, sdist, selection="extras", environment_kind=environment_kind)


if __name__ == "__main__":
    main()
