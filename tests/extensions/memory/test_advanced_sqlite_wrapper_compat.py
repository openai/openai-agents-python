from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agents.extensions.memory.advanced_sqlite_session import AdvancedSQLiteSession

pytestmark = pytest.mark.asyncio


async def test_advanced_sqlite_get_items_preserves_branch_id_positional_argument() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "advanced.db"
        session = AdvancedSQLiteSession(session_id="test", db_path=db_path, create_tables=True)

        await session.add_items([
            {"role": "user", "content": "main message"},
        ])
        branch_id = await session.create_branch_from_turn(1, "branch-a")
        assert branch_id == "branch-a"
        await session.add_items([
            {"role": "user", "content": "branch message"},
        ])
        await session.switch_to_branch("main")

        branch_items = await session.get_items(50, "branch-a")
        contents = [item.get("content") for item in branch_items if isinstance(item, dict)]

        assert "branch message" in contents
        assert "main message" not in contents
