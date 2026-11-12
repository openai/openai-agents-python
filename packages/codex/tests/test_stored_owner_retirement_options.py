"""Stored-owner preservation validates before entering the close owner."""

from unittest.mock import AsyncMock

import pytest

from _agents_codex.session import Session


@pytest.mark.parametrize("value", [False, True])
async def test_preservation_option_reaches_the_existing_close_owner(monkeypatch, value):
    close = AsyncMock()
    monkeypatch.setattr(Session, "_close_stored_history", close)
    owner = object.__new__(Session)
    await owner.delete_stored(preserve_descendants=value)
    close.assert_awaited_once_with({"kind": "delete", "preserve_descendants": value})


async def test_default_delete_preserves_the_physical_delete_choice(monkeypatch):
    close = AsyncMock()
    monkeypatch.setattr(Session, "_close_stored_history", close)
    await object.__new__(Session).delete_stored()
    close.assert_awaited_once_with({"kind": "delete", "preserve_descendants": False})


@pytest.mark.parametrize("value", [None, 1, "true"])
async def test_invalid_preservation_option_has_no_close_effect(monkeypatch, value):
    close = AsyncMock()
    monkeypatch.setattr(Session, "_close_stored_history", close)
    with pytest.raises(TypeError, match="preserve_descendants"):
        await object.__new__(Session).delete_stored(preserve_descendants=value)
    close.assert_not_called()
