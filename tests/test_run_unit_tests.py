from __future__ import annotations

import importlib.util
import os
import signal
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def test_runner() -> ModuleType:
    path = Path(__file__).parents[1] / ".github" / "scripts" / "run_unit_tests.py"
    spec = importlib.util.spec_from_file_location("run_unit_tests_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_discovers_both_default_pytest_filename_patterns(
    test_runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_prefix.py").write_text("def test_prefix(): pass\n", encoding="utf-8")
    (tests / "suffix_test.py").write_text(
        "import pytest\npytestmark = pytest.mark.serial\n",
        encoding="utf-8",
    )
    (tests / "helper.py").write_text("HELPER = True\n", encoding="utf-8")
    monkeypatch.setattr(test_runner, "ROOT", tmp_path)

    assert [path.name for path in test_runner._test_files()] == [
        "suffix_test.py",
        "test_prefix.py",
    ]
    assert [path.name for path in test_runner._serial_test_files()] == ["suffix_test.py"]


def test_failure_status_is_propagated(
    test_runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(test_runner, "TERMINATION_GRACE_SECONDS", 0.01)
    with test_runner._ProcessSupervisor() as supervisor:
        supervisor.start(
            "failing tests",
            [sys.executable, "-c", "raise SystemExit(7)"],
            log_path=tmp_path / "failure.log",
        )

        assert supervisor.wait() is False


@pytest.mark.skipif(os.name != "posix", reason="Signal delivery requires POSIX.")
def test_signal_during_launch_is_handled_after_child_registration(
    test_runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = test_runner.subprocess.Popen
    launched_processes: list[Any] = []

    def signal_after_launch(*args: Any, **kwargs: Any) -> Any:
        process = real_popen(*args, **kwargs)
        launched_processes.append(process)
        os.kill(os.getpid(), test_runner.signal.SIGTERM)
        return process

    monkeypatch.setattr(test_runner, "TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(test_runner.subprocess, "Popen", signal_after_launch)

    with pytest.raises(SystemExit) as raised:
        with test_runner._ProcessSupervisor() as supervisor:
            supervisor.start(
                "tests",
                [sys.executable, "-c", "import time; time.sleep(30)"],
                log_path=tmp_path / "tests.log",
            )

    assert raised.value.code == 128 + test_runner.signal.SIGTERM
    assert len(launched_processes) == 1
    assert launched_processes[0].poll() is not None


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="Signal masks require pthread_sigmask.",
)
def test_child_does_not_inherit_blocked_signals(test_runner: ModuleType, tmp_path: Path) -> None:
    log_path = tmp_path / "mask.log"
    child_code = """
import signal

mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
print(" ".join(str(int(signum)) for signum in sorted(mask, key=int)))
"""

    with test_runner._ProcessSupervisor() as supervisor:
        supervisor.start(
            "signal mask",
            [sys.executable, "-c", child_code],
            log_path=log_path,
        )
        assert supervisor.wait() is True

    child_mask = {int(value) for value in log_path.read_text(encoding="utf-8").split()}
    assert int(test_runner.signal.SIGINT) not in child_mask
    assert int(test_runner.signal.SIGTERM) not in child_mask


@pytest.mark.skipif(os.name != "posix", reason="Signal delivery requires POSIX.")
def test_run_all_replays_output_when_signaled_during_wait(
    test_runner: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ready_path = tmp_path / "serial-ready"
    parallel_code = """
import os
import signal
import sys
import time

while not os.path.exists(sys.argv[1]):
    time.sleep(0.01)
print("parallel output before signal", flush=True)
os.kill(os.getppid(), signal.SIGTERM)
time.sleep(30)
"""
    serial_code = """
import sys
import time

open(sys.argv[1], "w").write("ready")
time.sleep(30)
"""
    monkeypatch.setattr(test_runner, "TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        test_runner,
        "_parallel_args",
        lambda: [sys.executable, "-c", parallel_code, str(ready_path)],
    )
    monkeypatch.setattr(
        test_runner,
        "_serial_args",
        lambda: [sys.executable, "-c", serial_code, str(ready_path)],
    )

    with pytest.raises(SystemExit) as raised:
        test_runner._run_all()

    output = capsys.readouterr().out
    assert raised.value.code == 128 + signal.SIGTERM
    assert "===== parallel tests =====" in output
    assert "parallel output before signal" in output


def test_run_all_replays_output_when_second_launch_fails(
    test_runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_popen = test_runner.subprocess.Popen
    launch_count = 0
    first_log_path: Path | None = None

    def fail_second_launch(*args: Any, **kwargs: Any) -> Any:
        nonlocal first_log_path, launch_count
        launch_count += 1
        if launch_count == 2:
            assert first_log_path is not None
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if "parallel output before failure" in first_log_path.read_text(encoding="utf-8"):
                    break
                time.sleep(0.01)
            else:
                pytest.fail("The first child did not produce output before launch failure.")
            raise OSError("expected launch failure")
        first_log_path = Path(kwargs["stdout"].name)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(test_runner, "TERMINATION_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        test_runner,
        "_parallel_args",
        lambda: [
            sys.executable,
            "-c",
            "import time; print('parallel output before failure', flush=True); time.sleep(30)",
        ],
    )
    monkeypatch.setattr(test_runner.subprocess, "Popen", fail_second_launch)

    with pytest.raises(OSError, match="expected launch failure"):
        test_runner._run_all()

    output = capsys.readouterr().out
    assert "===== parallel tests =====" in output
    assert "parallel output before failure" in output


@pytest.mark.skipif(os.name != "posix", reason="Process groups require POSIX.")
def test_cleanup_kills_owned_descendants(
    test_runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(test_runner, "TERMINATION_GRACE_SECONDS", 0.05)
    ready_path = tmp_path / "ready"
    parent_code = """
import subprocess
import sys
import time

subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1], 'w').write(str(__import__('os').getpid())); "
        "time.sleep(30)",
        sys.argv[1],
    ]
)
time.sleep(30)
"""

    with test_runner._ProcessSupervisor() as supervisor:
        supervisor.start(
            "process tree",
            [sys.executable, "-c", parent_code, str(ready_path)],
            log_path=tmp_path / "tree.log",
        )
        deadline = time.monotonic() + 2
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready_path.exists()
        descendant_pid = int(ready_path.read_text(encoding="utf-8"))

        supervisor._cleanup()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(descendant_pid, 0)
        except (PermissionError, ProcessLookupError):
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"Descendant process {descendant_pid} survived cleanup.")


def test_parallel_command_keeps_xdist_worksteal(
    test_runner: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(test_runner, "_color_args", lambda: ["--color=yes"])

    assert test_runner._parallel_args() == [
        sys.executable,
        "-m",
        "pytest",
        "--color=yes",
        "-n",
        "auto",
        "--dist",
        "worksteal",
        "-m",
        "not serial",
    ]
