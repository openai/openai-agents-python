from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
TESTS_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish.yml"
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


def _workflow_job(name: str, path: Path = TESTS_WORKFLOW) -> str:
    workflow = path.read_text(encoding="utf-8")
    job_pattern = rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-z0-9-]+:\n|\Z)"
    match = re.search(job_pattern, workflow)
    assert match is not None
    return match.group("body")


@pytest.mark.parametrize(
    ("path", "eligible"),
    [
        (b"tests/fixtures/released_api_contract.json", True),
        (b"tests/fixtures/other_contract.json", False),
        (b"tests/fixtures/released_api_contract.json.bak", False),
        (b"src/agents/released_api_contract.json", False),
        (b"packages/codex/rust/released_api_contract.json", False),
        (b"tests/test_native_agent.py", True),
    ],
)
def test_native_reuse_allows_only_the_generated_api_fixture(path: bytes, eligible: bool) -> None:
    # Exercise the selector in the actual workflow against neighboring file names.
    script = _workflow_job("changes")
    match = re.search(r"(?ms)^(\s*)allowed = re\.compile\(\n(?P<pattern>.*?)^\1\)", script)
    assert match is not None
    pattern = ast.literal_eval("(" + match.group("pattern").strip() + ")")
    assert bool(re.compile(pattern).fullmatch(path)) is eligible


def test_repository_skills_workflow_uses_the_isolated_runner() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/repo-skills.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["repo-skills"]["steps"]
        if step.get("name") == "Run repository skill tests"
    )
    assert step["run"] == "make tests-repo-skills"
    assert _make_recipes()["tests-repo-skills"].strip() == (
        "uv run --no-project --python 3.11 python .github/scripts/run_repo_skill_tests.py"
    )
    assert (ROOT / ".github/scripts/run_repo_skill_tests.py").is_file()


def test_native_contract_preparation_has_no_credentials_or_sdk_install() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-candidate.yml").read_text(), Loader=yaml.BaseLoader
    )
    contract = workflow["jobs"]["contract"]
    assert contract["permissions"] == {"contents": "read"}
    assert "environment" not in contract
    assert "OPENAI_API_KEY" not in str(contract) and "secrets." not in str(contract)
    preparation = next(
        step
        for step in contract["steps"]
        if step.get("name") == "Freeze and validate the candidate public API"
    )
    assert preparation["env"]["UV_NO_SYNC"] == "1"
    assert preparation["env"]["UV_FROZEN"] == "1"
    assert preparation["run"].startswith("uv sync --group dev --no-install-project\n")
    recipes = _make_recipes()
    assert "update_released_api_contract.py" in recipes["update-released-api-contract"]
    assert '--version "$(VERSION)" --check' in recipes["check-released-api-contract"]


@pytest.mark.parametrize("job_name", ["checks", "build"])
def test_release_build_validates_before_executing_candidate_code(job_name: str) -> None:
    build = _workflow_job(job_name, PUBLISH_WORKFLOW)

    assert "contents: read" in build
    assert "id-token:" not in build
    assert "environment:" not in build
    assert "ref: refs/heads/main\n          path: control" in build
    assert "ref: ${{ github.sha }}\n          path: release-source" in build
    assert build.count("persist-credentials: false") == 2
    assert "fetch-depth: 0" in build
    validation = build.index("python -I control/.github/scripts/verify_release.py")
    candidate_command = "run: uv lock --check" if job_name == "checks" else "qualify-distributions"
    assert validation < build.index(candidate_command)
    assert ' --tag "$RELEASE_TAG" --expected-sha "$RELEASE_SHA"' in build
    if job_name == "checks":
        assert "enable-cache: false" in build


def test_release_checks_gate_collection_without_rebuilding() -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    checks, build = jobs["checks"], jobs["build"]
    assert build["needs"] == "checks"
    assert checks["if"] == "github.repository == 'openai/openai-agents-python'"
    assert "if" not in build
    assert jobs["publish"]["if"] == "${{ false }}"
    qualification = next(
        step for step in checks["steps"] if step["name"] == "Verify qualified release distributions"
    )
    assert qualification["run"].endswith("release_automation.py qualify-distributions")
    collection = next(
        step for step in build["steps"] if step["name"] == "Collect qualified release distributions"
    )
    assert "--output-dir release-source/dist" in collection["run"]
    for job in (checks, build, jobs["publish"]):
        assert "continue-on-error" not in job
        for step in job["steps"]:
            assert "continue-on-error" not in step
            assert "setup-native" not in step.get("uses", "")
            assert "uv build" not in step.get("run", "")
            if step.get("name") in {
                "Verify human-reviewed release candidate",
                "Checkout approval validator from main",
                "Revalidate human approval before upload",
            }:
                assert step["if"] == "vars.RELEASE_AUTOMATION_ENABLED == 'true'"
            else:
                assert "if" not in step
    tests = yaml.load(TESTS_WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert set(tests["on"]["push"]["branches"]) == {"main", "v1"}


def test_release_checks_reject_stale_lockfile(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("The publish workflow requires uv.")
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    check = next(
        step for step in workflow["jobs"]["checks"]["steps"] if step["name"] == "Check release lock"
    )
    assert check["run"] == "uv lock --check"
    dependency = tmp_path / "dependency"
    dependency.mkdir()
    (dependency / "pyproject.toml").write_text(
        '[project]\nname = "fixture-dependency"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    project = (
        '[project]\nname = "release-fixture"\nversion = "0.1.0"\n'
        'requires-python = ">=3.10"\ndependencies = []\n'
        '[tool.uv.sources]\nfixture-dependency = { path = "dependency" }\n'
    )
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(project, encoding="utf-8")
    env = {
        "PATH": os.defpath,
        "UV_PYTHON": sys.executable,
        "UV_CACHE_DIR": str(tmp_path / "cache"),
    }
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    command = [uv, "--offline", "--no-config"]
    subprocess.run(command + ["lock"], cwd=tmp_path, env=env, check=True, timeout=15)
    lockfile = tmp_path / "uv.lock"
    original_lock = lockfile.read_bytes()
    # Exercise the exact release check without building or installing either package.
    sync = command + ["lock", "--check"]
    env.update(check["env"])
    subprocess.run(sync, cwd=tmp_path, env=env, check=True, timeout=15)
    pyproject.write_text(
        project.replace("dependencies = []", 'dependencies = ["fixture-dependency"]'),
        encoding="utf-8",
    )
    result = subprocess.run(sync, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "lockfile" in result.stderr and "needs to be updated" in result.stderr
    assert lockfile.read_bytes() == original_lock


def test_release_build_is_isolated_from_test_execution() -> None:
    workflow = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))
    checks = workflow["jobs"]["checks"]
    build = workflow["jobs"]["build"]
    # Separate GitHub-hosted jobs provide fresh runners, not just new directories.
    assert checks["runs-on"] == build["runs-on"] == "ubuntu-latest"
    assert build["permissions"] == {"contents": "read", "actions": "read"}
    assert checks["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "actions": "read",
    }
    assert "outputs" not in checks
    assert build["needs"] == "checks"
    for job in (checks, build):
        assert "env" not in job and "container" not in job
        for step in job["steps"]:
            action = step.get("uses", "")
            assert not action.startswith(("actions/cache@", "actions/download-artifact@"))
            if action.startswith("astral-sh/setup-uv@"):
                assert step["with"]["enable-cache"] is False
            if job is checks:
                assert not action.startswith("actions/upload-artifact@")
    build_commands = [step["run"] for step in build["steps"] if "run" in step]
    assert len(build_commands) == 2  # Provenance validation, then packaging; no test execution.
    assert "qualify-distributions --output-dir release-source/dist" in build_commands[-1]


def test_pypi_job_only_publishes_the_build_artifact() -> None:
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    publish = _workflow_job("publish", PUBLISH_WORKFLOW)

    assert "permissions: {}" in workflow
    assert workflow.count("id-token: write") == 1
    assert "needs: build" in publish
    assert "name: pypi" in publish
    assert "id-token: write" in publish
    assert (
        "run: python3 -I control/.github/scripts/release_automation.py verify-publication"
        in publish
    )
    actions = re.findall(r"uses: ([^\s]+)", publish)
    assert len(actions) == 3
    assert actions[0].startswith("actions/download-artifact@")
    assert actions[1].startswith("actions/checkout@")
    assert actions[2].startswith("pypa/gh-action-pypi-publish@")
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in actions)
    assert "artifact-ids: ${{ needs.build.outputs.artifact-id }}" in publish
    assert "artifact-id: ${{ steps.upload.outputs.artifact-id }}" in _workflow_job(
        "build", PUBLISH_WORKFLOW
    )
    assert "path: dist/" in publish
    assert "merge-multiple: true" in publish


def test_existing_release_tagging_does_not_run_for_the_preview_branch() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/release-tag.yml").read_text(), Loader=yaml.BaseLoader
    )
    trigger = workflow["on"]["pull_request"]
    assert trigger["types"] == ["closed"]
    assert trigger["branches"] == ["main"]


def test_v1_source_only_workflows_cannot_publish_or_tag() -> None:
    workflows = ROOT / ".github/workflows"
    for name, job in [("publish.yml", "publish"), ("alpha-install.yml", "install")]:
        workflow = yaml.load((workflows / name).read_text(), Loader=yaml.BaseLoader)
        assert workflow["jobs"][job]["if"] == "${{ false }}"
    tags = yaml.load((workflows / "release-tag.yml").read_text(), Loader=yaml.BaseLoader)
    assert tags["jobs"]["tag-release"]["if"].startswith("false &&")
    codeql = yaml.load((workflows / "codeql.yml").read_text(), Loader=yaml.BaseLoader)
    assert codeql["on"]["push"]["branches"] == ["main", "v1"]
    assert codeql["on"]["pull_request"]["branches"] == ["main", "v1"]
