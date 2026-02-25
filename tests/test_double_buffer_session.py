"""Tests for DoubleBufferSession -- double-buffered context window management.

Reference: https://marklubin.me/posts/hopping-context-windows/
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from agents import SQLiteSession, TResponseInputItem
from agents.extensions.memory.double_buffer_session import (
    DoubleBufferSession,
    RenewalPolicy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(role: str, content: str) -> TResponseInputItem:
    """Create a simple message item dict."""
    return cast(TResponseInputItem, {"role": role, "content": content})


def _content(item: TResponseInputItem) -> Any:
    """Extract the ``content`` field from any input item.

    ``TResponseInputItem`` is a large TypedDict union and most variants don't
    expose ``content``, but in tests we only ever create simple message dicts
    that do have it.  This helper avoids ``typeddict-item`` errors everywhere.
    """
    return cast(dict[str, Any], item)["content"]


def _back(session: DoubleBufferSession) -> list[TResponseInputItem]:
    """Return the back buffer, asserting it is not ``None``."""
    bb = session._back_buffer
    assert bb is not None, "expected back buffer to exist"
    return bb


def _user(content: str) -> TResponseInputItem:
    return _make_item("user", content)


def _assistant(content: str) -> TResponseInputItem:
    return _make_item("assistant", content)


async def _trivial_summarizer(
    items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    """A test summarizer that returns a single assistant message summarizing the count."""
    return [_assistant(f"Summary of {len(items)} items")]


async def _identity_summarizer(
    items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    """A test summarizer that returns items unchanged (useful for verifying plumbing)."""
    return list(items)


async def _counting_summarizer_factory():
    """Factory that returns a summarizer and a counter list to inspect calls."""
    calls: list[list[TResponseInputItem]] = []

    async def summarizer(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
        calls.append(list(items))
        return [_assistant(f"Summary #{len(calls)} of {len(items)} items")]

    return summarizer, calls


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


class TestConstruction:
    """Tests for constructor parameter validation."""

    @pytest.mark.asyncio
    async def test_valid_construction(self) -> None:
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=100,
            summarizer=_trivial_summarizer,
        )
        assert session.session_id == "test"
        assert not session.has_back_buffer
        assert session.generation_count == 0
        assert session.max_context_items == 100
        assert session.checkpoint_threshold == 0.70
        assert session.swap_threshold == 0.95
        assert session.max_generations is None
        assert session.renewal_policy == RenewalPolicy.RECURSE
        underlying.close()

    @pytest.mark.asyncio
    async def test_invalid_checkpoint_threshold_zero(self) -> None:
        underlying = SQLiteSession("test")
        with pytest.raises(ValueError, match="checkpoint_threshold"):
            DoubleBufferSession(
                session_id="test",
                underlying_session=underlying,
                checkpoint_threshold=0.0,
                summarizer=_trivial_summarizer,
            )
        underlying.close()

    @pytest.mark.asyncio
    async def test_invalid_checkpoint_threshold_one(self) -> None:
        underlying = SQLiteSession("test")
        with pytest.raises(ValueError, match="checkpoint_threshold"):
            DoubleBufferSession(
                session_id="test",
                underlying_session=underlying,
                checkpoint_threshold=1.0,
                summarizer=_trivial_summarizer,
            )
        underlying.close()

    @pytest.mark.asyncio
    async def test_invalid_swap_threshold_zero(self) -> None:
        underlying = SQLiteSession("test")
        with pytest.raises(ValueError, match="swap_threshold"):
            DoubleBufferSession(
                session_id="test",
                underlying_session=underlying,
                swap_threshold=0.0,
                summarizer=_trivial_summarizer,
            )
        underlying.close()

    @pytest.mark.asyncio
    async def test_swap_must_exceed_checkpoint(self) -> None:
        underlying = SQLiteSession("test")
        with pytest.raises(ValueError, match="swap_threshold.*must be greater"):
            DoubleBufferSession(
                session_id="test",
                underlying_session=underlying,
                checkpoint_threshold=0.8,
                swap_threshold=0.7,
                summarizer=_trivial_summarizer,
            )
        underlying.close()

    @pytest.mark.asyncio
    async def test_swap_equal_to_checkpoint_rejected(self) -> None:
        underlying = SQLiteSession("test")
        with pytest.raises(ValueError, match="swap_threshold.*must be greater"):
            DoubleBufferSession(
                session_id="test",
                underlying_session=underlying,
                checkpoint_threshold=0.8,
                swap_threshold=0.8,
                summarizer=_trivial_summarizer,
            )
        underlying.close()

    @pytest.mark.asyncio
    async def test_invalid_max_context_items(self) -> None:
        underlying = SQLiteSession("test")
        with pytest.raises(ValueError, match="max_context_items"):
            DoubleBufferSession(
                session_id="test",
                underlying_session=underlying,
                max_context_items=0,
                summarizer=_trivial_summarizer,
            )
        underlying.close()

    @pytest.mark.asyncio
    async def test_invalid_max_generations(self) -> None:
        underlying = SQLiteSession("test")
        with pytest.raises(ValueError, match="max_generations"):
            DoubleBufferSession(
                session_id="test",
                underlying_session=underlying,
                max_generations=0,
                summarizer=_trivial_summarizer,
            )
        underlying.close()

    @pytest.mark.asyncio
    async def test_renewal_policy_from_string(self) -> None:
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            renewal_policy="dump",
            summarizer=_trivial_summarizer,
        )
        assert session.renewal_policy == RenewalPolicy.DUMP
        underlying.close()


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------


class TestPhaseTransitions:
    """Tests for the NORMAL -> CONCURRENT -> SWAP lifecycle."""

    @pytest.mark.asyncio
    async def test_stays_normal_below_checkpoint(self) -> None:
        """Adding items below the checkpoint threshold keeps phase NORMAL."""
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.7,  # checkpoint at 7 items
            swap_threshold=0.9,  # swap at 9 items
            summarizer=_trivial_summarizer,
        )

        # Add 6 items (below checkpoint of 7).
        for i in range(6):
            await session.add_items([_user(f"msg {i}")])

        assert not session.has_back_buffer
        items = await session.get_items()
        assert len(items) == 6
        underlying.close()

    @pytest.mark.asyncio
    async def test_checkpoint_triggers_concurrent(self) -> None:
        """Crossing the checkpoint threshold transitions to CONCURRENT."""
        underlying = SQLiteSession("test")
        summarizer, calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.7,
            swap_threshold=0.9,
            summarizer=summarizer,
        )

        # Add 7 items to trigger checkpoint (70% of 10).
        for i in range(7):
            await session.add_items([_user(f"msg {i}")])

        assert session.has_back_buffer
        assert len(calls) == 1
        # The summarizer should have been called with all 7 items.
        assert len(calls[0]) == 7
        assert session.generation_count == 1
        underlying.close()

    @pytest.mark.asyncio
    async def test_concurrent_appends_to_both_buffers(self) -> None:
        """In CONCURRENT phase, new items go to both active and back buffer."""
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.7,
            swap_threshold=0.9,
            summarizer=summarizer,
        )

        # Trigger checkpoint.
        for i in range(7):
            await session.add_items([_user(f"msg {i}")])
        assert session.has_back_buffer

        # Add one more item in concurrent phase.
        await session.add_items([_user("concurrent msg")])

        # Active buffer should have 8 items.
        active_items = await session.get_items()
        assert len(active_items) == 8

        # Back buffer should have summary + 1 new item.
        # Summary is 1 item, plus 1 concurrent message = 2.
        bb = _back(session)
        assert len(bb) == 2
        assert _content(bb[-1]) == "concurrent msg"
        underlying.close()

    @pytest.mark.asyncio
    async def test_swap_replaces_active_with_back_buffer(self) -> None:
        """Crossing the swap threshold swaps buffers and returns to NORMAL."""
        underlying = SQLiteSession("test")
        summarizer, calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.7,
            swap_threshold=0.9,
            summarizer=summarizer,
        )

        # Trigger checkpoint at 7.
        for i in range(7):
            await session.add_items([_user(f"msg {i}")])
        assert session.has_back_buffer

        # Add 2 more items to reach 9 (90% = swap threshold).
        await session.add_items([_user("msg 7")])
        await session.add_items([_user("msg 8")])

        # Should have swapped.
        assert not session.has_back_buffer

        # Active buffer should now contain the back buffer contents:
        # 1 summary item + 2 concurrent items = 3 items.
        active_items = await session.get_items()
        assert len(active_items) == 3

        # First item should be the summary.
        assert "Summary" in _content(active_items[0])
        # Last two should be the concurrent messages.
        assert _content(active_items[1]) == "msg 7"
        assert _content(active_items[2]) == "msg 8"

        # Back buffer should be empty after swap.
        assert session._back_buffer is None
        underlying.close()

    @pytest.mark.asyncio
    async def test_full_lifecycle_multiple_swaps(self) -> None:
        """Verify a complete cycle: NORMAL -> CONCURRENT -> SWAP -> NORMAL -> repeat."""
        underlying = SQLiteSession("test")
        summarizer, calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,  # checkpoint at 5
            swap_threshold=0.8,  # swap at 8
            summarizer=summarizer,
        )

        # --- First cycle ---
        # Fill to checkpoint.
        for i in range(5):
            await session.add_items([_user(f"cycle1-{i}")])
        assert session.has_back_buffer
        assert len(calls) == 1

        # Fill to swap.
        for i in range(3):
            await session.add_items([_user(f"cycle1-extra-{i}")])
        assert not session.has_back_buffer

        # After swap, active buffer = 1 summary + 3 concurrent = 4 items.
        items_after_first_swap = await session.get_items()
        assert len(items_after_first_swap) == 4

        # --- Second cycle ---
        # Add one more to hit checkpoint again (4 + 1 = 5).
        await session.add_items([_user("cycle2-trigger")])
        assert session.has_back_buffer
        assert len(calls) == 2  # second summarization

        underlying.close()


# ---------------------------------------------------------------------------
# Summary accumulation and renewal
# ---------------------------------------------------------------------------


class TestRenewal:
    """Tests for incremental summary accumulation and renewal policies."""

    @pytest.mark.asyncio
    async def test_generation_count_increments(self) -> None:
        """Each checkpoint increments the generation count."""
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.8,
            max_generations=5,
            summarizer=summarizer,
        )

        # First cycle.
        for i in range(5):
            await session.add_items([_user(f"g1-{i}")])
        assert session.generation_count == 1

        # Complete swap.
        for i in range(3):
            await session.add_items([_user(f"g1-swap-{i}")])
        assert not session.has_back_buffer

        # Second cycle checkpoint.
        for i in range(1):
            await session.add_items([_user(f"g2-{i}")])
        assert session.generation_count == 2
        underlying.close()

    @pytest.mark.asyncio
    async def test_recurse_renewal_policy(self) -> None:
        """When max_generations is reached with RECURSE, summaries are meta-summarized."""
        underlying = SQLiteSession("test")
        summarizer, calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.8,
            max_generations=2,
            renewal_policy=RenewalPolicy.RECURSE,
            summarizer=summarizer,
        )

        # First cycle: checkpoint + swap.
        for i in range(5):
            await session.add_items([_user(f"g1-{i}")])
        assert session.generation_count == 1
        for i in range(3):
            await session.add_items([_user(f"g1-swap-{i}")])
        assert not session.has_back_buffer

        # Second cycle: checkpoint triggers gen count = 2 = max_generations.
        # The swap should trigger renewal.
        # After first swap we have 4 items. Add 1 to trigger checkpoint at 5.
        await session.add_items([_user("g2-trigger")])
        assert session.generation_count == 2
        assert session.has_back_buffer

        # Swap to trigger renewal.
        for i in range(3):
            await session.add_items([_user(f"g2-swap-{i}")])

        # After renewal with RECURSE, generation_count resets to 1.
        assert session.generation_count == 1
        assert not session.has_back_buffer
        assert len(session._summary_generations) == 1

        # The last summarizer call should be the meta-summary call.
        # calls[-1] should contain the 2 previous summaries.
        meta_call_items = calls[-1]
        # Each summary generation is 1 item, so meta-summary input = 2 items.
        assert len(meta_call_items) == 2
        underlying.close()

    @pytest.mark.asyncio
    async def test_dump_renewal_policy(self) -> None:
        """When max_generations is reached with DUMP, summaries are discarded."""
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.8,
            max_generations=2,
            renewal_policy=RenewalPolicy.DUMP,
            summarizer=summarizer,
        )

        # First cycle.
        for i in range(5):
            await session.add_items([_user(f"g1-{i}")])
        for i in range(3):
            await session.add_items([_user(f"g1-swap-{i}")])

        # Second cycle.
        await session.add_items([_user("g2-trigger")])
        assert session.generation_count == 2

        for i in range(3):
            await session.add_items([_user(f"g2-swap-{i}")])

        # After renewal with DUMP, everything is cleared.
        assert session.generation_count == 0
        assert session._summary_generations == []
        assert not session.has_back_buffer
        underlying.close()


# ---------------------------------------------------------------------------
# Session protocol methods
# ---------------------------------------------------------------------------


class TestSessionProtocol:
    """Tests for standard Session protocol methods (get_items, pop_item, clear)."""

    @pytest.mark.asyncio
    async def test_get_items_delegates_to_underlying(self) -> None:
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            summarizer=_trivial_summarizer,
        )

        await session.add_items([_user("hello"), _assistant("hi")])
        items = await session.get_items()
        assert len(items) == 2
        assert _content(items[0]) == "hello"
        assert _content(items[1]) == "hi"
        underlying.close()

    @pytest.mark.asyncio
    async def test_get_items_with_limit(self) -> None:
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            summarizer=_trivial_summarizer,
        )

        for i in range(5):
            await session.add_items([_user(f"msg {i}")])

        items = await session.get_items(limit=2)
        assert len(items) == 2
        assert _content(items[0]) == "msg 3"
        assert _content(items[1]) == "msg 4"
        underlying.close()

    @pytest.mark.asyncio
    async def test_pop_item_in_normal_phase(self) -> None:
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=100,  # large enough to stay NORMAL
            summarizer=_trivial_summarizer,
        )

        await session.add_items([_user("first"), _user("second")])
        popped = await session.pop_item()
        assert popped is not None
        assert _content(popped) == "second"

        items = await session.get_items()
        assert len(items) == 1
        underlying.close()

    @pytest.mark.asyncio
    async def test_pop_item_in_concurrent_phase_syncs_back_buffer(self) -> None:
        """pop_item in CONCURRENT phase should also pop from the back buffer."""
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.9,
            summarizer=summarizer,
        )

        # Reach checkpoint.
        for i in range(5):
            await session.add_items([_user(f"msg {i}")])
        assert session.has_back_buffer

        # Add concurrent item.
        await session.add_items([_user("concurrent")])
        back_len_before = len(_back(session))

        # Pop should remove from both buffers.
        popped = await session.pop_item()
        assert popped is not None
        assert _content(popped) == "concurrent"
        assert len(_back(session)) == back_len_before - 1
        underlying.close()

    @pytest.mark.asyncio
    async def test_pop_item_after_checkpoint_preserves_summary_seed(self) -> None:
        """pop_item right after checkpoint must NOT delete the summary seed.

        When the back buffer contains only the summary seed (no concurrent
        items yet), a pop from the active buffer should not touch the back
        buffer at all.  Previously, the code would unconditionally pop from
        the back buffer whenever it was non-None and non-empty, which would
        corrupt the compaction state by removing the summary.
        """
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,  # checkpoint at 5
            swap_threshold=0.9,  # swap at 9
            summarizer=summarizer,
        )

        # Fill to checkpoint threshold to trigger back buffer creation.
        for i in range(5):
            await session.add_items([_user(f"msg {i}")])
        assert session.has_back_buffer

        # At this point, the back buffer contains only the summary seed.
        assert len(_back(session)) == 1
        summary_content = _content(_back(session)[0])

        # Pop from the active buffer.  This should NOT touch the back
        # buffer because it only has the summary seed.
        popped = await session.pop_item()
        assert popped is not None
        assert _content(popped) == "msg 4"

        # The back buffer must still contain exactly the summary seed.
        assert len(_back(session)) == 1
        assert _content(_back(session)[0]) == summary_content
        underlying.close()

    @pytest.mark.asyncio
    async def test_pop_item_concurrent_with_items_still_pops_back_buffer(self) -> None:
        """pop_item in CONCURRENT phase with concurrent items should still pop
        from the back buffer (only the concurrent item, not the summary)."""
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,  # checkpoint at 5
            swap_threshold=0.9,  # swap at 9
            summarizer=summarizer,
        )

        # Trigger checkpoint.
        for i in range(5):
            await session.add_items([_user(f"msg {i}")])
        assert session.has_back_buffer

        # Add concurrent items so the back buffer has summary + 2 items.
        await session.add_items([_user("concurrent 0")])
        await session.add_items([_user("concurrent 1")])
        assert len(_back(session)) == 3  # summary + 2

        # Pop should remove the last concurrent item from both buffers.
        popped = await session.pop_item()
        assert popped is not None
        assert _content(popped) == "concurrent 1"
        assert len(_back(session)) == 2  # summary + 1

        # Pop again -- removes the other concurrent item.
        popped = await session.pop_item()
        assert popped is not None
        assert _content(popped) == "concurrent 0"
        assert len(_back(session)) == 1  # only summary seed remains

        # Pop once more -- back buffer only has summary seed, so it should
        # NOT be touched.
        summary_content = _content(_back(session)[0])
        popped = await session.pop_item()
        assert popped is not None
        assert _content(popped) == "msg 4"
        assert len(_back(session)) == 1
        assert _content(_back(session)[0]) == summary_content
        underlying.close()

    @pytest.mark.asyncio
    async def test_pop_item_empty_session(self) -> None:
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            summarizer=_trivial_summarizer,
        )
        result = await session.pop_item()
        assert result is None
        underlying.close()

    @pytest.mark.asyncio
    async def test_clear_session_resets_everything(self) -> None:
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.9,
            summarizer=summarizer,
        )

        # Get into concurrent phase.
        for i in range(5):
            await session.add_items([_user(f"msg {i}")])
        assert session.has_back_buffer
        assert session.generation_count == 1

        await session.clear_session()

        assert not session.has_back_buffer
        assert session.generation_count == 0
        assert session._back_buffer is None
        assert session._summary_generations == []
        items = await session.get_items()
        assert len(items) == 0
        underlying.close()

    @pytest.mark.asyncio
    async def test_add_empty_items_is_noop(self) -> None:
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            summarizer=_trivial_summarizer,
        )
        await session.add_items([])
        items = await session.get_items()
        assert len(items) == 0
        underlying.close()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_batch_add_crosses_both_thresholds(self) -> None:
        """Adding a large batch that crosses checkpoint in NORMAL stays in CONCURRENT.

        The swap does not happen in the same add_items call that triggers the
        checkpoint, because the back buffer was just seeded.
        """
        underlying = SQLiteSession("test")
        summarizer, calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.9,
            summarizer=summarizer,
        )

        # Add 7 items at once -- crosses checkpoint (5) but should not swap (9).
        items = [_user(f"batch {i}") for i in range(7)]
        await session.add_items(items)

        assert session.has_back_buffer
        assert len(calls) == 1
        underlying.close()

    @pytest.mark.asyncio
    async def test_swap_threshold_exactly_one(self) -> None:
        """swap_threshold=1.0 is valid and triggers at 100% capacity."""
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=1.0,
            summarizer=summarizer,
        )

        # Trigger checkpoint at 5.
        for i in range(5):
            await session.add_items([_user(f"msg {i}")])
        assert session.has_back_buffer

        # Need to reach 10 items for swap.
        for i in range(5):
            await session.add_items([_user(f"extra {i}")])
        assert not session.has_back_buffer
        underlying.close()

    @pytest.mark.asyncio
    async def test_back_buffer_is_deep_copied(self) -> None:
        """Ensure items added to the back buffer are deep copies, not references."""
        underlying = SQLiteSession("test")
        summarizer, _calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.9,
            summarizer=summarizer,
        )

        # Trigger checkpoint.
        for i in range(5):
            await session.add_items([_user(f"msg {i}")])
        assert session.has_back_buffer

        # Add an item and then mutate the original dict.
        mutable_item: TResponseInputItem = _user("mutable")
        await session.add_items([mutable_item])
        cast(dict[str, Any], mutable_item)["content"] = "MUTATED"

        # The back buffer should not be affected.
        last_back = _back(session)[-1]
        assert _content(last_back) == "mutable"
        underlying.close()

    @pytest.mark.asyncio
    async def test_summarizer_called_with_current_snapshot(self) -> None:
        """The summarizer receives the full current active buffer at checkpoint time."""
        received_items: list[list[TResponseInputItem]] = []

        async def capturing_summarizer(
            items: list[TResponseInputItem],
        ) -> list[TResponseInputItem]:
            received_items.append(list(items))
            return [_assistant("summary")]

        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.9,
            summarizer=capturing_summarizer,
        )

        for i in range(5):
            await session.add_items([_user(f"msg {i}")])

        assert len(received_items) == 1
        assert len(received_items[0]) == 5
        contents = [_content(item) for item in received_items[0]]
        assert contents == [f"msg {i}" for i in range(5)]
        underlying.close()

    @pytest.mark.asyncio
    async def test_multiple_items_in_single_add(self) -> None:
        """Adding multiple items in a single call works correctly."""
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.9,
            summarizer=_trivial_summarizer,
        )

        items = [_user(f"msg {i}") for i in range(3)]
        await session.add_items(items)

        retrieved = await session.get_items()
        assert len(retrieved) == 3
        underlying.close()

    @pytest.mark.asyncio
    async def test_session_settings_passthrough(self) -> None:
        """Session settings are stored on the double buffer session."""
        from agents.memory.session_settings import SessionSettings

        settings = SessionSettings(limit=42)
        underlying = SQLiteSession("test")
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            summarizer=_trivial_summarizer,
            session_settings=settings,
        )
        assert session.session_settings is not None
        assert session.session_settings.limit == 42
        underlying.close()


# ---------------------------------------------------------------------------
# Stop-the-world fallback tests
# ---------------------------------------------------------------------------


class TestStopTheWorldFallback:
    """When items hit the swap threshold with no checkpoint taken, we MUST
    do an inline checkpoint+swap -- NEVER skip compaction."""

    @pytest.mark.asyncio
    async def test_large_batch_crosses_both_thresholds(self) -> None:
        """Adding a batch that crosses both checkpoint AND swap thresholds
        at once should still compact (stop-the-world), not just continue
        with an uncompacted buffer."""
        underlying = SQLiteSession("test")
        summarizer, calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,  # checkpoint at 5
            swap_threshold=0.9,  # swap at 9
            summarizer=summarizer,
        )

        # Add 10 items at once -- crosses both thresholds in NORMAL phase
        items = [_user(f"batch {i}") for i in range(10)]
        await session.add_items(items)

        # Should have performed a stop-the-world: checkpoint + swap
        assert not session.has_back_buffer  # back to NORMAL after swap
        assert session.generation_count == 1  # checkpoint was called
        assert len(calls) >= 1  # summarizer was invoked

        # Active buffer should contain ONLY the summary -- the checkpoint
        # already included all 10 items so nothing extra is appended.
        active_items = await session.get_items()
        assert len(active_items) == 1
        assert "Summary" in _content(active_items[0])
        underlying.close()

    @pytest.mark.asyncio
    async def test_stop_the_world_summarises_all_items(self) -> None:
        """During stop-the-world, the checkpoint summarizes ALL items (old +
        new). After swap the active buffer holds only the summary -- the new
        items are NOT duplicated alongside it."""
        underlying = SQLiteSession("test")
        summarizer, calls = await _counting_summarizer_factory()
        session = DoubleBufferSession(
            session_id="test",
            underlying_session=underlying,
            max_context_items=10,
            checkpoint_threshold=0.5,
            swap_threshold=0.9,
            summarizer=summarizer,
        )

        # Add items one by one up to 4 (below checkpoint)
        for i in range(4):
            await session.add_items([_user(f"old {i}")])
        assert not session.has_back_buffer

        # Now add 6 more at once, pushing total to 10 (>= swap at 9)
        batch = [_user(f"new {i}") for i in range(6)]
        await session.add_items(batch)

        # Should have done stop-the-world swap
        assert not session.has_back_buffer
        assert session.generation_count == 1

        # The summarizer should have received all 10 items (4 old + 6 new).
        assert len(calls) == 1
        assert len(calls[0]) == 10

        # After swap the active buffer contains ONLY the summary -- no
        # duplicated new items.
        active_items = await session.get_items()
        assert len(active_items) == 1
        assert "Summary" in _content(active_items[0])
        underlying.close()
