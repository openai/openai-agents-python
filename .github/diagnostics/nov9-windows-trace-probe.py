"""Run the unchanged native deadline case once under its original 45s cap."""

import asyncio
import dis
import faulthandler
import hashlib
import os
import subprocess
import sys
import traceback
from contextvars import Context
from pathlib import Path

PREFIX = "Trace probe "
WARMUP_TOOL_ID = 3
TRANSPORT_PHASES = {
    "AsyncHTTPConnection._connect": ("/httpcore2/_async/connection.py", "connect"),
    "AsyncHTTPConnection.aclose": ("/httpcore2/_async/connection.py", "close"),
    "AsyncHTTP11Connection._send_request_headers": ("/httpcore2/_async/http11.py", "send-request"),
    "AsyncHTTP11Connection._send_request_body": ("/httpcore2/_async/http11.py", "send-request"),
    "AsyncHTTP11Connection._receive_response_headers": (
        "/httpcore2/_async/http11.py",
        "read-response-headers",
    ),
    "AsyncHTTP11Connection._response_closed": ("/httpcore2/_async/http11.py", "close"),
    "AsyncClient.__aexit__": ("/httpx2/_client.py", "close"),
    "Response.aclose": ("/httpx2/_models.py", "close"),
}


def mark(message):
    print(PREFIX + message, flush=True)


def observe_warmup(exporter):
    monitoring = sys.monitoring
    events = monitoring.events
    boundary = exporter.on_trace_events.__code__
    send = exporter._send.__code__
    wait_for = asyncio.wait_for.__code__
    timeout_exit = asyncio.Timeout.__aexit__.__code__
    # In 3.14 wait_for raises directly for a nonpositive budget and uses
    # Timeout.__aexit__ for a positive budget.
    direct_raises = {
        instruction.offset
        for instruction in dis.get_instructions(wait_for)
        if instruction.opname == "RAISE_VARARGS"
    }
    observations = []

    def record(phase, error):
        if isinstance(error, (StopIteration, StopAsyncIteration, GeneratorExit)):
            return
        entry = (phase, type(error).__name__)
        if len(observations) < 12 and entry not in observations:
            observations.append(entry)

    def transport(code, error):
        location = TRANSPORT_PHASES.get(code.co_qualname)
        if location and code.co_filename.replace("\\", "/").endswith(location[0]):
            record(location[1], error)

    def raised(code, offset, error):
        if code is send:
            # These lines cover the client and stream in the pinned SDK.
            # The SDK raises its sanitized error below this range.
            if any(
                start <= offset < end and line in range(373, 387)
                for start, end, line in send.co_lines()
            ):
                record("exporter-call", error)
        elif type(error) is TimeoutError:
            if code is timeout_exit:
                wait_frame = sys._getframe(1).f_back
                if wait_frame is None or wait_frame.f_code is not wait_for:
                    return
                caller = wait_frame.f_back
            elif code is wait_for and offset in direct_raises:
                caller = sys._getframe(1).f_back
            else:
                return
            if caller and caller.f_code is boundary:
                record("wait-for-deadline", error)
        elif isinstance(error, asyncio.CancelledError):
            # A transport coroutine may handle the cancellation and return.
            transport(code, error)

    def report():
        details = ", ".join(f"{phase} {name}" for phase, name in observations)
        mark("warmup observations " + (details or "none"))

    def finish(code, _offset, _value):
        monitoring.set_events(WARMUP_TOOL_ID, events.NO_EVENTS)
        monitoring.set_local_events(WARMUP_TOOL_ID, code, events.NO_EVENTS)
        # Print after control returns to the loop.
        asyncio.get_running_loop().call_soon(report, context=Context())

    def unwound(code, offset, error):
        if code is boundary:
            finish(code, offset, error)
        else:
            transport(code, error)

    def start(_code, _offset):
        monitoring.set_events(WARMUP_TOOL_ID, events.RAISE | events.PY_UNWIND)

    monitoring.use_tool_id(WARMUP_TOOL_ID, "native deadline warmup")
    monitoring.register_callback(WARMUP_TOOL_ID, events.PY_START, start)
    monitoring.register_callback(WARMUP_TOOL_ID, events.PY_RETURN, finish)
    monitoring.register_callback(WARMUP_TOOL_ID, events.RAISE, raised)
    monitoring.register_callback(WARMUP_TOOL_ID, events.PY_UNWIND, unwound)
    monitoring.set_local_events(WARMUP_TOOL_ID, boundary, events.PY_START | events.PY_RETURN)


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


async def run_case(fixture):
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
        from tests import test_native_trace_exporter as fixture

        observe_warmup(fixture.telemetry.OpenAITraceExporter)
        try:
            asyncio.run(run_case(fixture))
        finally:
            sys.monitoring.free_tool_id(WARMUP_TOOL_ID)
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
