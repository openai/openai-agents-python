from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SERIAL_MARKER_SOURCE = ".".join(("pytest", "mark", "serial"))
REVIEW_OPTIONAL_MARKER_SOURCE = ".".join(("pytest", "mark", "review_optional"))


@pytest.fixture
def serial_test_runner() -> ModuleType:
    path = Path(__file__).parents[1] / ".github" / "scripts" / "run_serial_tests.py"
    spec = importlib.util.spec_from_file_location("run_serial_tests_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runner_tests_do_not_self_select_as_serial() -> None:
    contents = Path(__file__).read_text(encoding="utf-8")

    assert SERIAL_MARKER_SOURCE not in contents
    assert REVIEW_OPTIONAL_MARKER_SOURCE not in contents


def test_discovers_both_default_pytest_filename_patterns(
    serial_test_runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_prefix.py").write_text("def test_prefix(): pass\n", encoding="utf-8")
    (tests / "suffix_test.py").write_text(
        f"import pytest\npytestmark = {SERIAL_MARKER_SOURCE}\n",
        encoding="utf-8",
    )
    (tests / "helper.py").write_text("HELPER = True\n", encoding="utf-8")
    monkeypatch.setattr(serial_test_runner, "ROOT", tmp_path)

    assert [path.name for path in serial_test_runner._test_files()] == [
        "suffix_test.py",
        "test_prefix.py",
    ]
    assert [path.name for path in serial_test_runner._serial_test_files()] == ["suffix_test.py"]


def test_review_selection_keeps_mixed_serial_files(
    serial_test_runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    mixed_file = tests / "test_mixed.py"
    mixed_file.write_text(
        "import pytest\n"
        f"pytestmark = {SERIAL_MARKER_SOURCE}\n"
        f"@{REVIEW_OPTIONAL_MARKER_SOURCE}\n"
        "def test_optional(): pass\n"
        "def test_required(): pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(serial_test_runner, "ROOT", tmp_path)

    assert [path.name for path in serial_test_runner._serial_test_files()] == ["test_mixed.py"]
    assert serial_test_runner._serial_args(marker_expression="serial and not review_optional") == [
        sys.executable,
        "-m",
        "pytest",
        str(Path("tests") / "test_mixed.py"),
        "-m",
        "serial and not review_optional",
    ]


def test_serial_command_targets_only_discovered_files(
    serial_test_runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serial_file = tmp_path / "tests" / "test_serial.py"
    serial_file.parent.mkdir()
    serial_file.write_text(
        f"import pytest\npytestmark = {SERIAL_MARKER_SOURCE}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(serial_test_runner, "ROOT", tmp_path)

    assert serial_test_runner._serial_args() == [
        sys.executable,
        "-m",
        "pytest",
        str(Path("tests") / "test_serial.py"),
        "-m",
        "serial",
    ]
