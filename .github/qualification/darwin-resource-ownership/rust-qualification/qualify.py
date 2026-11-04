"""Qualify the exact Rust coalition owner using the existing launchd fixtures."""

import argparse
import contextlib
import os
from pathlib import Path
import platform
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid


def driver_line(driver):
    if not select.select([driver.stdout], [], [], 6)[0]:
        raise RuntimeError("Rust driver timed out")
    line = driver.stdout.readline()
    if not line:
        raise RuntimeError("Rust driver exited before reporting a result")
    result = line.decode("utf-8").strip()
    print(result, flush=True)
    return result


def driver_step(driver, step):
    driver.stdin.write(step)
    driver.stdin.flush()
    return driver_line(driver)


def run_case(fixture, driver_path, directory, domain, system_image):
    api = fixture.Darwin()
    label = f"org.openai.sdk.coalition-rust.{uuid.uuid4().hex}"
    with contextlib.ExitStack() as stack:
        listener = stack.enter_context(socket.socket(socket.AF_UNIX))
        listener.bind(str(directory / "sock"))
        listener.listen(3)
        listener.settimeout(4)
        helper_job = fixture.load_job(stack, directory, domain, label + ".target", "h")
        helper, helper_token = fixture.accept_peer(stack, listener, b"h", helper_job)
        control_job = fixture.load_job(stack, directory, domain, label + ".control", "c")
        control, control_token = fixture.accept_peer(stack, listener, b"c", control_job)
        helper_pid, control_pid = helper_token[5], control_token[5]
        target_id = api.member(helper_pid, helper_token[7])
        control_id = api.member(control_pid, control_token[7])
        fixture.check(target_id not in {control_id, api.member(os.getpid())},
                      "target and control must use distinct resource coalitions")

        with subprocess.Popen(
            [str(driver_path), str(helper.fileno())],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            pass_fds=(helper.fileno(),),
        ) as driver:
            try:
                fixture.check(driver_line(driver) == f"READY {helper_pid} {target_id} 1",
                              "Rust driver did not acquire the singleton helper")
                helper.sendall(b"f")
                child_pid = struct.unpack("=I", fixture.read_exact(helper, 4))[0]
                if system_image:
                    detached = None
                    deadline = time.monotonic() + 2
                    while api.image(child_pid) != fixture.SYSTEM_IMAGE:
                        fixture.check(time.monotonic() < deadline, "child did not exec /bin/sleep")
                        time.sleep(0.01)
                else:
                    tracked_parent = child_pid
                    detached, detached_token = fixture.accept_peer(stack, listener, b"d")
                    child_pid = detached_token[5]
                    helper.sendall(b"e")
                    fixture.check(fixture.read_exact(helper, 1) == b"r",
                                  "helper did not reap the tracked parent")
                    fixture.check(api.identity(tracked_parent, missing_ok=True) is None,
                                  "tracked parent is still alive")
                    started, exited = api.counters(target_id)
                    if started - exited > 2:
                        api.counters(target_id, expected=(started, started - 2))
                fixture.check(child_pid not in {helper_pid, control_pid, os.getpid(), driver.pid},
                              "child PID overlaps a known process")
                fixture.check(os.getsid(child_pid) == os.getpgid(child_pid) == child_pid,
                              "child did not detach into its own POSIX session")
                fixture.check(api.member(child_pid) == target_id,
                              "child left the resource coalition after detaching")
                fixture.check(driver_step(driver, b"t") == "TIMED_OUT 2",
                              "bounded drain did not report the remaining child")
                if detached is not None:
                    fixture.ping(detached)

                fixture.check(driver_step(driver, b"d") == "PARKED 1",
                              "Rust drain did not leave only the helper")
                if detached is None:
                    fixture.check(fixture.read_exact(helper, 1) == b"k",
                                  "waitpid did not report SIGKILL for the system image")
                else:
                    fixture.check(detached.recv(1) == b"", "detached child socket stayed open")
                fixture.wait_for_exit(api, child_pid)
                fixture.ping(helper)
                fixture.ping(control)

                # Close the fixture socket by half-closing it. The driver still
                # holds a duplicate descriptor for the same accepted socket.
                helper.shutdown(socket.SHUT_WR)
                fixture.check(helper.recv(1) == b"", "helper did not exit")
                fixture.wait_for_exit(api, helper_pid)
                fixture.check(driver_step(driver, b"z") == "FINAL 0",
                              "Rust driver did not read zero live target tasks")
                fixture.ping(control)
                fixture.check(api.member(control_pid, control_token[7]) == control_id,
                              "control changed resource coalition")
                fixture.check(driver.wait(timeout=2) == 0, "Rust driver failed")
            finally:
                if driver.poll() is None:
                    driver.terminate()
                    try:
                        driver.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        driver.kill()
                        driver.wait(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("driver", type=Path)
    parser.add_argument("domain", choices=("gui", "user"))
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--system-image", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.fixture.resolve()))
    import smoke as fixture

    fixture.check(platform.system() == "Darwin" and platform.machine() == "arm64",
                  "run on native Apple Silicon macOS")
    fixture.check(os.getuid() == os.geteuid() != 0, "run as an ordinary user")
    signal.signal(signal.SIGTERM, fixture.interrupt)
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="sdk-rust-coalition-", dir="/tmp") as tmp:
        directory = Path(tmp)
        subprocess.run(
            ["/usr/bin/clang", "-std=c11", "-D_DARWIN_C_SOURCE",
             "-Wall", "-Wextra", "-Werror", str(args.fixture.resolve() / "helper.c"),
             "-o", str(directory / "helper")],
            check=True, timeout=30,
        )
        run_case(fixture, args.driver.resolve(), directory, f"{args.domain}/{os.getuid()}",
                 args.system_image)
    print("PASS: Rust coalition ownership")


if __name__ == "__main__":
    main()
