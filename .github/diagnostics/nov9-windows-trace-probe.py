"""Run the unchanged native deadline case once under its original 45s cap."""

import asyncio
import faulthandler
import hashlib
import os
import subprocess
import sys
import traceback
from pathlib import Path

PREFIX = "Trace probe "


def mark(message):
    print(PREFIX + message, flush=True)


def dump_tasks(loop):
    for task in asyncio.all_tasks(loop):
        current = task.get_coro()
        frames = []
        while current is not None:
            frame = getattr(current, "cr_frame", None) or getattr(current, "gi_frame", None)
            if frame is not None:
                name = Path(frame.f_code.co_filename).name
                frames.append(f"{name}:{frame.f_lineno}:{frame.f_code.co_name}")
            current = getattr(current, "cr_await", None) or getattr(current, "gi_yieldfrom", None)
        mark("await stack " + " -> ".join(frames))
    loop.call_later(3, dump_tasks, loop)


def report_exception(error):
    frames = traceback.extract_tb(error.__traceback__)
    locations = " -> ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)
    mark(f"{type(error).__name__} at {locations}")


def loop_exception(_loop, context):
    error = context.get("exception")
    if error is not None:
        report_exception(error)
    else:
        mark("event-loop exception without traceback")


async def run_case():
    from tests import test_native_trace_exporter as fixture

    mark("entering native_deadline_case")
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(loop_exception)
    loop.call_later(3, dump_tasks, loop)
    await fixture.native_deadline_case()
    mark("case body returned")


def child():
    mark("child started")
    faulthandler.dump_traceback_later(12, repeat=True)
    try:
        asyncio.run(run_case())
        mark("asyncio.run returned")
    except Exception as error:
        report_exception(error)
        return 1
    finally:
        faulthandler.cancel_dump_traceback_later()
    return 0


def emit_stacks(stdout, stderr):
    for line in stdout.splitlines():
        if line.startswith(PREFIX.encode()):
            print(line.decode("utf-8", errors="replace"), flush=True)
    for line in stderr.splitlines():
        line = line.decode("utf-8", errors="replace")
        if line.startswith(("Timeout (", "Thread 0x", "Current thread 0x")):
            print(line, flush=True)
        elif line.lstrip().startswith('File "'):
            # Only faulthandler stack frames. Avoid full local paths.
            file, _, location = line.lstrip()[6:].partition('"')
            if location.startswith(", line "):
                print(Path(file).name + location, flush=True)


def parent():
    import agents._native as addon

    assert sys.version_info[:3] == (3, 14, 10)
    with Path(addon.__file__).open("rb") as binary:
        assert hashlib.file_digest(binary, "sha256").hexdigest() == os.environ["NOV9_VERIFIED_ADDON_SHA"]
    assert Path(addon.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
    mark("CPython 3.14.10; installed native addon hash matched")
    test_root = Path.cwd() / "packages" / "codex"
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONPATH"] = str(test_root)
    env["PYO3_PYTHON"] = sys.executable
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    env.setdefault("RUST_MIN_STACK", str(16 * 1024 * 1024))
    try:
        result = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).resolve()), "--child"],
            env=env,
            capture_output=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired as error:
        emit_stacks(error.stdout or b"", error.stderr or b"")
        mark("native_deadline_case exceeded 45 seconds")
        return 1
    emit_stacks(result.stdout, result.stderr)
    mark(f"native_deadline_case exit {result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(child() if sys.argv[1:] == ["--child"] else parent())
