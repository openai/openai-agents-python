from __future__ import annotations

import logging

from agents.function_schema import _suppress_griffe_logging


def test_suppress_griffe_logging_restores_configured_notset_level() -> None:
    logger = logging.getLogger("griffe")
    root_logger = logging.getLogger()
    previous_logger_level = logger.level
    previous_root_level = root_logger.level

    try:
        logger.setLevel(logging.NOTSET)
        root_logger.setLevel(logging.WARNING)
        assert logger.getEffectiveLevel() == logging.WARNING

        with _suppress_griffe_logging():
            assert logger.level == logging.ERROR

        assert logger.level == logging.NOTSET
        assert logger.getEffectiveLevel() == logging.WARNING
    finally:
        logger.setLevel(previous_logger_level)
        root_logger.setLevel(previous_root_level)
