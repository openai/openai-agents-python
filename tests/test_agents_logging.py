from __future__ import annotations

import io
import logging
import sys
from collections.abc import Generator

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
