from __future__ import annotations

import re
from collections.abc import Callable

from ..items import TResponseInputItem
from ..util._types import MaybeAwaitable

SessionInputCallback = Callable[
    [list[TResponseInputItem], list[TResponseInputItem]],
    MaybeAwaitable[list[TResponseInputItem]],
]
"""A function that combines session history with new input items.

Args:
    history_items: The list of items from the session history.
    new_items: The list of new input items for the current turn.

Returns:
    A list of combined items to be used as input for the agent. Can be sync or async.
"""


# SQLite identifiers cannot be parameterised, so any caller-supplied table name
# is interpolated directly into SQL. Restrict accepted names to a conservative
# identifier pattern to prevent SQL injection (CWE-89) when a downstream
# application allows table names to be influenced by configuration or user input.
_SAFE_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_sql_identifier(name: str, *, kind: str = "identifier") -> str:
    """Validate that ``name`` is a safe SQL identifier.

    Only ASCII letters, digits and underscores are permitted, and the name must
    not start with a digit. Raises ``ValueError`` otherwise. Returns ``name``
    unchanged when valid so it can be used inline.
    """
    if not isinstance(name, str) or not _SAFE_SQL_IDENTIFIER.match(name):
        raise ValueError(
            f"Invalid SQL {kind} {name!r}: must match [A-Za-z_][A-Za-z0-9_]*"
        )
    return name
