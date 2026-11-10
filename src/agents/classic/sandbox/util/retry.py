"""Compatibility aliases for the shared implementation."""

import sys as _sys

from _agents_shared import retry as _implementation
from _agents_shared.retry import (
    DEFAULT_TRANSIENT_RETRY_BACKOFF as DEFAULT_TRANSIENT_RETRY_BACKOFF,
    DEFAULT_TRANSIENT_RETRY_INTERVAL_S as DEFAULT_TRANSIENT_RETRY_INTERVAL_S,
    DEFAULT_TRANSIENT_RETRY_MAX_ATTEMPT as DEFAULT_TRANSIENT_RETRY_MAX_ATTEMPT,
    TRANSIENT_HTTP_STATUS_CODES as TRANSIENT_HTTP_STATUS_CODES,
    BackoffStrategy as BackoffStrategy,
    P as P,
    T as T,
    exception_chain_contains_type as exception_chain_contains_type,
    exception_chain_has_status_code as exception_chain_has_status_code,
    iter_exception_chain as iter_exception_chain,
    retry_async as retry_async,
)

_sys.modules[__name__] = _implementation
