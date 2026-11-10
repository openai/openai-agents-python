"""The same timeout assertion on every supported Python version."""

import sys
from asyncio import CancelledError

if sys.version_info >= (3, 11):
    from asyncio import timeout as timeout
else:
    from async_timeout import timeout as timeout


def original_cancellation(error: CancelledError) -> CancelledError:
    """Python 3.10 task awaits have been wrapping the original cancellation."""
    if sys.version_info < (3, 11) and not error.args:
        original = error.__context__
        if isinstance(original, CancelledError):
            return original
    return error
