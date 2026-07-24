import logging
from collections.abc import Callable, Mapping
from types import TracebackType

from . import _debug

logger = logging.getLogger("openai.agents")

_DiagnosticExtra = Callable[[], Mapping[str, object]]
_DIAGNOSTIC_CONTEXT_FIELD = "openai_agents_diagnostic_context"


def _exception_info(
    exc: BaseException,
) -> tuple[type[BaseException], BaseException, TracebackType | None]:
    """Build logging exception info without evaluating exception truthiness."""
    traceback = BaseException.__getattribute__(exc, "__traceback__")
    return type(exc), exc, traceback


def _log_record_extra(diagnostic_extra: _DiagnosticExtra | None) -> dict[str, object] | None:
    if diagnostic_extra is None:
        return None
    try:
        return {_DIAGNOSTIC_CONTEXT_FIELD: dict(diagnostic_extra())}
    except Exception:
        return None


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
            exc_info=_exception_info(exc),
            extra=_log_record_extra(diagnostic_extra),
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
            exc_info=_exception_info(exc),
            extra=_log_record_extra(diagnostic_extra),
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
