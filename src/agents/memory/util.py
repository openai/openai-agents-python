from __future__ import annotations

from typing import Callable

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
