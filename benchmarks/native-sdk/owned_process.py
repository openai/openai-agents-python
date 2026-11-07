"""Owned POSIX process groups with deferred TERM/INT during bounded teardown."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _note(error, text):
    if hasattr(error, "add_note"):
        error.add_note(text)
    else:
        error.cleanup_notes = (*getattr(error, "cleanup_notes", ()), text)


@contextmanager
def defer_cleanup_signals():
    """Preserve active errors; otherwise replay interrupts only after cleanup."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("owned process cleanup requires the main thread")
    active_error = sys.exc_info()[1]
    deferred = []
    previous = {number: signal.getsignal(number) for number in SIGNALS}
    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)

    def collect(number, _frame):
        deferred.append(number)

    try:
        for number, handler in previous.items():
            if handler != signal.SIG_IGN:
                signal.signal(number, collect)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
    cleanup_error = None
    try:
        yield deferred
    except BaseException as error:
        cleanup_error = error
        raise
    finally:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        try:
            # Consume signals pending at restoration that were not intentionally
            # blocked by the caller. Keep the caller's SIG_IGN/mask semantics.
            for number in signal.sigpending().intersection(SIGNALS):
                if number not in old_mask and previous[number] != signal.SIG_IGN:
                    signal.sigwait({number})
                    deferred.append(number)
            for number, handler in previous.items():
                signal.signal(number, handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if deferred:
            target = active_error if active_error is not None else cleanup_error
            if target is not None:
                target.deferred_cleanup_signals = tuple(deferred)
                _note(
                    target,
                    f"Deferred cleanup signals: {[signal.Signals(n).name for n in deferred]}",
                )
            else:
                # One raised interrupt represents all arrivals; if a custom
                # handler returns normally, continue replaying the remainder.
                for number in deferred:
                    handler = previous[number]
                    try:
                        if handler == signal.SIG_DFL:
                            signal.raise_signal(number)
                        elif handler != signal.SIG_IGN:
                            handler(number, None)
                    except BaseException as error:
                        error.deferred_cleanup_signals = tuple(deferred)
                        _note(
                            error,
                            f"Deferred cleanup signals: "
                            f"{[signal.Signals(n).name for n in deferred]}",
                        )
                        raise


def _terminate(process, *, grouped, term_timeout=2, kill_timeout=5):
    """TERM/KILL/reap the owned group or fixed peer, even after leader exit."""
    active_error = sys.exc_info()[1]
    try:
        with defer_cleanup_signals():
            try:
                try:
                    if grouped:
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=term_timeout)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                try:
                    if grouped:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                finally:
                    process.wait(timeout=kill_timeout)
    except BaseException as error:
        if active_error is not None and error is not active_error:
            _note(active_error, f"Owned-group cleanup failure: {type(error).__name__}: {error}")
            return  # The caller's already-active exception continues unchanged.
        raise


def terminate_group(process, *, term_timeout=2, kill_timeout=5):
    """Retire a dedicated group, even when its leader has already exited."""
    return _terminate(process, grouped=True, term_timeout=term_timeout, kill_timeout=kill_timeout)


def terminate_child(process, *, term_timeout=2, kill_timeout=5):
    """Retire the fixed single-process peer that inherits its worker's group."""
    return _terminate(process, grouped=False, term_timeout=term_timeout, kill_timeout=kill_timeout)


def read_ready_line(process, *, timeout=10, max_bytes=65536):
    """Read one bounded UTF-8 ready line without an unbounded readline."""
    if timeout <= 0 or max_bytes <= 0 or process.stdout is None:
        raise ValueError("Positive readiness bounds and a stdout pipe are required")
    deadline = time.monotonic() + timeout
    pending = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError(f"Peer did not publish readiness within {timeout}s")
            block = os.read(process.stdout.fileno(), min(4096, max_bytes - len(pending) + 1))
            if not block:
                raise RuntimeError("Peer closed stdout before publishing readiness")
            pending.extend(block)
            if len(pending) > max_bytes:
                raise RuntimeError("Peer ready line exceeded its byte bound")
            if b"\n" in pending:
                return bytes(pending).split(b"\n", 1)[0].decode("utf-8")
