from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Bash process-group runner")
_SCRIPT = (
    Path(__file__).resolve().parents[1] / ".agents/skills/code-change-verification/scripts/run.sh"
)
_STEPS = ("format", "lint", "typecheck", "tests")

# Each fake make waits for an explicit release, and owns a real child process.
_MAKE = r'''
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

root = Path(os.environ["DRIVER_TEST_ROOT"])
step = sys.argv[1]
child_code = """
import os, signal, sys, time
from pathlib import Path
root, step = Path(sys.argv[1]), sys.argv[2]
def terminate(*_):
    (root / (step + '.terminated')).touch()
    sys.exit(0)
signal.signal(signal.SIGTERM, terminate)
(root / (step + '.child')).write_text(str(os.getpid()))
release = step + ('.child-release' if os.environ.get('ORPHAN_STEP') == step else '.release')
while not (root / release).exists():
    time.sleep(0.01)
"""
child = subprocess.Popen([sys.executable, "-c", child_code, str(root), step])
def terminate(*_):
    # Reap the worker so process absence is observable on every supported OS.
    child.wait(timeout=5)
    sys.exit(143)
signal.signal(signal.SIGTERM, terminate)
(root / (step + '.started')).write_text(str(os.getpid()))
if os.environ.get('ORPHAN_STEP') == step:
    while not (root / (step + '.release')).exists():
        time.sleep(0.01)
    sys.exit(0)
child.wait()
(root / (step + '.finished')).touch()
status = int((root / (step + '.release')).read_text())
print('controlled ' + step + ' output', flush=True)
sys.exit(status)
'''


def _await(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 15
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for a controlled process transition")
        time.sleep(0.01)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class _Run:
    def __init__(self, root: Path, process: subprocess.Popen[bytes]) -> None:
        self.root = root
        self.process = process

    def output(self) -> str:
        return (self.root / "output").read_text()

    def ready(self, step: str) -> None:
        _await(lambda: (self.root / f"{step}.child").exists())

    def release(self, step: str, status: int = 0) -> None:
        (self.root / f"{step}.release").write_text(str(status))

    def passed(self, step: str) -> None:
        _await(lambda: f"make {step} passed in " in self.output())

    def assert_stopped(self) -> None:
        for path in [*self.root.glob("*.started"), *self.root.glob("*.child")]:
            _await(lambda path=path: not _alive(int(path.read_text())))


@contextmanager
def _run(root: Path, *, orphan_step: str = "", block_date: bool = False) -> Iterator[_Run]:
    bin_path = root / "bin"
    bin_path.mkdir()
    make = bin_path / "make"
    make.write_text(f"#!{sys.executable}\n" + _MAKE)
    make.chmod(0o755)
    ps = bin_path / "ps"
    ps.write_text("#!/bin/sh\nexit 1\n")
    ps.chmod(0o755)
    if block_date:
        date = bin_path / "date"
        date.write_text(
            f"#!{sys.executable}\n"
            "import os, signal\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
            "(Path(os.environ['DRIVER_TEST_ROOT']) / 'date.marker').touch()\n"
            "signal.pause()\n"
        )
        date.chmod(0o755)
    environment = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "SYSTEMROOT", "LANG")
        if key in os.environ
    }
    environment.update(
        PATH=str(bin_path) + os.pathsep + environment.get("PATH", os.defpath),
        DRIVER_TEST_ROOT=str(root),
        CODE_CHANGE_VERIFICATION_HEARTBEAT_SECONDS="1",
        ORPHAN_STEP=orphan_step,
    )
    with (root / "output").open("wb") as output:
        process = subprocess.Popen(
            ["bash", str(_SCRIPT)],
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        run = _Run(root, process)
        try:
            yield run
        finally:
            # Release gates even after failed assertions, then stop only owned groups.
            for step in _STEPS:
                run.release(step)
                (root / f"{step}.child-release").touch()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            finally:
                for path in root.glob("*.started"):
                    pid = int(path.read_text())
                    if _alive(pid):
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


@pytest.mark.parametrize("first", ["lint", "typecheck"])
def test_success_waits_for_every_step_without_ps(tmp_path: Path, first: str) -> None:
    with _run(tmp_path) as run:
        run.ready("format")
        assert not (tmp_path / "lint.started").exists()
        run.release("format")
        for step in _STEPS[1:]:
            run.ready(step)
        _await(lambda: "still running: lint" in run.output())
        run.release(first)
        run.passed(first)
        other = "typecheck" if first == "lint" else "lint"
        run.release(other)
        run.passed(other)
        _await(lambda: "still running: tests" in run.output())
        assert run.process.poll() is None, run.output()
        assert "all commands passed" not in run.output()
        assert not (tmp_path / "tests.finished").exists()
        run.release("tests")
        assert run.process.wait(timeout=10) == 0, run.output()
        assert (tmp_path / "tests.finished").exists()
        for step in _STEPS:
            assert run.output().count(f"make {step} passed in ") == 1
        assert run.output().count("all commands passed") == 1
        assert all(int(seconds) < 60 for seconds in re.findall(r"passed in (\d+)s", run.output()))
        run.assert_stopped()


@pytest.mark.parametrize("failing", ["format", "lint", "tests"])
def test_failure_reaps_remaining_steps_and_preserves_status(tmp_path: Path, failing: str) -> None:
    with _run(tmp_path) as run:
        run.ready("format")
        if failing != "format":
            run.release("format")
            for step in _STEPS[1:]:
                run.ready(step)
        run.release(failing, 23)
        assert run.process.wait(timeout=10) == 23, run.output()
        assert f"make {failing} failed with exit code 23" in run.output()
        assert f"controlled {failing} output" in run.output()
        assert "all commands passed" not in run.output()
        if failing == "format":
            assert not (tmp_path / "lint.started").exists()
        run.assert_stopped()


def test_successful_leader_with_running_child_is_rejected(tmp_path: Path) -> None:
    with _run(tmp_path, orphan_step="tests") as run:
        run.ready("format")
        run.release("format")
        for step in _STEPS[1:]:
            run.ready(step)
        run.release("tests")
        assert run.process.wait(timeout=10) == 1, run.output()
        assert "make tests left running processes" in run.output()
        assert "all commands passed" not in run.output()
        run.assert_stopped()


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_cancellation_during_launch_reaps_registered_processes(
    tmp_path: Path, interrupt: signal.Signals
) -> None:
    with _run(tmp_path, block_date=True) as run:
        run.ready("format")
        _await(lambda: (tmp_path / "date.marker").exists())
        # Interrupt the timestamp subprocess while the formatter has its own group.
        os.killpg(run.process.pid, interrupt)
        assert run.process.wait(timeout=10) == 128 + interrupt, run.output()
        assert "all commands passed" not in run.output()
        run.assert_stopped()


@pytest.mark.parametrize("phase", ["format", "parallel"])
@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_cancellation_reaps_owned_processes(
    tmp_path: Path, phase: str, interrupt: signal.Signals
) -> None:
    with _run(tmp_path) as run:
        run.ready("format")
        active = "format"
        if phase == "parallel":
            run.release("format")
            for step in _STEPS[1:]:
                run.ready(step)
            run.release("lint")
            run.passed("lint")
            active = "tests"
        run.process.send_signal(interrupt)
        _await(lambda: (tmp_path / f"{active}.terminated").exists())
        # A second signal during cleanup must preserve the original exit status.
        run.process.send_signal(signal.SIGTERM)
        assert run.process.wait(timeout=10) == 128 + interrupt, run.output()
        assert "all commands passed" not in run.output()
        run.assert_stopped()
