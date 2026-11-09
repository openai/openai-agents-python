"""The committed native API contract is independent of release-time generation."""

from __future__ import annotations

import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/update_released_api_contract.py"
_VERSION = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
assert _VERSION is not None
VERSION = _VERSION.group(1)


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    shutil.copytree(
        ROOT / "src/agents",
        tmp_path / "src/agents",
        ignore=shutil.ignore_patterns("*.so", "*.pyd", "bin", "licenses", "__pycache__"),
    )
    shutil.copytree(
        ROOT / "packages/codex/src/_agents_codex",
        tmp_path / "packages/codex/src/_agents_codex",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in ["pyproject.toml", "packages/codex/tests/fixtures/released_api_contract.json"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    return tmp_path


def check(candidate: Path, version: str = VERSION) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("OPENAI_API_KEY", None)
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import importlib.util, pathlib, sys; "
            "spec = importlib.util.spec_from_file_location('contract', sys.argv[1]); "
            "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
            "module.ROOT = pathlib.Path(sys.argv[2]); "
            "module.CONTRACT = module.ROOT / "
            "'packages/codex/tests/fixtures/released_api_contract.json'; "
            "sys.argv = [sys.argv[1], '--version', sys.argv[3], '--check']; "
            "raise SystemExit(module.main())",
            str(SCRIPT),
            str(candidate),
            version,
        ],
        cwd=candidate,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def implementation(candidate: Path, name: str) -> Path:
    return candidate / "packages/codex/src/_agents_codex" / name


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            "session.py",
            "async def submit(",
            "async def _removed_submit(",
        ),
        (
            "session.py",
            "async def submit(\n        self,\n        input: UserInput,",
            "async def submit(\n        self,\n        input: str,",
        ),
        (
            "observation.py",
            "    call_id: str\n    name: str\n",
            "    name: str\n    call_id: str\n",
        ),
        ("observation.py", "    sequence: int = 0", "    sequence: int = 1"),
        ("input.py", "    text: str\n", "    text: bytes\n"),
        (
            "input.py",
            "UserInput: TypeAlias = str | UserMessage",
            "UserInput: TypeAlias = bytes | UserMessage",
        ),
        ("observation.py", "    | UsageUpdated\n", ""),
        (
            "input.py",
            "str | Sequence[InputText | InputImage | InputImageFile | InputFile]",
            "str | Sequence[InputText | InputImageFile | InputFile]",
        ),
        ("agent.py", "    reasoning_effort: str\n", ""),
        (
            "agent.py",
            "class ModelSettings(TypedDict, total=False, closed=True):",
            "class ModelSettings(TypedDict, total=True, closed=True):",
        ),
        ("_function_tools.py", " | HttpMCP[object]", " | int"),
        ("_function_tools.py", " | StdioMCP[object]", " | str"),
        ("tools.py", "    field_names: tuple[str, ...]", "    field_names: tuple[int, ...]"),
        (
            "_events.py",
            "class Failure(TypedDict):\n    code: str",
            "class Failure(TypedDict):\n    code: bytes",
        ),
        (
            "_events.py",
            'Literal["in_progress", "completed", "failed", "incomplete"]',
            'Literal["in_progress", "completed", "incomplete"]',
        ),
        (
            "subagents.py",
            "Annotated[str, AfterValidator(_nonblank)]",
            "Annotated[bytes, AfterValidator(_nonblank)]",
        ),
        (
            "_function_tools.py",
            "Callable[[ToolContextT, PolicyContextT, FunctionTool[Any, Any, Any]], "
            "_CapturedFunction]",
            "Callable[[ToolContextT, PolicyContextT, FunctionTool[Any, Any, Any]], Any]",
        ),
        ("_function_tools.py", "    | FunctionTool[Any, Any]\n", ""),
        (
            "_function_tools.py",
            'ToolContextT = TypeVar("ToolContextT")',
            'ToolContextT = TypeVar("ToolContextT", bound=str)',
        ),
        (
            "_function_tools.py",
            '"BinderT_co", bound=Callable[..., _CapturedFunction] | None, covariant=True, '
            "default=None",
            '"BinderT_co", bound=Callable[..., _CapturedFunction] | None, covariant=False, '
            "default=None",
        ),
        (
            "_function_tools.py",
            '"BinderT_co", bound=Callable[..., _CapturedFunction] | None, covariant=True, '
            "default=None",
            '"BinderT_co", bound=Callable[..., _CapturedFunction] | None, covariant=True, '
            "default=Any",
        ),
        (
            "_function_tools.py",
            "    callback_timeout: float | None = None",
            "    callback_timeout: float | None = 10.0",
        ),
    ],
)
def test_api_changes_fail_without_rewriting_baseline(
    candidate: Path,
    path: str,
    before: str,
    after: str,
) -> None:
    source = implementation(candidate, path)
    text = source.read_text()
    assert before in text
    source.write_text(text.replace(before, after, 1))
    baseline = candidate / "packages/codex/tests/fixtures/released_api_contract.json"
    saved = baseline.read_bytes()
    result = check(candidate)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "differs from the reviewed snapshot" in result.stderr
    assert baseline.read_bytes() == saved


def test_supported_submodule_removal_fails(candidate: Path) -> None:
    (candidate / "src/agents/schema.py").unlink()
    result = check(candidate)
    assert result.returncode != 0


@pytest.mark.parametrize(
    "replacement",
    ["from .agent import Agent as NativeProvider", "from .input import InputText as UserInput"],
)
def test_root_binding_change_fails(candidate: Path, replacement: str) -> None:
    source = candidate / "src/agents/__init__.py"
    source.write_text(source.read_text() + f"\n{replacement}\n")
    assert check(candidate).returncode == 1


def test_public_facade_rebinding_fails_even_when_runtime_declaration_is_unchanged(
    candidate: Path,
) -> None:
    source = candidate / "src/agents/session.py"
    source.write_text(source.read_text() + "\nfrom .input import InputText as Session\n")
    assert check(candidate).returncode == 1


def test_empty_submodule_exports_are_not_inferred_from_remaining_classes(candidate: Path) -> None:
    source = candidate / "src/agents/observation.py"
    source.write_text(source.read_text() + "\n__all__ = []\n")
    assert check(candidate).returncode == 1


def test_public_facade_export_removal_fails(candidate: Path) -> None:
    source = candidate / "src/agents/observation.py"
    text = source.read_text()
    before = '    "OutputTextDelta",\n'
    assert before in text
    source.write_text(text.replace(before, "", 1))
    assert check(candidate).returncode == 1


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "TypeAliasType as _PublicTypeAliasType",
            "TypeAliasType as _DifferentConstructor",
        ),
        ('_type_alias_constructor("agents._events")', '_type_alias_constructor("agents.input")'),
    ],
)
def test_public_alias_constructor_pattern_cannot_change(
    candidate: Path, before: str, after: str
) -> None:
    source = implementation(candidate, "_events.py")
    text = source.read_text()
    assert before in text
    source.write_text(text.replace(before, after, 1))
    result = check(candidate)
    assert result.returncode != 0
    assert "Unrecognized public type-alias constructor" in result.stderr


def test_public_type_alias_must_keep_its_runtime_identity(candidate: Path) -> None:
    source = candidate / "src/agents/_events.py"
    source.write_text(
        source.read_text()
        + '\nEnvironmentStatus = TypeAliasType("EnvironmentStatus", EnvironmentStatus.__value__)\n'
    )
    result = check(candidate)
    assert result.returncode != 0
    assert "Public type alias agents._events.EnvironmentStatus differs" in result.stderr


def test_contract_comparison_ignores_only_redundant_import_aliases() -> None:
    comparison = runpy.run_path(str(SCRIPT))["_comparison_api"]
    api = {
        "root_imports": ["from typing import Any as Any"],
        "modules": {
            "agents.telemetry": {
                "imports": ["from ._trace_context import TraceContext as TraceContext"],
                "declarations": ["Alias = 'from typing import Any as Any'"],
            }
        },
        "root_definitions": {"agents._function_tools": {"imports": ["import typing as typing"]}},
    }
    expected = json.loads(json.dumps(api))
    expected["root_imports"] = ["from typing import Any"]
    expected["modules"]["agents.telemetry"]["imports"] = [
        "from ._trace_context import TraceContext"
    ]
    expected["root_definitions"]["agents._function_tools"]["imports"] = ["import typing"]
    assert comparison(api) == expected
    assert api["root_imports"] == ["from typing import Any as Any"]
    for actual, renamed in [
        ("from typing import Any", "from typing import Any as Object"),
        ("import typing", "import typing as types"),
        ("import os.path", "import os.path as path"),
    ]:
        original = {"root_imports": [actual], "modules": {}, "root_definitions": {}}
        changed = {**original, "root_imports": [renamed]}
        assert comparison(original) != comparison(changed)


@pytest.mark.parametrize("location", ["root_declarations", "modules", "root_definitions"])
def test_contract_comparison_normalizes_declarations_without_changing_values(location: str) -> None:
    comparison = runpy.run_path(str(SCRIPT))["_comparison_api"]

    def api(declaration: str) -> dict:
        result = {
            "root_imports": [],
            "root_declarations": [],
            "modules": {},
            "root_definitions": {},
        }
        if location == "root_declarations":
            result[location] = [declaration]
        else:
            result[location] = {"example": {"declarations": [declaration]}}
        return result

    original = api("Factory = lambda: 'lambda:'")
    spaced = api("Factory = lambda : 'lambda:'")
    assert comparison(original) == comparison(spaced)
    assert original != spaced
    assert comparison(original) != comparison(api("Factory = lambda: 'lambda :'"))
    assert comparison(original) != comparison(api("Factory = lambda value: 'lambda:'"))
    assert comparison(original) != comparison(api("Factory = lambda: None"))


def test_method_body_does_not_change_contract(candidate: Path) -> None:
    source = implementation(candidate, "session.py")
    text = source.read_text()
    before = 'self._check_callback_operation("start work in its own Session")'
    assert before in text
    source.write_text(text.replace(before, f"implementation_local = {before}", 1))
    result = check(candidate)
    assert result.returncode == 0, result.stderr


def test_unreferenced_private_signature_does_not_change_contract(candidate: Path) -> None:
    source = implementation(candidate, "tools.py")
    text = source.read_text()
    before = "def _function_parameters(\n    callback: Callable[..., Any],"
    assert before in text
    source.write_text(text.replace(before, "def _function_parameters(\n    callback: object,", 1))
    result = check(candidate)
    assert result.returncode == 0, result.stderr


def test_version_metadata_can_advance_without_changing_api(candidate: Path) -> None:
    project = candidate / "pyproject.toml"
    project.write_text(project.read_text().replace(VERSION, "999.0.0", 1))
    result = check(candidate, "999.0.0")
    assert result.returncode == 0, result.stderr


def test_current_snapshot_covers_native_methods_and_types(candidate: Path) -> None:
    result = check(candidate)
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(
        (candidate / "packages/codex/tests/fixtures/released_api_contract.json").read_text()
    )
    api = snapshot["public_api"]
    assert "agents.manual" in api["modules"]
    assert {"FunctionTool", "function_tool", "Tool", "ActionRequested", "ActionReply"} <= set(
        api["root_exports"]
    )
    assert {"ManualTool", "FunctionCallOutput"}.isdisjoint(api["root_exports"])
    assert "agents.observation" in api["modules"]
    assert "agents.state" in api["modules"]
    assert api["modules"]["agents.input"]["exports"]["UserInput"] == "UserInput"
    assert api["modules"]["agents.observation"]["exports"]["Observation"] == "Observation"
    session_api = "\n".join(api["modules"]["agents.session"]["declarations"])
    child_api = "\n".join(api["modules"]["agents.subagents"]["declarations"])
    turn_api = "\n".join(api["modules"]["agents.turn"]["declarations"])
    assert "async def submit(" in session_api and "async def start(" in child_api
    assert "async def submit(" not in child_api
    assert all(
        f"async def {name}(" not in session_api
        for name in ("start", "run", "prompt", "run_streamed", "interrupt")
    )
    assert "async def cancel_all(" in session_api and "async def try_cancel(" in child_api
    assert all(
        name in turn_api for name in ("async def steer(", "async def cancel(", "def events(")
    )
    assert any(
        "reasoning_effort: str" in item for item in api["modules"]["agents.agent"]["declarations"]
    )
    assert "AgentBase" not in api["root_exports"]
    assert "agents.tracing" not in api["modules"]


def test_release_checks_baseline_before_generating(tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-candidate.yml").read_text())
    preparation = next(
        step
        for step in workflow["jobs"]["contract"]["steps"]
        if step.get("name") == "Freeze and validate the candidate public API"
    )
    # Exercise the actual workflow command order: a failed baseline check must
    # prevent any generation, even though generation itself would succeed.
    (tmp_path / "Makefile").write_text(
        "check-released-api-contract:\n\t@exit 1\n"
        "update-released-api-contract:\n\t@touch generated\n"
    )
    commands = preparation["run"].splitlines()
    assert commands[0] == "uv sync --group dev --no-install-project"
    result = subprocess.run(
        ["bash", "-e", "-c", "\n".join(commands[1:])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert not (tmp_path / "generated").exists()
