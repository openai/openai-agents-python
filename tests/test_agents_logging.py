from __future__ import annotations

import io
import logging
import sys
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

import agents
from agents import enable_verbose_stdout_logging


@pytest.fixture
def agents_logger(monkeypatch: pytest.MonkeyPatch) -> Generator[logging.Logger, None, None]:
    logger = logging.getLogger("openai.agents")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    logger.handlers.clear()
    monkeypatch.setattr(agents, "_verbose_stdout_handler", None)

    try:
        yield logger
    finally:
        added_handlers = [
            handler for handler in logger.handlers if handler not in original_handlers
        ]
        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)
        for handler in added_handlers:
            handler.close()


def test_enable_verbose_stdout_logging_reuses_its_handler(
    agents_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)

    enable_verbose_stdout_logging()
    handler = agents_logger.handlers[0]
    enable_verbose_stdout_logging()
    agents_logger.debug("debug message")

    assert agents_logger.handlers == [handler]
    assert stdout.getvalue() == "debug message\n"


def test_enable_verbose_stdout_logging_preserves_application_handler(
    agents_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    application_handler = logging.StreamHandler(stdout)
    application_handler.setLevel(logging.WARNING)
    agents_logger.addHandler(application_handler)

    enable_verbose_stdout_logging()
    agents_logger.debug("debug message")

    assert agents_logger.handlers[0] is application_handler
    assert application_handler.level == logging.WARNING
    assert len(agents_logger.handlers) == 2
    assert stdout.getvalue() == "debug message\n"


def test_enable_verbose_stdout_logging_follows_replaced_stdout(
    agents_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", first_stdout)
    enable_verbose_stdout_logging()
    handler = agents_logger.handlers[0]
    agents_logger.debug("first message")
    assert first_stdout.getvalue() == "first message\n"
    first_stdout.close()

    second_stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", second_stdout)
    enable_verbose_stdout_logging()
    agents_logger.debug("second message")

    assert agents_logger.handlers == [handler]
    assert second_stdout.getvalue() == "second message\n"


def test_enable_verbose_stdout_logging_serializes_handler_initialization(
    agents_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    original_stream_handler = logging.StreamHandler
    original_add_handler = logging.Logger.addHandler
    constructor_count = 0
    constructor_count_lock = threading.Lock()
    second_constructor_started = threading.Event()
    first_handler_attached = threading.Event()
    start_barrier = threading.Barrier(3)

    def coordinated_stream_handler(stream: Any = None) -> logging.StreamHandler[Any]:
        nonlocal constructor_count
        with constructor_count_lock:
            constructor_count += 1
            current_constructor = constructor_count

        handler = original_stream_handler(stream)
        if current_constructor == 1:
            second_constructor_started.wait(timeout=0.2)
        else:
            second_constructor_started.set()
            first_handler_attached.wait(timeout=0.2)
        return handler

    def tracking_add_handler(
        logger: logging.Logger,
        handler: logging.Handler,
    ) -> None:
        original_add_handler(logger, handler)
        if logger is agents_logger:
            first_handler_attached.set()

    def enable_logging() -> None:
        start_barrier.wait(timeout=1)
        enable_verbose_stdout_logging()

    monkeypatch.setattr(logging, "StreamHandler", coordinated_stream_handler)
    monkeypatch.setattr(logging.Logger, "addHandler", tracking_add_handler)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(enable_logging) for _ in range(2)]
        start_barrier.wait(timeout=1)
        for future in futures:
            future.result(timeout=1)

    monkeypatch.setattr(logging, "StreamHandler", original_stream_handler)
    monkeypatch.setattr(logging.Logger, "addHandler", original_add_handler)
    agents_logger.debug("debug message")

    assert constructor_count == 1
    assert len(agents_logger.handlers) == 1
    assert stdout.getvalue() == "debug message\n"


def test_enable_verbose_stdout_logging_falls_back_to_stderr(
    agents_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    enable_verbose_stdout_logging()
    handler = agents_logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)

    monkeypatch.setattr(sys, "stdout", None)
    enable_verbose_stdout_logging()
    agents_logger.debug("debug message")

    assert agents_logger.handlers == [handler]
    assert handler.stream is stderr
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == "debug message\n"
