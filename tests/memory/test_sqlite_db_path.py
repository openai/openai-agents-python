"""SQLite file sessions accept ~/... paths by expanding the user directory."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiosqlite")

from agents.extensions.memory import AsyncSQLiteSession
from agents.memory import SQLiteSession


@pytest.mark.asyncio
async def test_sqlite_session_expands_tilde_db_path(tmp_path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    db_path = "~/hermes_tilde_sync.db"
    expanded = Path(db_path).expanduser()
    assert str(expanded).startswith(str(fake_home))

    session = SQLiteSession("tilde-sync", db_path=db_path)
    try:
        await session.add_items([{"role": "user", "content": "hello"}])
        items = await session.get_items()
        assert len(items) == 1
        assert expanded.exists()
    finally:
        session.close()


@pytest.mark.asyncio
async def test_async_sqlite_session_expands_tilde_db_path(tmp_path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    db_path = "~/hermes_tilde_async.db"
    expanded = Path(db_path).expanduser()
    assert str(expanded).startswith(str(fake_home))

    session = AsyncSQLiteSession("tilde-async", db_path=db_path)
    try:
        await session.add_items([{"role": "user", "content": "hello"}])
        items = await session.get_items()
        assert len(items) == 1
        assert expanded.exists()
    finally:
        await session.close()
