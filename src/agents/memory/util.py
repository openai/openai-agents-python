from __future__ import annotations

from typing import Callable, Union

from ..items import TResponseInputItem
from ..util._types import MaybeAwaitable

SessionMixerCallable = Callable[
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


SessionInputHandler = Union[SessionMixerCallable, None]
"""Defines how to handle session history when new input is provided.

- `None` (default): The new input is appended to the session history.
- `SessionMixerCallable`: A custom function that receives the history and new input, and
   returns the desired combined list of items.
"""
