import logging
from collections.abc import Callable, Mapping

from . import _debug

logger = logging.getLogger("openai.agents")

_DiagnosticExtra = Callable[[], Mapping[str, object]]


def _log_action_error(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    redact: bool,
    stacklevel: int,
    diagnostic_extra: _DiagnosticExtra | None,
) -> None:
    """Log an action failure without inspecting a redacted exception."""
    if redact:
        target_logger.error("%s", message, stacklevel=stacklevel)
    else:
        target_logger.error(
            "%s: %s",
            message,
            exc,
            exc_info=exc,
            extra=diagnostic_extra() if diagnostic_extra is not None else None,
            stacklevel=stacklevel,
        )


def _log_action_at_level(
    log_method: Callable[..., None],
    message: str,
    exc: BaseException,
    *,
    redact: bool,
    stacklevel: int,
    diagnostic_extra: _DiagnosticExtra | None,
) -> None:
    """Log an action failure at a caller-selected level."""
    if redact:
        log_method("%s", message, stacklevel=stacklevel)
    else:
        log_method(
            "%s: %s",
            message,
            exc,
            exc_info=exc,
            extra=diagnostic_extra() if diagnostic_extra is not None else None,
            stacklevel=stacklevel,
        )


def log_model_action_error(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Log a model-data failure according to the model logging policy."""
    _log_action_error(
        target_logger,
        message,
        exc,
        redact=_debug.DONT_LOG_MODEL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )


def log_model_action_debug(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Debug-log a model-data failure according to the model logging policy."""
    _log_action_at_level(
        target_logger.debug,
        message,
        exc,
        redact=_debug.DONT_LOG_MODEL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )


def log_model_action_warning(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Warning-log a model-data failure according to the model logging policy."""
    _log_action_at_level(
        target_logger.warning,
        message,
        exc,
        redact=_debug.DONT_LOG_MODEL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )


def log_tool_action_error(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Log a tool-data failure according to the tool logging policy."""
    _log_action_error(
        target_logger,
        message,
        exc,
        redact=_debug.DONT_LOG_TOOL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )


def log_tool_action_debug(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Debug-log a tool-data failure according to the tool logging policy."""
    _log_action_at_level(
        target_logger.debug,
        message,
        exc,
        redact=_debug.DONT_LOG_TOOL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )


def log_tool_action_warning(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Warning-log a tool-data failure according to the tool logging policy."""
    _log_action_at_level(
        target_logger.warning,
        message,
        exc,
        redact=_debug.DONT_LOG_TOOL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )


def log_model_and_tool_action_error(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Log a mixed model/tool-data failure only when both data policies allow it."""
    _log_action_error(
        target_logger,
        message,
        exc,
        redact=_debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )


def log_model_and_tool_action_debug(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Debug-log a mixed-data failure only when both data policies allow it."""
    _log_action_at_level(
        target_logger.debug,
        message,
        exc,
        redact=_debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )


def log_model_and_tool_action_warning(
    target_logger: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    stacklevel: int = 3,
    diagnostic_extra: _DiagnosticExtra | None = None,
) -> None:
    """Warning-log a mixed-data failure only when both data policies allow it."""
    _log_action_at_level(
        target_logger.warning,
        message,
        exc,
        redact=_debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA,
        stacklevel=stacklevel,
        diagnostic_extra=diagnostic_extra,
    )
