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
pending = root / (step + '.child.tmp')
pending.write_text(str(os.getpid()))
pending.replace(root / (step + '.child'))
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
parent_marker = root / (step + '.started')
parent_pending = root / (step + '.started.tmp')
parent_pending.write_text(str(os.getpid()))
parent_pending.replace(parent_marker)
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
    if sys.platform == "linux":
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except (FileNotFoundError, ProcessLookupError):
            return False
        # Zombies have terminated even if the container's PID 1 has not reaped them.
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
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
        _await(
            lambda: (self.root / f"{step}.started").exists()
            and (self.root / f"{step}.child").exists()
        )

    def release(self, step: str, status: int = 0) -> None:
        pending = self.root / f"{step}.release.tmp"
        pending.write_text(str(status))
        pending.replace(self.root / f"{step}.release")

    def passed(self, step: str) -> None:
        _await(lambda: f"make {step} passed in " in self.output())

    def assert_stopped(self) -> None:
        for path in [*self.root.glob("*.started"), *self.root.glob("*.child")]:
            _await(lambda path=path: not _alive(int(path.read_text())))


@pytest.mark.parametrize(("state", "alive"), [("S", True), ("Z", False)])
def test_linux_liveness_distinguishes_zombies(
    monkeypatch: pytest.MonkeyPatch, state: str, alive: bool
) -> None:
    pid = os.getpid()

    def read_stat(path: Path) -> str:
        assert path == Path(f"/proc/{pid}/stat")
        return f"{pid} (fake (worker)) {state} 1 2 3"

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", read_stat)
    assert _alive(pid) is alive


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_linux_liveness_accepts_disappeared_process(
    monkeypatch: pytest.MonkeyPatch, error: type[OSError]
) -> None:
    def read_stat(path: Path) -> str:
        raise error(path)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(Path, "read_text", read_stat)
    assert not _alive(os.getpid())


@pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux waitid and procfs")
def test_stopped_assertion_accepts_unreaped_child(tmp_path: Path) -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    try:
        assert child.stdin is not None
        (tmp_path / "worker.child").write_text(str(child.pid))
        assert _alive(child.pid)
        child.stdin.close()
        # Observe termination without reaping, independently of PID 1's behavior.
        _await(
            lambda: os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None
        )
        os.kill(child.pid, 0)
        _Run(tmp_path, child).assert_stopped()
    finally:
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=10)


@contextmanager
def _run(
    root: Path, *, orphan_step: str = "", interrupt_before_wait: signal.Signals | None = None
) -> Iterator[_Run]:
    bin_path = root / "bin"
    bin_path.mkdir()
    make = bin_path / "make"
    make.write_text(f"#!{sys.executable}\n" + _MAKE)
    make.chmod(0o755)
    ps = bin_path / "ps"
    ps.write_text("#!/bin/sh\nexit 1\n")
    ps.chmod(0o755)
    environment = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "SYSTEMROOT", "LANG")
        if key in os.environ
    }
    environment.update(
        PATH=str(bin_path) + os.pathsep + environment.get("PATH", os.defpath),
        DRIVER_TEST_ROOT=str(root),
        ORPHAN_STEP=orphan_step,
    )
    command = ["bash", str(_SCRIPT)]
    if interrupt_before_wait is not None:
        # Inject cancellation after the status guard, before the real wait starts.
        command = [
            "bash",
            "-c",
            f"""
wait() {{
  if [ -z "${{cancel_sent:-}}" ]; then
    cancel_sent=1
    while [ ! -f "${{DRIVER_TEST_ROOT}}/format.started" ] ||
          [ ! -f "${{DRIVER_TEST_ROOT}}/format.child" ]; do
      sleep 0.01
    done
    kill -{interrupt_before_wait.name} "$$"
  fi
  builtin wait "$@"
}}
source "$1"
""",
            "bash",
            str(_SCRIPT),
        ]
    with (root / "output").open("wb") as output:
        process = subprocess.Popen(
            command,
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
                    # Descendants may still run after their group leader terminates.
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def test_ready_waits_for_parent_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        globals(),
        "_MAKE",
        _MAKE.replace(
            "parent_pending.replace(parent_marker)",
            "if step == 'format':\n"
            "    while not (root / 'parent-release').exists():\n"
            "        time.sleep(0.01)\n"
            "parent_pending.replace(parent_marker)",
        ),
    )
    with _run(tmp_path) as run:
        try:
            _await(lambda: (tmp_path / "format.child").exists())
            assert not (tmp_path / "format.started").exists()
            await_transition = _await

            def publish_parent(predicate: Callable[[], bool]) -> None:
                # The live worker alone must not release the ownership barrier.
                assert not predicate()
                (tmp_path / "parent-release").touch()
                await_transition(predicate)

            with monkeypatch.context() as readiness:
                readiness.setitem(globals(), "_await", publish_parent)
                run.ready("format")
            for step in _STEPS:
                run.ready(step)
                run.release(step)
                run.passed(step)
            assert run.process.wait(timeout=10) == 0, run.output()
            run.assert_stopped()
        finally:
            (tmp_path / "parent-release").touch()


def test_success_waits_for_each_step_without_ps(tmp_path: Path) -> None:
    with _run(tmp_path) as run:
        for index, step in enumerate(_STEPS):
            run.ready(step)
            assert run.process.poll() is None, run.output()
            assert "all commands passed" not in run.output()
            assert not (tmp_path / f"{step}.finished").exists()
            for later in _STEPS[index + 1 :]:
                assert not (tmp_path / f"{later}.started").exists()
            run.release(step)
            run.passed(step)
            assert (tmp_path / f"{step}.finished").exists()
            assert f"controlled {step} output" in run.output()
        assert run.process.wait(timeout=10) == 0, run.output()
        for step in _STEPS:
            assert run.output().count(f"make {step} passed in ") == 1
        assert run.output().count("all commands passed") == 1
        assert all(int(seconds) < 60 for seconds in re.findall(r"passed in (\d+)s", run.output()))
        run.assert_stopped()


@pytest.mark.parametrize("failing", _STEPS)
def test_failure_stops_before_the_next_step(tmp_path: Path, failing: str) -> None:
    with _run(tmp_path) as run:
        for step in _STEPS[: _STEPS.index(failing)]:
            run.ready(step)
            run.release(step)
            run.passed(step)
        run.ready(failing)
        run.release(failing, 23)
        assert run.process.wait(timeout=10) == 23, run.output()
        assert f"make {failing} failed with exit code 23" in run.output()
        assert f"controlled {failing} output" in run.output()
        assert "all commands passed" not in run.output()
        for later in _STEPS[_STEPS.index(failing) + 1 :]:
            assert not (tmp_path / f"{later}.started").exists()
        run.assert_stopped()


def test_step_completion_cleans_descendants_before_the_next_step(tmp_path: Path) -> None:
    with _run(tmp_path, orphan_step="format") as run:
        run.ready("format")
        worker = int((tmp_path / "format.child").read_text())
        run.release("format")
        run.ready("lint")
        assert not _alive(worker)
        for step in _STEPS[1:]:
            run.ready(step)
            run.release(step)
            run.passed(step)
        assert run.process.wait(timeout=10) == 0, run.output()
        run.assert_stopped()


def test_harness_cleans_descendants_after_the_group_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the orphan alive even when the harness releases its normal exit gate.
    monkeypatch.setitem(
        globals(),
        "_MAKE",
        _MAKE.replace(
            "while not (root / release).exists():",
            "while step == 'format' or not (root / release).exists():",
        ),
    )
    with _run(tmp_path, orphan_step="format") as run:
        run.ready("format")
        run.process.kill()
        run.process.wait(timeout=10)
        run.release("format")
        leader = int((tmp_path / "format.started").read_text())
        worker = int((tmp_path / "format.child").read_text())
        _await(lambda: not _alive(leader))
        assert _alive(worker)
    run.assert_stopped()


@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_cancellation_before_wait_stops_the_active_step(
    tmp_path: Path, interrupt: signal.Signals
) -> None:
    with _run(tmp_path, interrupt_before_wait=interrupt) as run:
        assert run.process.wait(timeout=10) == 128 + interrupt, run.output()
        assert (tmp_path / "format.terminated").exists()
        assert not (tmp_path / "format.finished").exists()
        assert not (tmp_path / "lint.started").exists()
        assert "all commands passed" not in run.output()
        run.assert_stopped()


@pytest.mark.parametrize("phase", ["format", "tests"])
@pytest.mark.parametrize("interrupt", [signal.SIGINT, signal.SIGTERM])
def test_cancellation_reaps_owned_processes(
    tmp_path: Path, phase: str, interrupt: signal.Signals
) -> None:
    with _run(tmp_path) as run:
        for step in _STEPS[: _STEPS.index(phase)]:
            run.ready(step)
            run.release(step)
            run.passed(step)
        run.ready(phase)
        run.process.send_signal(interrupt)
        _await(lambda: (tmp_path / f"{phase}.terminated").exists())
        # A second signal during cleanup must preserve the original exit status.
        run.process.send_signal(signal.SIGTERM)
        assert run.process.wait(timeout=10) == 128 + interrupt, run.output()
        assert "all commands passed" not in run.output()
        for later in _STEPS[_STEPS.index(phase) + 1 :]:
            assert not (tmp_path / f"{later}.started").exists()
        run.assert_stopped()
