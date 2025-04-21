from typing import Any

from ..logger import logger
from ..tracing import Span, SpanError, get_current_span


def attach_error_to_span(span: Span[Any], error: SpanError) -> None:
    span.set_error(error)


def attach_error_to_current_span(error: SpanError) -> None:
    span = get_current_span()
    if span:
        attach_error_to_span(span, error)
    else:
        logger.warning(f"No span to add error {error} to")
