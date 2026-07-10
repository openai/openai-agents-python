import logging
from typing import Any

logger = logging.getLogger("openai.agents")


def debug_with_context(message: str, **context: Any) -> None:
    """Log a debug message with structured context data.

    Args:
        message: The log message.
        **context: Structured context key-value pairs to include in the log.
    """
    if context:
        logger.debug(f"{message} | {context}")
    else:
        logger.debug(message)


def info_with_context(message: str, **context: Any) -> None:
    """Log an info message with structured context data.

    Args:
        message: The log message.
        **context: Structured context key-value pairs to include in the log.
    """
    if context:
        logger.info(f"{message} | {context}")
    else:
        logger.info(message)


def warning_with_context(message: str, **context: Any) -> None:
    """Log a warning message with structured context data.

    Args:
        message: The log message.
        **context: Structured context key-value pairs to include in the log.
    """
    if context:
        logger.warning(f"{message} | {context}")
    else:
        logger.warning(message)


def error_with_context(message: str, **context: Any) -> None:
    """Log an error message with structured context data.

    Args:
        message: The log message.
        **context: Structured context key-value pairs to include in the log.
    """
    if context:
        logger.error(f"{message} | {context}")
    else:
        logger.error(message)
