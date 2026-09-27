from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

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


def test_windows_runner_uses_subprocess_and_propagates_exit_code(
    serial_test_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[list[str], bool]] = []

    def capture_run(command: list[str], *, check: bool) -> SimpleNamespace:
        captured.append((command, check))
        return SimpleNamespace(returncode=23)

    monkeypatch.setattr(serial_test_runner.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", capture_run)
    monkeypatch.setattr(serial_test_runner.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        serial_test_runner.os,
        "execv",
        lambda *_args: pytest.fail("Windows serial runner must not call os.execv"),
    )
    monkeypatch.setattr(sys, "argv", ["run_serial_tests.py"])

    with pytest.raises(SystemExit) as exc_info:
        serial_test_runner.main()

    assert exc_info.value.code == 23
    assert len(captured) == 1
    assert captured[0][1] is False


def test_non_windows_runner_still_execs(
    serial_test_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[list[str]] = []

    monkeypatch.setattr(serial_test_runner.sys, "platform", "linux")
    monkeypatch.setattr(serial_test_runner.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("POSIX serial runner must not call subprocess.run"),
    )
    monkeypatch.setattr(
        serial_test_runner.os, "execv", lambda _executable, command: captured.append(command)
    )
    monkeypatch.setattr(sys, "argv", ["run_serial_tests.py"])

    serial_test_runner.main()

    assert len(captured) == 1
    assert "serial" in captured[0]
