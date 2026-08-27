from __future__ import annotations

import re
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
DAPR_REDIS_TEST = ROOT / "integration_tests" / "containers" / "test_dapr_redis.py"
EXAMPLE_RUNNER = ROOT / ".github" / "scripts" / "run_examples.sh"
EXAMPLE_SUITE = ROOT / "examples" / "run_examples.py"
SKILLS = ROOT / ".agents" / "skills"


def _make_recipes() -> dict[str, str]:
    recipes: dict[str, str] = {}
    current_target: str | None = None
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        target_match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_-]*):(?:\s.*)?", line)
        if target_match:
            current_target = target_match.group(1)
            recipes[current_target] = ""
        elif current_target is not None and line.startswith("\t"):
            recipes[current_target] += line.removeprefix("\t") + "\n"
        elif line and not line.startswith((" ", "\t")):
            current_target = None
    return recipes


def _workflow_job(name: str) -> str:
    workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
    job_pattern = rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z0-9-]+:\n|\Z)"
    match = re.search(job_pattern, workflow)
    assert match is not None
    return match.group("body")


def test_examples_run_analysis_skill_has_no_execution_path() -> None:
    analysis_skill = SKILLS / "examples-run-analysis"
    assert not (SKILLS / "examples-auto-run").exists()
    assert not (SKILLS / "integration-tests").exists()
    assert sorted(
        path.relative_to(analysis_skill).as_posix()
        for path in analysis_skill.rglob("*")
        if path.is_file()
    ) == ["SKILL.md", "agents/openai.yaml"]

    instructions = (analysis_skill / "SKILL.md").read_text(encoding="utf-8")
    prompt = (analysis_skill / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert "This skill is read-only and analysis-only." in instructions
    assert (
        "Never invoke an examples Make target or `.github/scripts/run_examples.sh`." in instructions
    )
    assert "Inspect the process table and `.tmp/examples-auto-run.pid`" in instructions
    assert "including foreground and background runs" in instructions
    assert "an absent or stale pid file does not prove that no run is active" in instructions
    assert ".tmp/examples-run.pid" not in instructions
    assert "Do not execute any of these commands as part of this skill." in instructions
    assert "without executing or controlling any process" in prompt


def test_makefile_exposes_every_preserved_example_operation() -> None:
    recipes = _make_recipes()
    expected_commands = {
        "examples-run": "$(EXAMPLES_RUNNER) start $(EXAMPLES_ARGS)",
        "examples-run-background": "$(EXAMPLES_RUNNER) start --background $(EXAMPLES_ARGS)",
        "examples-status": "$(EXAMPLES_RUNNER) status",
        "examples-stop": "$(EXAMPLES_RUNNER) stop",
        "examples-logs": "$(EXAMPLES_RUNNER) logs",
        "examples-tail": "$(EXAMPLES_RUNNER) tail $(EXAMPLES_LOG)",
    }

    assert EXAMPLE_RUNNER.is_file()
    assert "EXAMPLES_RUNNER := bash .github/scripts/run_examples.sh" in MAKEFILE.read_text(
        encoding="utf-8"
    )
    for target, command in expected_commands.items():
        assert recipes[target].strip() == command
    assert "examples-rerun" not in recipes
    assert "examples-collect-rerun" not in recipes


def test_repository_example_script_preserves_runner_contract() -> None:
    runner = EXAMPLE_RUNNER.read_text(encoding="utf-8")

    assert 'PID_FILE="$ROOT/.tmp/examples-auto-run.pid"' in runner
    assert 'LOG_DIR="$ROOT/.tmp/examples-start-logs"' in runner
    assert (
        'DEFAULT_UV_EXTRAS="litellm any-llm sqlalchemy redis blaxel modal runloop temporal"'
        in runner
    )
    for required_argument in ("--auto-mode", "--main-log", "--logs-dir"):
        assert required_argument in runner
    for optional_mode in (
        "EXAMPLES_INCLUDE_INTERACTIVE",
        "EXAMPLES_INCLUDE_SERVER",
        "EXAMPLES_INCLUDE_AUDIO",
        "EXAMPLES_INCLUDE_EXTERNAL",
    ):
        assert optional_mode in runner
    for operation in ("start", "status", "stop", "logs", "tail"):
        assert re.search(rf"(?:^|\n)  {operation}\)", runner)
    assert 'rm -f "$PID_FILE"' in runner


def test_examples_rerun_mechanism_is_removed() -> None:
    sources = [
        MAKEFILE.read_text(encoding="utf-8"),
        EXAMPLE_RUNNER.read_text(encoding="utf-8"),
        EXAMPLE_SUITE.read_text(encoding="utf-8"),
        (ROOT / "examples" / "README.md").read_text(encoding="utf-8"),
        (SKILLS / "examples-run-analysis" / "SKILL.md").read_text(encoding="utf-8"),
    ]

    assert all("rerun" not in source.lower() for source in sources)


def test_all_make_integration_entry_points_use_classified_profiles() -> None:
    namespace = runpy.run_path(str(ROOT / ".github" / "scripts" / "run_integration_tests.py"))
    classified_profiles = set(namespace["PROFILE_CREDENTIAL_CLASSES"])

    recipes = _make_recipes()
    integration_recipes = {
        target: recipe
        for target, recipe in recipes.items()
        if target == "integration-tests" or target.startswith("integration-tests-")
    }
    assert integration_recipes
    for target, recipe in integration_recipes.items():
        profile = re.search(r"--profile ([a-z0-9-]+)", recipe)
        assert profile is not None, target
        assert profile.group(1) in classified_profiles


def test_container_integration_has_one_non_matrix_workflow_job() -> None:
    containers_job = _workflow_job("containers")
    tests_job = _workflow_job("tests")

    assert "matrix:" not in containers_job
    assert 'python-version: "3.14"' in containers_job
    assert 'TESTCONTAINERS_RYUK_DISABLED: "true"' in containers_job
    assert containers_job.count("make integration-tests-containers") == 1
    assert "integration-tests-containers" not in tests_job


def test_container_integration_pins_dapr_runtime_image() -> None:
    source = DAPR_REDIS_TEST.read_text(encoding="utf-8")

    assert (
        '"daprio/daprd:1.16.2@sha256:'
        '3ae30141b9775b5bc03d073185abf1101fbad1e1941c1c3075527bc4865454e3"' in source
    )
    assert "daprio/daprd:latest" not in source


def test_container_integration_does_not_require_docker_cli() -> None:
    source = DAPR_REDIS_TEST.read_text(encoding="utf-8")

    assert 'shutil.which("docker")' not in source
    assert "client.ping()" in source


def test_prospective_contract_preparation_removes_api_key_before_uv() -> None:
    recipe = _make_recipes()["prepare-prospective-released-api-contract"]

    assert recipe.startswith("@unset OPENAI_API_KEY; \\\n")
    assert recipe.index("unset OPENAI_API_KEY") < recipe.index("uv run")
