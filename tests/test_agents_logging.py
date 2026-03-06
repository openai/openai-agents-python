from __future__ import annotations

import logging

from agents import enable_verbose_stdout_logging


def test_enable_verbose_stdout_logging_attaches_handler() -> None:
    logger = logging.getLogger("openai.agents")
    logger.handlers.clear()
    enable_verbose_stdout_logging()
    assert logger.handlers
    logger.handlers.clear()
