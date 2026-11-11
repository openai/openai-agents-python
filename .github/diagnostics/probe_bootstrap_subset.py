#!/usr/bin/env python3
"""Probe bootstrap_subset on macOS 15.8.2 (24H232) arm64 with stock /usr/bin/true."""

from contextlib import ExitStack
import ctypes as ct
import json
import os
import signal
import subprocess
import sys

EXPECTED_OS = ("15.8.2", "24H232")
DEADLINE_SECONDS = 10
STOP = 2

TASK_BOOTSTRAP_PORT = 4
MACH_PORT_RIGHT_RECEIVE = 1
MACH_MSG_TYPE_MAKE_SEND = 20
MACH_PORT_TYPE_SEND = 1 << 16
MACH_PORT_TYPE_DEAD_NAME = 1 << 20
INVALID_PORT_NAMES = (0, 0xFFFFFFFF)


class ProbeStop(Exception):
    def __init__(self, phase, reason, **details):
        self.record = dict(phase=phase, reason=reason, **details)


def report(status, phase, **details):
    print(json.dumps(dict(status=status, phase=phase, **details)), flush=True)


def check(phase, code_kind, code):
    if code:
        raise ProbeStop(phase, "call failed", code_kind=code_kind, code=code)


def platform_gate():
    host = os.uname()
    if sys.platform != "darwin" or host.machine != "arm64":
        raise ProbeStop("platform", "requires Darwin arm64", system=host.sysname, arch=host.machine)
    if os.getuid() == 0 or os.geteuid() == 0:
        raise ProbeStop("platform", "requires an unprivileged account")
    if sys.version_info < (3, 10):
        raise ProbeStop("platform", "requires Python 3.10 or later")
    return host.machine


def probe(cleanup_errors):
    arch = platform_gate()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    report("running", "sw_vers")
    try:
        version = subprocess.run(
            ["/usr/bin/sw_vers"], capture_output=True, text=True, env={}, check=False
        )
    except OSError as error:
        raise ProbeStop("sw_vers", "could not execute", code_kind="errno", code=error.errno)
    check("sw_vers", "process_exit", version.returncode)
    fields = dict(line.split(":", 1) for line in version.stdout.splitlines() if ":" in line)
    product_version = fields.get("ProductVersion", "").strip()
    build = fields.get("BuildVersion", "").strip()
    report("observed", "platform", arch=arch, version=product_version, build=build)
    if (product_version, build) != EXPECTED_OS:
        raise ProbeStop("platform", "requires macOS 15.8.2 build 24H232")

    report("running", "symbols")
    try:
        lib = ct.CDLL("/usr/lib/libSystem.B.dylib")
    except OSError as error:
        raise ProbeStop("symbols", "could not load libSystem", detail=str(error))

    def bind(symbol, *argument_types):
        try:
            function = getattr(lib, symbol)
        except AttributeError:
            raise ProbeStop("symbols", "missing libSystem symbol", symbol=symbol) from None
        function.argtypes = argument_types
        function.restype = ct.c_int
        return function

    port = ct.c_uint32
    port_ref = ct.POINTER(port)
    attr_ref = ct.POINTER(ct.c_void_p)
    strings = ct.POINTER(ct.c_char_p)
    get_special = bind("task_get_special_port", port, ct.c_int, port_ref)
    port_type = bind("mach_port_type", port, port, port_ref)
    allocate = bind("mach_port_allocate", port, port, port_ref)
    insert_right = bind("mach_port_insert_right", port, port, port, ct.c_uint32)
    destroy = bind("mach_port_destroy", port, port)
    deallocate = bind("mach_port_deallocate", port, port)
    make_subset = bind("bootstrap_subset", port, port, port_ref)
    attr_init = bind("posix_spawnattr_init", attr_ref)
    attr_destroy = bind("posix_spawnattr_destroy", attr_ref)
    attr_special = bind("posix_spawnattr_setspecialport_np", attr_ref, port, ct.c_int)
    spawn = bind("posix_spawn", ct.POINTER(ct.c_int), ct.c_char_p, attr_ref, attr_ref,
                 strings, strings)
    try:
        task = port.in_dll(lib, "mach_task_self_").value
    except ValueError:
        raise ProbeStop("symbols", "missing libSystem symbol", symbol="mach_task_self_") from None

    def require_live_send(phase, name):
        if name in INVALID_PORT_NAMES:
            raise ProbeStop(phase, "null or dead port name")
        rights = port()
        check(phase, "mach", port_type(task, name, ct.byref(rights)))
        if rights.value & MACH_PORT_TYPE_DEAD_NAME or not rights.value & MACH_PORT_TYPE_SEND:
            raise ProbeStop(phase, "missing live send right", port_type=rights.value)

    def release(resource, code_kind, function, *args):
        code = function(*args)
        if code:
            cleanup_errors.append(dict(resource=resource, code_kind=code_kind, code=code))

    with ExitStack() as rights:
        inherited = port()
        check("task_get_special_port", "mach",
              get_special(task, TASK_BOOTSTRAP_PORT, ct.byref(inherited)))
        if inherited.value not in INVALID_PORT_NAMES:
            rights.callback(release, "inherited_send", "mach", deallocate, task, inherited.value)
        require_live_send("inherited_bootstrap_port", inherited.value)

        requestor = port()
        check("mach_port_allocate", "mach",
              allocate(task, MACH_PORT_RIGHT_RECEIVE, ct.byref(requestor)))
        rights.callback(release, "requestor", "mach", destroy, task, requestor.value)
        check("mach_port_insert_right", "mach",
              insert_right(task, requestor.value, requestor.value, MACH_MSG_TYPE_MAKE_SEND))

        subset = port()
        report("running", "bootstrap_subset")
        check("bootstrap_subset", "bootstrap_mig",
              make_subset(inherited.value, requestor.value, ct.byref(subset)))
        report("observed", "bootstrap_subset", code_kind="bootstrap_mig", code=0)
        if subset.value not in INVALID_PORT_NAMES:
            rights.callback(release, "subset_send", "mach", deallocate, task, subset.value)
        # These names all belong to the worker's Mach IPC space.
        if subset.value in (inherited.value, requestor.value, task):
            raise ProbeStop("subset_bootstrap_port", "subset aliases an existing probe right")
        require_live_send("subset_bootstrap_port", subset.value)

        attr = ct.c_void_p()
        check("posix_spawnattr_init", "errno", attr_init(ct.byref(attr)))
        rights.callback(release, "spawn_attributes", "errno", attr_destroy, ct.byref(attr))
        check("posix_spawnattr_setspecialport_np", "errno",
              attr_special(ct.byref(attr), subset.value, TASK_BOOTSTRAP_PORT))

        binary = b"/usr/bin/true"
        argv = (ct.c_char_p * 2)(binary, None)
        envp = (ct.c_char_p * 1)(None)
        pid = ct.c_int()
        report("running", "posix_spawn")
        check("posix_spawn", "errno",
              spawn(ct.byref(pid), binary, None, ct.byref(attr), argv, envp))
        if pid.value <= 0:
            raise ProbeStop("posix_spawn", "spawn returned an invalid child PID")
        report("observed", "posix_spawn", code_kind="errno", code=0)
        report("running", "waitpid")
        try:
            _, wait_status = os.waitpid(pid.value, 0)
        except OSError as error:
            raise ProbeStop("waitpid", "could not reap fixed child", code_kind="errno",
                            code=error.errno)
        check("fixed_child", "process_exit", os.waitstatus_to_exitcode(wait_status))


def worker():
    cleanup_errors = []
    try:
        probe(cleanup_errors)
        if cleanup_errors:
            raise ProbeStop("cleanup", "could not release probe resources")
    except ProbeStop as error:
        report("stop", **error.record, cleanup_errors=cleanup_errors)
        return STOP
    report("pass", "complete", distinct_live_send_right=True, scoped_spawn_accepted=True,
           fixed_child_exit=0, job_authority_tested=False)
    return 0


def main():
    try:
        if len(sys.argv) != 1:
            raise ProbeStop("arguments", "takes no arguments")
        platform_gate()
        entry = "import runpy, sys; sys.exit(runpy.run_path(sys.argv[1])['worker']())"
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", "-S", "-c", entry, os.path.abspath(__file__)],
                env={}, stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as error:
            raise ProbeStop("worker_start", "could not execute worker", code_kind="errno",
                            code=error.errno)
        try:
            result = process.wait(timeout=DEADLINE_SECONDS)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            if isinstance(error, subprocess.TimeoutExpired):
                raise ProbeStop("worker_timeout", "worker deadline exceeded",
                                deadline_seconds=DEADLINE_SECONDS)
            raise ProbeStop("worker_interrupt", "probe interrupted")
        if result not in (0, STOP):
            raise ProbeStop("worker_exit", "unexpected worker exit",
                            code_kind="process_exit", code=result)
        return result
    except ProbeStop as error:
        report("stop", **error.record)
        return STOP


if __name__ == "__main__":
    raise SystemExit(main())
