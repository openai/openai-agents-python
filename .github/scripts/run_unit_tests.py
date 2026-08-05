from __future__ import annotations

import argparse
import fnmatch
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PYTEST_FILE_PATTERNS = ("test_*.py", "*_test.py")
SERIAL_MARKER = "pytest.mark.serial"
TERMINATION_GRACE_SECONDS = 1.0
HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM)
KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _test_files() -> list[Path]:
    return sorted(
        path
        for path in (ROOT / "tests").rglob("*.py")
        if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in PYTEST_FILE_PATTERNS)
    )


def _serial_test_files() -> list[Path]:
    return [path for path in _test_files() if SERIAL_MARKER in path.read_text(encoding="utf-8")]


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _color_args() -> list[str]:
    return ["--color=yes"] if sys.stdout.isatty() else []


def _parallel_args() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *_color_args(),
        "-n",
        "auto",
        "--dist",
        "worksteal",
        "-m",
        "not serial",
    ]


def _serial_args() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        *_color_args(),
        *(_relative(path) for path in _serial_test_files()),
        "-m",
        "serial",
    ]


def _print_log(name: str, path: Path) -> None:
    print(f"\n===== {name} =====")
    output = path.read_text(encoding="utf-8")
    print(output, end="" if output.endswith("\n") else "\n")


@dataclass
class _ChildProcess:
    name: str
    process: subprocess.Popen[str]
    log: Any
    log_path: Path
    allowed_exit_codes: frozenset[int]


class _ProcessSupervisor:
    def __init__(self) -> None:
        self.children: list[_ChildProcess] = []
        self._previous_handlers: dict[signal.Signals, Any] = {}
        self._pending_signal: int | None = None

    def __enter__(self) -> _ProcessSupervisor:
        def handle_signal(signum: int, _frame: Any) -> None:
            if self._pending_signal is None:
                self._pending_signal = signum

        for signum in HANDLED_SIGNALS:
            self._previous_handlers[signum] = signal.signal(signum, handle_signal)
        return self

    def __exit__(self, exc_type: Any, _exc: Any, _traceback: Any) -> None:
        if exc_type is not None:
            self._cleanup()
        for signum, handler in self._previous_handlers.items():
            signal.signal(signum, handler)
        if exc_type is None:
            self._raise_pending_signal()

    def start(
        self,
        name: str,
        args: Sequence[str],
        *,
        log_path: Path,
        allowed_exit_codes: Iterable[int] = (0,),
    ) -> None:
        log = log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                args,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=os.name == "posix",
            )
            self.children.append(
                _ChildProcess(
                    name=name,
                    process=process,
                    log=log,
                    log_path=log_path,
                    allowed_exit_codes=frozenset(allowed_exit_codes),
                )
            )
        except BaseException:
            log.close()
            raise
        finally:
            self._raise_pending_signal()

    def wait(self) -> bool:
        succeeded = True
        pending = list(self.children)
        while pending:
            self._raise_pending_signal()
            for child in list(pending):
                status = child.process.poll()
                if status is None:
                    continue
                child.log.close()
                pending.remove(child)
                if status not in child.allowed_exit_codes:
                    succeeded = False
                    print(f"{child.name} failed with status {status}.", file=sys.stderr)
            if pending:
                time.sleep(0.02)
        self._raise_pending_signal()
        if not succeeded:
            self._cleanup()
        return succeeded

    def _raise_pending_signal(self) -> None:
        if self._pending_signal is None:
            return
        signum = self._pending_signal
        self._pending_signal = None
        self._cleanup()
        raise SystemExit(128 + signum)

    @staticmethod
    def _signal_group(child: _ChildProcess, signum: int) -> bool:
        try:
            if os.name == "posix":
                os.killpg(child.process.pid, signum)
            elif signum == signal.SIGTERM:
                child.process.terminate()
            else:
                child.process.kill()
        except (PermissionError, ProcessLookupError):
            return False
        return True

    def _cleanup(self) -> None:
        if not self.children:
            return
        active_groups = [
            child for child in self.children if self._signal_group(child, signal.SIGTERM)
        ]
        if active_groups:
            time.sleep(TERMINATION_GRACE_SECONDS)
            for child in active_groups:
                self._signal_group(child, KILL_SIGNAL)
        for child in self.children:
            child.process.wait()
            if not child.log.closed:
                child.log.close()


def _run_all() -> int:
    with tempfile.TemporaryDirectory(prefix="openai-agents-tests-") as directory:
        temporary_root = Path(directory)
        supervisor = _ProcessSupervisor()
        try:
            with supervisor:
                supervisor.start(
                    "parallel tests",
                    _parallel_args(),
                    log_path=temporary_root / "parallel.log",
                )
                supervisor.start(
                    "serial tests",
                    _serial_args(),
                    log_path=temporary_root / "serial.log",
                )
                succeeded = supervisor.wait()
                return 0 if succeeded else 1
        finally:
            for child in supervisor.children:
                if not child.log.closed:
                    child.log.close()
                _print_log(child.name, child.log_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial-only", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.serial_only:
        os.execv(sys.executable, _serial_args())
    return _run_all()


if __name__ == "__main__":
    raise SystemExit(main())
