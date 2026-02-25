"""Double-buffered context window management for seamless context transitions.

Implements a double-buffering technique for maintaining conversation continuity
across context window boundaries. Instead of stopping to summarize when the
context fills up, this session maintains two buffers and switches between them
with minimal disruption.

Algorithm:

1. **Checkpoint** at configurable threshold (default 70% capacity) -- summarize
   the current context and seed the back buffer with that summary.
2. **Concurrent** -- keep working in the active buffer while also appending
   every new message to the back buffer (``self._back_buffer is not None``).
3. **Swap** -- when the active buffer hits the swap threshold (default 95%),
   swap to the back buffer seamlessly (``self._back_buffer`` reset to ``None``).

The presence or absence of the back buffer (``None`` vs a list) replaces the
old enum-based phase tracking.

When ``max_generations`` is reached, the renewal policy controls what happens
(None means no limit -- renewal disabled by default):

* ``"recurse"`` -- meta-summarize all accumulated summaries into one.
* ``"dump"`` -- discard accumulated summaries and start fresh.

Usage::

    from agents import SQLiteSession
    from agents.extensions.memory import DoubleBufferSession

    underlying = SQLiteSession("my-session")
    session = DoubleBufferSession(
        session_id="my-session",
        underlying_session=underlying,
        max_context_items=100,
        summarizer=my_summarize_fn,
    )

    await Runner.run(agent, "Hello", session=session)
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING

from ...items import TResponseInputItem
from ...memory.session import SessionABC
from ...memory.session_settings import SessionSettings

if TYPE_CHECKING:
    from ...memory.session import Session

logger = logging.getLogger("openai-agents.double-buffer")


class RenewalPolicy(str, Enum):
    """Policy for handling accumulated summaries when max_generations is reached.

    Attributes:
        RECURSE: Meta-summarize all accumulated summaries into one and continue.
        DUMP: Discard all accumulated summaries and start fresh.
    """

    RECURSE = "recurse"
    DUMP = "dump"


# Type alias for the summarizer callable.
# It receives a list of items and returns a summary as a list of items.
Summarizer = Callable[
    [list[TResponseInputItem]],
    Awaitable[list[TResponseInputItem]],
]


class DoubleBufferSession(SessionABC):
    """Session decorator implementing double-buffered context window management.

    Wraps any existing ``Session`` and transparently manages context window
    transitions by maintaining an active buffer and a back buffer. When the
    active buffer crosses the checkpoint threshold, a summary is created and
    used to seed the back buffer. New messages are appended to both buffers
    concurrently. When the active buffer crosses the swap threshold, the back
    buffer becomes the new active buffer.

    Args:
        session_id: Unique identifier for this session.
        underlying_session: The session store that holds the active buffer.
        max_context_items: Maximum number of items before context is considered full.
        checkpoint_threshold: Fraction of ``max_context_items`` at which to create
            a checkpoint and seed the back buffer. Defaults to 0.70.
        swap_threshold: Fraction of ``max_context_items`` at which to swap to the
            back buffer. Defaults to 0.95.
        max_generations: Maximum number of summary generations to accumulate before
            applying the renewal policy. None means no limit (renewal disabled).
        renewal_policy: What to do when ``max_generations`` is reached. Defaults to
            ``RenewalPolicy.RECURSE``.
        summarizer: Async callable that takes a list of items and returns a
            summarized list of items. Required.
        session_settings: Optional session settings to pass through.
    """

    def __init__(
        self,
        session_id: str,
        underlying_session: Session,
        *,
        max_context_items: int = 100,
        checkpoint_threshold: float = 0.70,
        swap_threshold: float = 0.95,
        max_generations: int | None = None,
        renewal_policy: RenewalPolicy | str = RenewalPolicy.RECURSE,
        summarizer: Summarizer,
        session_settings: SessionSettings | None = None,
        checkpoint_timeout: float = 120.0,
    ):
        if not (0.0 < checkpoint_threshold < 1.0):
            raise ValueError(
                f"checkpoint_threshold must be between 0 and 1 exclusive, "
                f"got {checkpoint_threshold}"
            )
        if not (0.0 < swap_threshold <= 1.0):
            raise ValueError(
                f"swap_threshold must be between 0 (exclusive) and 1 (inclusive), "
                f"got {swap_threshold}"
            )
        if swap_threshold <= checkpoint_threshold:
            raise ValueError(
                f"swap_threshold ({swap_threshold}) must be greater than "
                f"checkpoint_threshold ({checkpoint_threshold})"
            )
        if max_context_items < 1:
            raise ValueError(f"max_context_items must be >= 1, got {max_context_items}")
        if max_generations is not None and max_generations < 1:
            raise ValueError(f"max_generations must be >= 1, got {max_generations}")

        self.session_id = session_id
        self.underlying_session = underlying_session
        self.session_settings = session_settings
        self.max_context_items = max_context_items
        self.checkpoint_threshold = checkpoint_threshold
        self.swap_threshold = swap_threshold
        self.max_generations = max_generations
        self.summarizer = summarizer
        self.checkpoint_timeout = checkpoint_timeout

        if isinstance(renewal_policy, str):
            self._renewal_policy = RenewalPolicy(renewal_policy)
        else:
            self._renewal_policy = renewal_policy

        # Internal state.
        # When ``None`` the session is in normal (single-buffer) mode.
        # When a list, it holds the back-buffer contents (concurrent mode).
        self._back_buffer: list[TResponseInputItem] | None = None
        self._summary_generations: list[list[TResponseInputItem]] = []
        self._generation_count: int = 0

    @property
    def has_back_buffer(self) -> bool:
        """``True`` when the back buffer is active (concurrent mode)."""
        return self._back_buffer is not None

    @property
    def generation_count(self) -> int:
        """Number of summary generations accumulated so far."""
        return self._generation_count

    @property
    def renewal_policy(self) -> RenewalPolicy:
        """The renewal policy for handling accumulated summaries."""
        return self._renewal_policy

    def _checkpoint_item_count(self) -> int:
        """Compute the item count that triggers a checkpoint."""
        return int(self.max_context_items * self.checkpoint_threshold)

    def _swap_item_count(self) -> int:
        """Compute the item count that triggers a swap."""
        return int(self.max_context_items * self.swap_threshold)

    async def _create_checkpoint(self, items: list[TResponseInputItem]) -> None:
        """Create a checkpoint summary and seed the back buffer.

        After this call ``self._back_buffer`` is a list (concurrent mode).
        """
        logger.debug(
            "checkpoint: summarizing %d items (generation %d)",
            len(items),
            self._generation_count + 1,
        )

        try:
            summary_items = await asyncio.wait_for(
                self.summarizer(items),
                timeout=self.checkpoint_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                "checkpoint: summarizer timed out after %.1fs (generation %d)",
                self.checkpoint_timeout,
                self._generation_count + 1,
            )
            raise

        self._generation_count += 1
        self._summary_generations.append(summary_items)

        # Seed the back buffer with the summary (activates concurrent mode).
        self._back_buffer = list(summary_items)

        logger.debug(
            "checkpoint: created summary with %d items, back buffer seeded, "
            "entering concurrent mode (generation %d)",
            len(summary_items),
            self._generation_count,
        )

    async def _perform_swap(self) -> None:
        """Swap the back buffer into the active position.

        Clears the underlying session and replaces it with the back buffer
        contents. Sets ``self._back_buffer`` to ``None`` (normal mode) so the
        cycle can restart.
        """
        back = self._back_buffer or []
        logger.debug(
            "swap: replacing active buffer with back buffer (%d items)",
            len(back),
        )

        await self.underlying_session.clear_session()
        if back:
            await self.underlying_session.add_items(back)

        self._back_buffer = None

        logger.debug("swap: complete, back in normal mode")

    async def _maybe_apply_renewal(self) -> None:
        """Apply the renewal policy if max_generations has been reached."""
        if self.max_generations is None or self._generation_count < self.max_generations:
            return

        logger.debug(
            "renewal: %d generations reached (max=%d), applying policy=%s",
            self._generation_count,
            self.max_generations,
            self._renewal_policy.value,
        )

        if self._renewal_policy == RenewalPolicy.DUMP:
            self._summary_generations = []
            self._generation_count = 0
            logger.debug("renewal: dumped all accumulated summaries")
        elif self._renewal_policy == RenewalPolicy.RECURSE:
            # Flatten all accumulated summaries and meta-summarize.
            all_summary_items: list[TResponseInputItem] = []
            for gen in self._summary_generations:
                all_summary_items.extend(gen)

            meta_summary = await self.summarizer(all_summary_items)
            self._summary_generations = [meta_summary]
            self._generation_count = 1
            logger.debug(
                "renewal: meta-summarized %d items across %d generations into %d items",
                len(all_summary_items),
                self.max_generations,
                len(meta_summary),
            )

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history from the active buffer.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.

        Returns:
            List of input items representing the conversation history.
        """
        return await self.underlying_session.get_items(limit)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation and manage buffer transitions.

        This method implements the core double-buffer logic:

        1. If in NORMAL phase and the active buffer crosses the checkpoint
           threshold, create a checkpoint and enter CONCURRENT phase.
        2. If in CONCURRENT phase, append items to both active and back buffers.
           If the active buffer crosses the swap threshold, perform a swap.
        3. After any swap, check if renewal is needed.

        Args:
            items: List of input items to add to the history.
        """
        if not items:
            return

        # Always add to the active (underlying) buffer.
        await self.underlying_session.add_items(items)

        current_items = await self.underlying_session.get_items()
        current_count = len(current_items)

        if self._back_buffer is None:
            # Normal mode -- no back buffer yet.
            if current_count >= self._swap_item_count():
                # Stop-the-world fallback: usage jumped past swap threshold
                # while still in normal mode (no checkpoint taken).  We MUST
                # checkpoint then swap inline -- NEVER skip compaction.
                logger.warning(
                    "stop-the-world: %d items >= swap threshold %d with no "
                    "checkpoint.  Performing inline checkpoint then swap.",
                    current_count,
                    self._swap_item_count(),
                )
                # current_items already includes the new items (they were
                # appended to the underlying session above), so the
                # checkpoint summary covers everything.  Do NOT re-append
                # ``items`` to the back buffer -- that would duplicate them.
                await self._create_checkpoint(current_items)
                await self._perform_swap()
                await self._maybe_apply_renewal()
            elif current_count >= self._checkpoint_item_count():
                await self._create_checkpoint(current_items)
                # After checkpoint, back buffer is active. No swap check yet
                # because we just entered concurrent -- the back buffer only
                # has the summary so far.

        else:
            # Concurrent mode -- back buffer is active.
            self._back_buffer.extend(copy.deepcopy(items))

            if current_count >= self._swap_item_count():
                await self._perform_swap()
                await self._maybe_apply_renewal()

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        If in CONCURRENT phase, also removes the last item from the back
        buffer to keep them in sync -- but only if the back buffer contains
        more than the summary seed (i.e., at least one concurrent item).
        This prevents accidentally deleting the summary that anchors the
        back buffer.

        Returns:
            The most recent item if it exists, None if the session is empty.
        """
        popped = await self.underlying_session.pop_item()
        if popped is not None and self._back_buffer is not None and len(self._back_buffer) > 1:
            self._back_buffer.pop()
        return popped

    async def clear_session(self) -> None:
        """Clear all items and reset the double-buffer state."""
        await self.underlying_session.clear_session()
        self._back_buffer = None
        self._summary_generations = []
        self._generation_count = 0
        logger.debug("clear: session and all buffers reset")
